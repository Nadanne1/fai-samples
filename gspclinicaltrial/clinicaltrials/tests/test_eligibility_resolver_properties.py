"""Property-based tests for the Eligibility Resolver.

Properties tested:
  P9  — Discrepancy Detection and Severity Classification: discrepancies
        flagged with correct severity and data points.
  P11 — Eligibility Determination Completeness: determination includes
        per-criterion breakdown and FHIR refs.
  P28 — FHIR Resource Deduplication on Merge: no duplicate clinical
        concepts after merge.

Validates Requirements: 3.2, 3.4, 3.5, 11.3
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from components.eligibility_resolver import (
    _classify_discrepancy,
    _detect_discrepancy,
    deduplicate_fhir_resources,
    evaluate_criterion,
    resolve_eligibility,
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_criterion_id_st = st.from_regex(r"IE-[0-9]{3}", fullmatch=True)
_criterion_type_st = st.sampled_from(["inclusion", "exclusion"])
_data_type_st = st.sampled_from(["boolean", "integer", "decimal", "string"])
_severity_st = st.sampled_from(["informational", "warning", "critical"])
_result_st = st.sampled_from(["pass", "fail", "indeterminate"])

_bool_value_st = st.booleans()
_int_value_st = st.integers(min_value=0, max_value=200)
_decimal_value_st = st.floats(min_value=0.1, max_value=500.0, allow_nan=False, allow_infinity=False)
_string_value_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N")),
    min_size=1,
    max_size=20,
)

_fhir_ref_st = st.from_regex(
    r"(Patient|Condition|Observation|MedicationRequest|Procedure|AllergyIntolerance)/[a-f0-9\-]{8}",
    fullmatch=True,
)
_fhir_refs_st = st.lists(_fhir_ref_st, min_size=0, max_size=3)

_coding_system_st = st.sampled_from([
    "http://snomed.info/sct",
    "http://hl7.org/fhir/sid/icd-10",
    "http://loinc.org",
    "http://www.nlm.nih.gov/research/umls/rxnorm",
])
_coding_code_st = st.from_regex(r"[A-Z0-9]{3,8}", fullmatch=True)

_resource_type_st = st.sampled_from([
    "Condition", "Observation", "MedicationRequest",
    "AllergyIntolerance", "Procedure",
])


def _criterion_result_st():
    """Strategy for a single criterion evaluation result dict."""
    return st.fixed_dictionaries({
        "criterion_id": _criterion_id_st,
        "result": _result_st,
        "discrepancy": st.one_of(
            st.none(),
            st.fixed_dictionaries({
                "criterion_id": _criterion_id_st,
                "severity": _severity_st,
                "patient_reported": _string_value_st,
                "fhir_record": _string_value_st,
                "description": st.just("test discrepancy"),
            }),
        ),
        "fhir_refs": _fhir_refs_st,
    })


def _fhir_resource_st(resource_type: str | None = None):
    """Strategy for a minimal FHIR resource with a clinical code."""
    rtype = resource_type or "Condition"
    return st.fixed_dictionaries({
        "id": st.uuids().map(str),
        "resourceType": st.just(rtype),
        "code": st.fixed_dictionaries({
            "coding": st.lists(
                st.fixed_dictionaries({
                    "system": _coding_system_st,
                    "code": _coding_code_st,
                    "display": _string_value_st,
                }),
                min_size=1,
                max_size=1,
            ),
        }),
    })


# ===================================================================
# Property 9 — Discrepancy Detection and Severity Classification
# Verify discrepancies flagged with correct severity and data points.
# Validates: Requirements 3.2
# ===================================================================


@given(
    patient_val=_bool_value_st,
    fhir_val=_bool_value_st,
    cid=_criterion_id_st,
    ctype=_criterion_type_st,
)
@settings(max_examples=50)
def test_p9_boolean_disagreement_is_critical(
    patient_val: bool, fhir_val: bool, cid: str, ctype: str
):
    """Any boolean disagreement between patient-reported and FHIR record
    must be classified as critical severity."""
    assume(patient_val != fhir_val)

    disc = _detect_discrepancy(
        patient_val, fhir_val,
        {"criterion_id": cid, "criterion_type": ctype, "data_type": "boolean"},
    )

    assert disc is not None, "Boolean disagreement must produce a discrepancy"
    assert disc["severity"] == "critical", (
        f"Boolean disagreement must be critical, got {disc['severity']}"
    )
    assert disc["patient_reported"] == patient_val
    assert disc["fhir_record"] == fhir_val
    assert disc["criterion_id"] == cid


@given(
    patient_val=_bool_value_st,
    cid=_criterion_id_st,
)
@settings(max_examples=30)
def test_p9_boolean_agreement_no_discrepancy(patient_val: bool, cid: str):
    """When patient-reported and FHIR boolean values agree, no discrepancy
    should be detected."""
    disc = _detect_discrepancy(
        patient_val, patient_val,
        {"criterion_id": cid, "criterion_type": "inclusion", "data_type": "boolean"},
    )
    assert disc is None, "Matching boolean values must not produce a discrepancy"


@given(
    patient_val=_decimal_value_st,
    fhir_val=_decimal_value_st,
    cid=_criterion_id_st,
)
@settings(max_examples=50)
def test_p9_numeric_deviation_severity_classification(
    patient_val: float, fhir_val: float, cid: str
):
    """Numeric discrepancies must be classified by deviation magnitude:
    >50% → critical, 10-50% → warning, <10% → informational."""
    assume(fhir_val != 0)
    assume(patient_val != fhir_val)

    disc = _detect_discrepancy(
        patient_val, fhir_val,
        {"criterion_id": cid, "criterion_type": "inclusion", "data_type": "decimal"},
    )

    if disc is None:
        # Values were equal after numeric parsing — acceptable
        return

    deviation = abs(patient_val - fhir_val) / abs(fhir_val)

    if deviation > 0.5:
        assert disc["severity"] == "critical", (
            f"Deviation {deviation:.2f} should be critical, got {disc['severity']}"
        )
    elif deviation > 0.1:
        assert disc["severity"] == "warning", (
            f"Deviation {deviation:.2f} should be warning, got {disc['severity']}"
        )
    else:
        assert disc["severity"] == "informational", (
            f"Deviation {deviation:.2f} should be informational, got {disc['severity']}"
        )


@given(
    patient_val=_string_value_st,
    fhir_val=_string_value_st,
    cid=_criterion_id_st,
)
@settings(max_examples=50)
def test_p9_exclusion_string_mismatch_is_critical(
    patient_val: str, fhir_val: str, cid: str
):
    """Any string mismatch on an exclusion criterion must be classified as
    critical severity."""
    assume(patient_val.strip().lower() != fhir_val.strip().lower())

    disc = _detect_discrepancy(
        patient_val, fhir_val,
        {"criterion_id": cid, "criterion_type": "exclusion", "data_type": "string"},
    )

    assert disc is not None, "String mismatch on exclusion must produce a discrepancy"
    assert disc["severity"] == "critical", (
        f"Exclusion string mismatch must be critical, got {disc['severity']}"
    )


@given(cid=_criterion_id_st, ctype=_criterion_type_st)
@settings(max_examples=30)
def test_p9_no_fhir_value_no_discrepancy(cid: str, ctype: str):
    """When FHIR record value is None (no data to compare), no discrepancy
    should be detected."""
    disc = _detect_discrepancy(
        "some_value", None,
        {"criterion_id": cid, "criterion_type": ctype, "data_type": "string"},
    )
    assert disc is None, "No FHIR data should mean no discrepancy"


@given(
    patient_val=_decimal_value_st,
    fhir_val=_decimal_value_st,
    cid=_criterion_id_st,
)
@settings(max_examples=30)
def test_p9_discrepancy_contains_both_data_points(
    patient_val: float, fhir_val: float, cid: str
):
    """Every detected discrepancy must include both the patient-reported and
    FHIR record values for audit purposes."""
    assume(fhir_val != 0)
    assume(patient_val != fhir_val)

    disc = _detect_discrepancy(
        patient_val, fhir_val,
        {"criterion_id": cid, "criterion_type": "inclusion", "data_type": "decimal"},
    )

    if disc is not None:
        assert "patient_reported" in disc, "Discrepancy must include patient_reported"
        assert "fhir_record" in disc, "Discrepancy must include fhir_record"
        assert "severity" in disc, "Discrepancy must include severity"
        assert "criterion_id" in disc, "Discrepancy must include criterion_id"
        assert disc["severity"] in ("informational", "warning", "critical")



# ===================================================================
# Property 11 — Eligibility Determination Completeness
# Verify determination includes per-criterion breakdown and FHIR refs.
# Validates: Requirements 3.4, 3.5
# ===================================================================


@given(results=st.lists(_criterion_result_st(), min_size=1, max_size=10))
@settings(max_examples=50)
def test_p11_determination_includes_breakdown(results: list[dict]):
    """resolve_eligibility must return a breakdown containing every input
    criterion result."""
    out = resolve_eligibility.invoke({"criteria_results": results})

    assert "determination" in out
    assert "breakdown" in out
    assert "fhir_refs" in out
    assert len(out["breakdown"]) == len(results), (
        f"Breakdown has {len(out['breakdown'])} items but input had {len(results)}"
    )


@given(results=st.lists(_criterion_result_st(), min_size=1, max_size=10))
@settings(max_examples=50)
def test_p11_determination_is_valid_value(results: list[dict]):
    """The determination must be one of eligible, ineligible, or borderline."""
    out = resolve_eligibility.invoke({"criteria_results": results})
    assert out["determination"] in ("eligible", "ineligible", "borderline"), (
        f"Invalid determination: {out['determination']}"
    )


@given(results=st.lists(_criterion_result_st(), min_size=1, max_size=8))
@settings(max_examples=50)
def test_p11_fhir_refs_aggregated(results: list[dict]):
    """All FHIR resource references from individual criteria must appear in
    the aggregated fhir_refs of the determination."""
    out = resolve_eligibility.invoke({"criteria_results": results})

    expected_refs = set()
    for cr in results:
        expected_refs.update(cr.get("fhir_refs", []))

    actual_refs = set(out["fhir_refs"])
    assert expected_refs == actual_refs, (
        f"Missing refs: {expected_refs - actual_refs}"
    )


@given(results=st.lists(_criterion_result_st(), min_size=1, max_size=8))
@settings(max_examples=50)
def test_p11_fhir_refs_no_duplicates(results: list[dict]):
    """The aggregated fhir_refs list must not contain duplicates."""
    out = resolve_eligibility.invoke({"criteria_results": results})
    refs = out["fhir_refs"]
    assert len(refs) == len(set(refs)), "fhir_refs contains duplicates"


@given(results=st.lists(_criterion_result_st(), min_size=1, max_size=8))
@settings(max_examples=50)
def test_p11_any_fail_means_ineligible(results: list[dict]):
    """If any criterion result is 'fail', the determination must be
    'ineligible'."""
    has_fail = any(cr["result"] == "fail" for cr in results)
    out = resolve_eligibility.invoke({"criteria_results": results})

    if has_fail:
        assert out["determination"] == "ineligible", (
            f"Expected ineligible when a criterion failed, got {out['determination']}"
        )


@given(results=st.lists(_criterion_result_st(), min_size=1, max_size=8))
@settings(max_examples=50)
def test_p11_all_pass_no_critical_means_eligible(results: list[dict]):
    """If all criteria pass and there are no critical discrepancies, the
    determination must be 'eligible'."""
    all_pass = all(cr["result"] == "pass" for cr in results)
    has_critical = any(
        cr.get("discrepancy") and cr["discrepancy"].get("severity") == "critical"
        for cr in results
    )

    out = resolve_eligibility.invoke({"criteria_results": results})

    if all_pass and not has_critical:
        assert out["determination"] == "eligible", (
            f"Expected eligible when all pass with no critical discrepancies, "
            f"got {out['determination']}"
        )


@given(results=st.lists(_criterion_result_st(), min_size=1, max_size=8))
@settings(max_examples=50)
def test_p11_indeterminate_without_fail_means_borderline(results: list[dict]):
    """If any criterion is indeterminate and none fail, the determination
    must be 'borderline'."""
    has_fail = any(cr["result"] == "fail" for cr in results)
    has_indeterminate = any(cr["result"] == "indeterminate" for cr in results)

    out = resolve_eligibility.invoke({"criteria_results": results})

    if has_indeterminate and not has_fail:
        assert out["determination"] == "borderline", (
            f"Expected borderline with indeterminate and no fail, "
            f"got {out['determination']}"
        )


@given(results=st.lists(_criterion_result_st(), min_size=1, max_size=8))
@settings(max_examples=50)
def test_p11_critical_discrepancy_tracked(results: list[dict]):
    """has_critical_discrepancies must be True iff any criterion has a
    critical-severity discrepancy."""
    has_critical = any(
        cr.get("discrepancy") and cr["discrepancy"].get("severity") == "critical"
        for cr in results
    )

    out = resolve_eligibility.invoke({"criteria_results": results})
    assert out["has_critical_discrepancies"] == has_critical


def test_p11_empty_results_returns_indeterminate():
    """An empty criteria_results list must produce an indeterminate
    determination with empty breakdown and refs."""
    out = resolve_eligibility.invoke({"criteria_results": []})
    assert out["determination"] == "indeterminate"
    assert out["breakdown"] == []
    assert out["fhir_refs"] == []
    assert out["has_critical_discrepancies"] is False



# ===================================================================
# Property 28 — FHIR Resource Deduplication on Merge
# Verify no duplicate clinical concepts after merge.
# Validates: Requirements 11.3
# ===================================================================


@given(
    rtype=_resource_type_st,
    system=_coding_system_st,
    code=_coding_code_st,
)
@settings(max_examples=50)
def test_p28_same_code_deduplicated(rtype: str, system: str, code: str):
    """When local and external data contain a resource with the same clinical
    code (system|code), the merged result must contain exactly one copy."""
    resource_local = {
        "id": "local-001",
        "resourceType": rtype,
        "code": {"coding": [{"system": system, "code": code, "display": "Test"}]},
    }
    resource_external = {
        "id": "external-001",
        "resourceType": rtype,
        "code": {"coding": [{"system": system, "code": code, "display": "Test"}]},
    }

    local_data = {rtype: [resource_local]}
    external_data = {rtype: [resource_external]}

    merged = deduplicate_fhir_resources(local_data, external_data)

    assert len(merged[rtype]) == 1, (
        f"Expected 1 resource after dedup, got {len(merged[rtype])}"
    )
    # Local resource should be preferred (added first)
    assert merged[rtype][0]["id"] == "local-001"


@given(
    rtype=_resource_type_st,
    system=_coding_system_st,
    code1=_coding_code_st,
    code2=_coding_code_st,
)
@settings(max_examples=50)
def test_p28_different_codes_both_kept(
    rtype: str, system: str, code1: str, code2: str
):
    """When local and external data contain resources with different clinical
    codes, both must be present in the merged result."""
    assume(code1 != code2)

    local_data = {rtype: [{
        "id": "local-001", "resourceType": rtype,
        "code": {"coding": [{"system": system, "code": code1, "display": "A"}]},
    }]}
    external_data = {rtype: [{
        "id": "ext-001", "resourceType": rtype,
        "code": {"coding": [{"system": system, "code": code2, "display": "B"}]},
    }]}

    merged = deduplicate_fhir_resources(local_data, external_data)

    assert len(merged[rtype]) == 2, (
        f"Expected 2 resources for different codes, got {len(merged[rtype])}"
    )


@given(
    rtype=_resource_type_st,
    system=_coding_system_st,
    code=_coding_code_st,
    n_dupes=st.integers(min_value=2, max_value=5),
)
@settings(max_examples=30)
def test_p28_multiple_external_duplicates_collapsed(
    rtype: str, system: str, code: str, n_dupes: int
):
    """Multiple external resources with the same clinical code should all be
    collapsed into a single entry when merged with a local resource sharing
    that code."""
    local_data = {rtype: [{
        "id": "local-001", "resourceType": rtype,
        "code": {"coding": [{"system": system, "code": code, "display": "X"}]},
    }]}
    external_data = {rtype: [
        {
            "id": f"ext-{i}", "resourceType": rtype,
            "code": {"coding": [{"system": system, "code": code, "display": "X"}]},
        }
        for i in range(n_dupes)
    ]}

    merged = deduplicate_fhir_resources(local_data, external_data)

    assert len(merged[rtype]) == 1, (
        f"Expected 1 after dedup of {n_dupes} external dupes, got {len(merged[rtype])}"
    )


@given(rtype=_resource_type_st)
@settings(max_examples=30)
def test_p28_empty_external_preserves_local(rtype: str):
    """When external data is empty, the merged result must equal the local
    data exactly."""
    local_resources = [
        {"id": f"loc-{i}", "resourceType": rtype,
         "code": {"coding": [{"system": "http://snomed.info/sct",
                               "code": f"C{i}", "display": f"D{i}"}]}}
        for i in range(3)
    ]
    local_data = {rtype: local_resources}
    external_data: dict[str, list[dict]] = {}

    merged = deduplicate_fhir_resources(local_data, external_data)

    assert merged[rtype] == local_resources


@given(rtype=_resource_type_st)
@settings(max_examples=30)
def test_p28_empty_local_preserves_external(rtype: str):
    """When local data is empty, the merged result must contain all external
    resources."""
    external_resources = [
        {"id": f"ext-{i}", "resourceType": rtype,
         "code": {"coding": [{"system": "http://loinc.org",
                               "code": f"L{i}", "display": f"Lab{i}"}]}}
        for i in range(3)
    ]
    local_data: dict[str, list[dict]] = {}
    external_data = {rtype: external_resources}

    merged = deduplicate_fhir_resources(local_data, external_data)

    assert merged[rtype] == external_resources


@given(
    system=_coding_system_st,
    code=_coding_code_st,
)
@settings(max_examples=30)
def test_p28_cross_resource_types_not_deduped(system: str, code: str):
    """Resources of different types sharing the same clinical code must NOT
    be deduplicated against each other."""
    local_data = {"Condition": [{
        "id": "cond-1", "resourceType": "Condition",
        "code": {"coding": [{"system": system, "code": code, "display": "X"}]},
    }]}
    external_data = {"Observation": [{
        "id": "obs-1", "resourceType": "Observation",
        "code": {"coding": [{"system": system, "code": code, "display": "X"}]},
    }]}

    merged = deduplicate_fhir_resources(local_data, external_data)

    assert len(merged.get("Condition", [])) == 1
    assert len(merged.get("Observation", [])) == 1


@given(
    rtype=_resource_type_st,
    system=_coding_system_st,
    codes=st.lists(_coding_code_st, min_size=2, max_size=6, unique=True),
)
@settings(max_examples=30)
def test_p28_no_duplicate_identity_keys_in_merged(
    rtype: str, system: str, codes: list[str]
):
    """After merging, no two resources of the same type should share the same
    identity key (system|code)."""
    # Create overlapping local and external sets
    mid = len(codes) // 2
    local_data = {rtype: [
        {"id": f"loc-{i}", "resourceType": rtype,
         "code": {"coding": [{"system": system, "code": c, "display": "D"}]}}
        for i, c in enumerate(codes)
    ]}
    external_data = {rtype: [
        {"id": f"ext-{i}", "resourceType": rtype,
         "code": {"coding": [{"system": system, "code": c, "display": "D"}]}}
        for i, c in enumerate(codes[mid:])
    ]}

    merged = deduplicate_fhir_resources(local_data, external_data)

    # Extract identity keys and verify uniqueness
    keys = []
    for r in merged[rtype]:
        codings = r.get("code", {}).get("coding", [])
        if codings:
            keys.append(f"{codings[0]['system']}|{codings[0]['code']}")

    assert len(keys) == len(set(keys)), (
        f"Duplicate identity keys found in merged result: {keys}"
    )
    # Total should equal the number of unique codes
    assert len(merged[rtype]) == len(codes), (
        f"Expected {len(codes)} unique resources, got {len(merged[rtype])}"
    )


def test_p28_ehr_metadata_keys_skipped():
    """Non-resource keys like 'ehr_query_failed' injected by the EHR tool
    must be excluded from the merged result."""
    local_data = {"Condition": [
        {"id": "c1", "resourceType": "Condition",
         "code": {"coding": [{"system": "http://snomed.info/sct",
                               "code": "123", "display": "Test"}]}},
    ]}
    external_data = {
        "ehr_query_failed": [{"error": True}],
        "failure_reason": [{"msg": "timeout"}],
    }

    merged = deduplicate_fhir_resources(local_data, external_data)

    assert "ehr_query_failed" not in merged
    assert "failure_reason" not in merged
    assert len(merged["Condition"]) == 1
