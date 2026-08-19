"""Property-based tests for the Protocol Engine.

Properties tested:
  P1  — Protocol Criteria Round-Trip: parsing then generating Questionnaire
        then extracting criteria back produces equivalent eligibility rules.
  P2  — Questionnaire Generation Completeness: one Questionnaire item per
        criterion with correct enableWhen logic for deep-dive modules.
  P3  — Questionnaire Persistence with Trial Reference: stored Questionnaire
        contains trial protocol identifier and version.
  P31 — ClinicalTrials.gov Baseline Integration: Questionnaire incorporates
        ClinicalTrials.gov criteria as baseline.

Validates Requirements: 1.1, 1.2, 1.4, 1.6, 12.1, 12.2
"""

import json
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from components.protocol_engine import (
    _build_questionnaire_item,
    _build_deep_dive_group,
    _detect_deep_dive_module,
    _DEEP_DIVE_MODULES,
    generate_fhir_questionnaire,
    clinicaltrials_gov_query,
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_VALID_DATA_TYPES = ["boolean", "integer", "decimal", "string", "date", "coding"]
_VALID_CRITERION_TYPES = ["inclusion", "exclusion"]
_VALID_OPERATORS = [
    "equals", "not_equals", "age_between", "greater_than", "less_than",
    "greater_or_equal", "less_or_equal", "exists", "not_exists",
    "contains", "in_list",
]
_FHIR_PATHS = [
    "Patient.birthDate", "Condition.code", "Observation.valueQuantity",
    "MedicationRequest.medicationCodeableConcept", "Procedure.code",
    "AllergyIntolerance.code",
]

_trial_id_st = st.from_regex(r"NCT[0-9]{8}", fullmatch=True)

_terminology_code_st = st.fixed_dictionaries({
    "system": st.sampled_from([
        "http://snomed.info/sct", "http://hl7.org/fhir/sid/icd-10",
        "http://www.nlm.nih.gov/research/umls/rxnorm",
    ]),
    "code": st.from_regex(r"[0-9]{4,8}", fullmatch=True),
    "display": st.text(min_size=3, max_size=40),
    "confidence": st.floats(min_value=0.5, max_value=1.0),
})


# A single eligibility rule — the building block for all properties
_eligibility_rule_st = st.fixed_dictionaries({
    "criterion_id": st.from_regex(r"(IE|EX)-[0-9]{3}", fullmatch=True),
    "description": st.text(min_size=5, max_size=80),
    "criterion_type": st.sampled_from(_VALID_CRITERION_TYPES),
    "data_type": st.sampled_from(_VALID_DATA_TYPES),
    "fhir_path": st.sampled_from(_FHIR_PATHS),
    "operator": st.sampled_from(_VALID_OPERATORS),
    "value": st.one_of(
        st.booleans(),
        st.integers(min_value=0, max_value=200),
        st.text(min_size=1, max_size=20),
        st.lists(st.integers(min_value=0, max_value=200), min_size=2, max_size=2),
    ),
    "terminology_codes": st.lists(_terminology_code_st, min_size=0, max_size=3),
})

# Lists of rules (1–8 rules keeps tests fast)
_eligibility_rules_st = st.lists(_eligibility_rule_st, min_size=1, max_size=8)

# Rules that are guaranteed to trigger a deep-dive module
_DEEP_DIVE_DESCRIPTIONS = {
    "cardiovascular": "History of heart failure or cardiac arrhythmia",
    "renal": "Chronic kidney disease with low eGFR",
    "hepatic": "Elevated liver enzymes (ALT/AST)",
    "oncology": "Prior cancer diagnosis or chemotherapy treatment",
}

_deep_dive_rule_st = st.sampled_from(list(_DEEP_DIVE_DESCRIPTIONS.keys())).flatmap(
    lambda module: st.fixed_dictionaries({
        "criterion_id": st.from_regex(r"(IE|EX)-[0-9]{3}", fullmatch=True),
        "description": st.just(_DEEP_DIVE_DESCRIPTIONS[module]),
        "criterion_type": st.just("inclusion"),
        "data_type": st.just("boolean"),
        "fhir_path": st.just("Condition.code"),
        "operator": st.just("exists"),
        "value": st.just(True),
        "terminology_codes": st.just([]),
    })
)

# A non-deep-dive rule (description won't match any module keywords)
_plain_rule_st = st.fixed_dictionaries({
    "criterion_id": st.from_regex(r"IE-[0-9]{3}", fullmatch=True),
    "description": st.just("Age between 18 and 75 years"),
    "criterion_type": st.just("inclusion"),
    "data_type": st.just("boolean"),
    "fhir_path": st.just("Patient.birthDate"),
    "operator": st.just("age_between"),
    "value": st.just([18, 75]),
    "terminology_codes": st.just([]),
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_healthlake_store(resource_id_override: str | None = None):
    """Return a mock healthlake_store_resource that succeeds."""
    mock = MagicMock()
    def _store(args):
        rid = resource_id_override or args.get("resource", {}).get("id", "mock-id")
        return {"resource_id": rid, "resource_type": "Questionnaire", "status": "created"}
    mock.invoke.side_effect = _store
    return mock


def _mock_audit():
    """Return a mock audit_log_event."""
    mock = MagicMock()
    mock.invoke.return_value = None
    return mock


def _invoke_generate(rules: list[dict], trial_id: str,
                     mock_store=None, mock_audit_fn=None):
    """Invoke generate_fhir_questionnaire with mocked HealthLake and audit."""
    mock_store = mock_store or _mock_healthlake_store()
    mock_audit_fn = mock_audit_fn or _mock_audit()

    with (
        patch("components.protocol_engine.healthlake_store_resource", mock_store),
        patch("components.protocol_engine.audit_log_event", mock_audit_fn),
    ):
        return generate_fhir_questionnaire.invoke({
            "eligibility_rules": rules,
            "trial_id": trial_id,
        })


def _extract_rules_from_questionnaire(questionnaire: dict) -> list[dict]:
    """Extract eligibility rules back from a FHIR Questionnaire's items.

    Walks all items (including nested deep-dive group sub-items) and
    reconstructs the rule dict from the eligibility-rule extension.
    """
    extracted: list[dict] = []

    def _walk(items: list[dict]):
        for item in items:
            # Check for eligibility-rule extension
            for ext in item.get("extension", []):
                if ext.get("url") == "http://example.org/fhir/StructureDefinition/eligibility-rule":
                    rule_meta = json.loads(ext["valueString"])
                    extracted.append({
                        "criterion_id": item["linkId"],
                        "description": item["text"],
                        "criterion_type": rule_meta.get("type", "inclusion"),
                        "fhir_path": rule_meta.get("fhir_path", ""),
                        "operator": rule_meta.get("operator", "equals"),
                        "value": rule_meta.get("value"),
                    })
            # Recurse into sub-items (deep-dive groups)
            if "item" in item:
                _walk(item["item"])

    _walk(questionnaire.get("item", []))
    return extracted


# ===================================================================
# Property 1 — Protocol Criteria Round-Trip
# Parsing then generating Questionnaire then extracting criteria back
# produces equivalent eligibility rules (same identifiers, descriptions,
# data types, FHIR resource references).
# Validates: Requirement 1.6
# ===================================================================


@given(rules=_eligibility_rules_st, trial_id=_trial_id_st)
@settings(max_examples=40)
def test_p1_round_trip_preserves_criterion_ids(
    rules: list[dict], trial_id: str,
):
    """Every criterion_id in the input rules must appear in the Questionnaire
    and be recoverable by extracting rules back from the resource."""
    # Ensure unique criterion IDs
    seen_ids: set[str] = set()
    unique_rules = []
    for r in rules:
        if r["criterion_id"] not in seen_ids:
            seen_ids.add(r["criterion_id"])
            unique_rules.append(r)
    assume(len(unique_rules) >= 1)

    result = _invoke_generate(unique_rules, trial_id)
    questionnaire = result["questionnaire"]
    assert questionnaire is not None

    extracted = _extract_rules_from_questionnaire(questionnaire)
    extracted_ids = {r["criterion_id"] for r in extracted}
    input_ids = {r["criterion_id"] for r in unique_rules}

    assert input_ids == extracted_ids, (
        f"Round-trip lost criterion IDs: missing={input_ids - extracted_ids}, "
        f"extra={extracted_ids - input_ids}"
    )


@given(rules=_eligibility_rules_st, trial_id=_trial_id_st)
@settings(max_examples=40)
def test_p1_round_trip_preserves_descriptions(
    rules: list[dict], trial_id: str,
):
    """Each criterion's description must survive the round-trip through
    Questionnaire generation and extraction."""
    seen_ids: set[str] = set()
    unique_rules = []
    for r in rules:
        if r["criterion_id"] not in seen_ids:
            seen_ids.add(r["criterion_id"])
            unique_rules.append(r)
    assume(len(unique_rules) >= 1)

    result = _invoke_generate(unique_rules, trial_id)
    extracted = _extract_rules_from_questionnaire(result["questionnaire"])
    extracted_by_id = {r["criterion_id"]: r for r in extracted}

    for rule in unique_rules:
        cid = rule["criterion_id"]
        assert cid in extracted_by_id, f"Criterion {cid} missing after round-trip"
        assert extracted_by_id[cid]["description"] == rule["description"], (
            f"Description mismatch for {cid}"
        )


@given(rules=_eligibility_rules_st, trial_id=_trial_id_st)
@settings(max_examples=40)
def test_p1_round_trip_preserves_fhir_path_and_operator(
    rules: list[dict], trial_id: str,
):
    """fhir_path and operator must survive the round-trip."""
    seen_ids: set[str] = set()
    unique_rules = []
    for r in rules:
        if r["criterion_id"] not in seen_ids:
            seen_ids.add(r["criterion_id"])
            unique_rules.append(r)
    assume(len(unique_rules) >= 1)

    result = _invoke_generate(unique_rules, trial_id)
    extracted = _extract_rules_from_questionnaire(result["questionnaire"])
    extracted_by_id = {r["criterion_id"]: r for r in extracted}

    for rule in unique_rules:
        cid = rule["criterion_id"]
        ext = extracted_by_id[cid]
        assert ext["fhir_path"] == rule["fhir_path"], (
            f"fhir_path mismatch for {cid}: {ext['fhir_path']} != {rule['fhir_path']}"
        )
        assert ext["operator"] == rule["operator"], (
            f"operator mismatch for {cid}: {ext['operator']} != {rule['operator']}"
        )


@given(rules=_eligibility_rules_st, trial_id=_trial_id_st)
@settings(max_examples=40)
def test_p1_round_trip_preserves_value(
    rules: list[dict], trial_id: str,
):
    """The criterion value must survive JSON serialisation round-trip."""
    seen_ids: set[str] = set()
    unique_rules = []
    for r in rules:
        if r["criterion_id"] not in seen_ids:
            seen_ids.add(r["criterion_id"])
            unique_rules.append(r)
    assume(len(unique_rules) >= 1)

    result = _invoke_generate(unique_rules, trial_id)
    extracted = _extract_rules_from_questionnaire(result["questionnaire"])
    extracted_by_id = {r["criterion_id"]: r for r in extracted}

    for rule in unique_rules:
        cid = rule["criterion_id"]
        # Values go through JSON serialisation so compare via JSON
        assert json.dumps(extracted_by_id[cid]["value"], sort_keys=True) == \
               json.dumps(rule["value"], sort_keys=True), (
            f"value mismatch for {cid}"
        )


# ===================================================================
# Property 2 — Questionnaire Generation Completeness
# One Questionnaire item per criterion with correct enableWhen logic
# for deep-dive modules.
# Validates: Requirements 1.1, 1.2
# ===================================================================


@given(rules=_eligibility_rules_st, trial_id=_trial_id_st)
@settings(max_examples=40)
def test_p2_one_item_per_criterion(
    rules: list[dict], trial_id: str,
):
    """The generated Questionnaire must contain exactly one item (possibly
    nested inside a deep-dive group) per input eligibility rule."""
    seen_ids: set[str] = set()
    unique_rules = []
    for r in rules:
        if r["criterion_id"] not in seen_ids:
            seen_ids.add(r["criterion_id"])
            unique_rules.append(r)
    assume(len(unique_rules) >= 1)

    result = _invoke_generate(unique_rules, trial_id)
    questionnaire = result["questionnaire"]
    assert questionnaire is not None

    # Collect all linkIds (including nested)
    all_link_ids: list[str] = []

    def _collect(items):
        for item in items:
            # Deep-dive groups have linkIds starting with DD-
            if not item.get("linkId", "").startswith("DD-"):
                all_link_ids.append(item["linkId"])
            if "item" in item:
                _collect(item["item"])

    _collect(questionnaire.get("item", []))

    input_ids = {r["criterion_id"] for r in unique_rules}
    collected_ids = set(all_link_ids)

    assert input_ids == collected_ids, (
        f"Item count mismatch: missing={input_ids - collected_ids}, "
        f"extra={collected_ids - input_ids}"
    )


@given(
    plain_rule=_plain_rule_st,
    deep_dive_rule=_deep_dive_rule_st,
    trial_id=_trial_id_st,
)
@settings(max_examples=30)
def test_p2_deep_dive_module_has_enable_when(
    plain_rule: dict, deep_dive_rule: dict, trial_id: str,
):
    """When a criterion matches a deep-dive module, it must be placed inside
    a group item that has an enableWhen condition."""
    # Ensure distinct IDs
    assume(plain_rule["criterion_id"] != deep_dive_rule["criterion_id"])

    rules = [plain_rule, deep_dive_rule]
    result = _invoke_generate(rules, trial_id)
    questionnaire = result["questionnaire"]

    # Find deep-dive group items (linkId starts with DD-)
    dd_groups = [
        item for item in questionnaire.get("item", [])
        if item.get("linkId", "").startswith("DD-")
    ]

    assert len(dd_groups) >= 1, "Expected at least one deep-dive group"

    for group in dd_groups:
        assert "enableWhen" in group, (
            f"Deep-dive group {group['linkId']} missing enableWhen"
        )
        assert len(group["enableWhen"]) >= 1
        ew = group["enableWhen"][0]
        assert "question" in ew, "enableWhen must reference a triggering question"
        assert "operator" in ew, "enableWhen must have an operator"


@given(rules=_eligibility_rules_st, trial_id=_trial_id_st)
@settings(max_examples=30)
def test_p2_all_items_have_required_fields(
    rules: list[dict], trial_id: str,
):
    """Every Questionnaire item must have linkId, text, type, and the
    eligibility-rule extension."""
    seen_ids: set[str] = set()
    unique_rules = []
    for r in rules:
        if r["criterion_id"] not in seen_ids:
            seen_ids.add(r["criterion_id"])
            unique_rules.append(r)
    assume(len(unique_rules) >= 1)

    result = _invoke_generate(unique_rules, trial_id)

    def _check(items):
        for item in items:
            if item.get("linkId", "").startswith("DD-"):
                # Deep-dive group — check sub-items
                assert item["type"] == "group"
                if "item" in item:
                    _check(item["item"])
                continue
            assert "linkId" in item, "Item missing linkId"
            assert "text" in item, f"Item {item['linkId']} missing text"
            assert "type" in item, f"Item {item['linkId']} missing type"
            assert "extension" in item, f"Item {item['linkId']} missing extension"
            rule_exts = [
                e for e in item["extension"]
                if e["url"] == "http://example.org/fhir/StructureDefinition/eligibility-rule"
            ]
            assert len(rule_exts) == 1, (
                f"Item {item['linkId']} must have exactly one eligibility-rule extension"
            )

    _check(result["questionnaire"].get("item", []))


@given(trial_id=_trial_id_st)
@settings(max_examples=10)
def test_p2_deep_dive_modules_reported(trial_id: str):
    """The result's deep_dive_modules list must match the modules detected
    from the input rules."""
    rules = [
        {
            "criterion_id": "IE-001", "description": "Age 18-75",
            "criterion_type": "inclusion", "data_type": "boolean",
            "fhir_path": "Patient.birthDate", "operator": "age_between",
            "value": [18, 75], "terminology_codes": [],
        },
        {
            "criterion_id": "IE-002",
            "description": "History of heart failure",
            "criterion_type": "inclusion", "data_type": "boolean",
            "fhir_path": "Condition.code", "operator": "exists",
            "value": True, "terminology_codes": [],
        },
        {
            "criterion_id": "IE-003",
            "description": "Chronic kidney disease",
            "criterion_type": "inclusion", "data_type": "boolean",
            "fhir_path": "Condition.code", "operator": "exists",
            "value": True, "terminology_codes": [],
        },
    ]

    result = _invoke_generate(rules, trial_id)

    assert "cardiovascular" in result["deep_dive_modules"]
    assert "renal" in result["deep_dive_modules"]


# ===================================================================
# Property 3 — Questionnaire Persistence with Trial Reference
# Stored Questionnaire contains trial protocol identifier and version.
# Validates: Requirement 1.4
# ===================================================================


@given(rules=_eligibility_rules_st, trial_id=_trial_id_st)
@settings(max_examples=40)
def test_p3_questionnaire_has_trial_identifier(
    rules: list[dict], trial_id: str,
):
    """The generated Questionnaire must contain an identifier referencing
    the originating trial (NCT number) and a version string."""
    seen_ids: set[str] = set()
    unique_rules = []
    for r in rules:
        if r["criterion_id"] not in seen_ids:
            seen_ids.add(r["criterion_id"])
            unique_rules.append(r)
    assume(len(unique_rules) >= 1)

    result = _invoke_generate(unique_rules, trial_id)
    q = result["questionnaire"]

    # identifier must reference the trial
    identifiers = q.get("identifier", [])
    assert len(identifiers) >= 1, "Questionnaire must have at least one identifier"
    trial_ids_in_resource = [
        ident["value"] for ident in identifiers
        if ident.get("system") == "https://clinicaltrials.gov"
    ]
    assert trial_id in trial_ids_in_resource, (
        f"Trial ID {trial_id} not found in Questionnaire identifiers"
    )

    # version must be present
    assert q.get("version"), "Questionnaire must have a version"


@given(rules=_eligibility_rules_st, trial_id=_trial_id_st)
@settings(max_examples=30)
def test_p3_questionnaire_stored_in_healthlake(
    rules: list[dict], trial_id: str,
):
    """generate_fhir_questionnaire must invoke healthlake_store_resource
    with the Questionnaire resource."""
    seen_ids: set[str] = set()
    unique_rules = []
    for r in rules:
        if r["criterion_id"] not in seen_ids:
            seen_ids.add(r["criterion_id"])
            unique_rules.append(r)
    assume(len(unique_rules) >= 1)

    mock_store = _mock_healthlake_store()
    _invoke_generate(unique_rules, trial_id, mock_store=mock_store)

    mock_store.invoke.assert_called_once()
    stored_resource = mock_store.invoke.call_args[0][0]["resource"]
    assert stored_resource["resourceType"] == "Questionnaire"
    # The stored resource must carry the trial identifier
    stored_ids = [
        ident["value"] for ident in stored_resource.get("identifier", [])
        if ident.get("system") == "https://clinicaltrials.gov"
    ]
    assert trial_id in stored_ids


@given(rules=_eligibility_rules_st, trial_id=_trial_id_st)
@settings(max_examples=20)
def test_p3_store_failure_does_not_crash(
    rules: list[dict], trial_id: str,
):
    """If HealthLake storage fails, the function must still return a valid
    result with store_status indicating failure."""
    seen_ids: set[str] = set()
    unique_rules = []
    for r in rules:
        if r["criterion_id"] not in seen_ids:
            seen_ids.add(r["criterion_id"])
            unique_rules.append(r)
    assume(len(unique_rules) >= 1)

    mock_store = MagicMock()
    mock_store.invoke.side_effect = Exception("HealthLake unavailable")

    result = _invoke_generate(unique_rules, trial_id, mock_store=mock_store)

    assert result["questionnaire"] is not None, "Must still return questionnaire"
    assert result["store_status"] == "failed"


@given(rules=_eligibility_rules_st, trial_id=_trial_id_st)
@settings(max_examples=30)
def test_p3_questionnaire_title_contains_trial_id(
    rules: list[dict], trial_id: str,
):
    """The Questionnaire title must reference the trial ID for traceability."""
    seen_ids: set[str] = set()
    unique_rules = []
    for r in rules:
        if r["criterion_id"] not in seen_ids:
            seen_ids.add(r["criterion_id"])
            unique_rules.append(r)
    assume(len(unique_rules) >= 1)

    result = _invoke_generate(unique_rules, trial_id)
    q = result["questionnaire"]

    assert trial_id in q.get("title", ""), (
        f"Questionnaire title must contain trial ID {trial_id}"
    )


# ===================================================================
# Property 31 — ClinicalTrials.gov Baseline Integration
# Questionnaire incorporates ClinicalTrials.gov criteria as baseline.
# Validates: Requirements 12.1, 12.2
# ===================================================================


_ctg_eligibility_text_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
    min_size=20,
    max_size=200,
)


def _make_ctg_api_response(
    nct_number: str,
    title: str = "Test Trial",
    phase: str = "PHASE3",
    status: str = "RECRUITING",
    conditions: list[str] | None = None,
    interventions: list[str] | None = None,
    eligibility_criteria: str = "Inclusion: Age 18+\nExclusion: Pregnant",
):
    """Build a mock ClinicalTrials.gov v2 API JSON response."""
    return {
        "protocolSection": {
            "identificationModule": {
                "nctId": nct_number,
                "officialTitle": title,
                "briefTitle": title,
            },
            "statusModule": {"overallStatus": status},
            "designModule": {"phases": [phase]},
            "conditionsModule": {"conditions": conditions or ["Diabetes"]},
            "armsInterventionsModule": {
                "interventions": [
                    {"name": name} for name in (interventions or ["Drug A"])
                ],
            },
            "eligibilityModule": {
                "eligibilityCriteria": eligibility_criteria,
            },
        },
    }


@given(trial_id=_trial_id_st)
@settings(max_examples=20)
def test_p31_ctg_query_returns_metadata(trial_id: str):
    """clinicaltrials_gov_query must return title, phase, status, conditions,
    interventions, and eligibility_criteria from the API response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = _make_ctg_api_response(trial_id)

    mock_table = MagicMock()
    mock_table.put_item.return_value = None
    mock_audit = MagicMock()
    mock_audit.invoke.return_value = None

    with (
        patch("components.protocol_engine.requests.get", return_value=mock_resp),
        patch("components.protocol_engine._protocol_table", mock_table),
        patch("components.protocol_engine.audit_log_event", mock_audit),
    ):
        result = clinicaltrials_gov_query.invoke({"nct_number": trial_id})

    assert result["nct_number"] == trial_id
    assert result["title"] == "Test Trial"
    assert result["phase"] == "PHASE3"
    assert result["overall_status"] == "RECRUITING"
    assert "Diabetes" in result["conditions"]
    assert "Drug A" in result["interventions"]
    assert result["eligibility_criteria"], "Must return eligibility criteria text"
    assert result["source"] == "clinicaltrials.gov"


@given(trial_id=_trial_id_st)
@settings(max_examples=20)
def test_p31_ctg_query_stores_config(trial_id: str):
    """clinicaltrials_gov_query must store the retrieved metadata in
    TrialProtocolConfig DynamoDB table."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = _make_ctg_api_response(trial_id)

    mock_table = MagicMock()
    mock_table.put_item.return_value = None
    mock_audit = MagicMock()
    mock_audit.invoke.return_value = None

    with (
        patch("components.protocol_engine.requests.get", return_value=mock_resp),
        patch("components.protocol_engine._protocol_table", mock_table),
        patch("components.protocol_engine.audit_log_event", mock_audit),
    ):
        clinicaltrials_gov_query.invoke({"nct_number": trial_id})

    mock_table.put_item.assert_called_once()
    stored_item = mock_table.put_item.call_args[1]["Item"]
    assert stored_item["trial_id"] == trial_id
    assert "nct_metadata" in stored_item
    assert stored_item["nct_metadata"]["nct_number"] == trial_id


@given(trial_id=_trial_id_st, eligibility_text=_ctg_eligibility_text_st)
@settings(max_examples=20)
def test_p31_ctg_eligibility_criteria_passed_through(
    trial_id: str, eligibility_text: str,
):
    """The eligibility criteria text from ClinicalTrials.gov must be returned
    verbatim so it can be used as baseline for Questionnaire generation."""
    assume(len(eligibility_text.strip()) > 0)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = _make_ctg_api_response(
        trial_id, eligibility_criteria=eligibility_text,
    )

    mock_table = MagicMock()
    mock_table.put_item.return_value = None
    mock_audit = MagicMock()
    mock_audit.invoke.return_value = None

    with (
        patch("components.protocol_engine.requests.get", return_value=mock_resp),
        patch("components.protocol_engine._protocol_table", mock_table),
        patch("components.protocol_engine.audit_log_event", mock_audit),
    ):
        result = clinicaltrials_gov_query.invoke({"nct_number": trial_id})

    assert result["eligibility_criteria"] == eligibility_text


@given(trial_id=_trial_id_st)
@settings(max_examples=20)
def test_p31_ctg_graceful_degradation_on_api_failure(trial_id: str):
    """When ClinicalTrials.gov API fails, the system must fall back to local
    cache and log the failure."""
    # Set up a cached entry in DynamoDB
    cached_meta = {
        "nct_number": trial_id,
        "title": "Cached Trial",
        "phase": "PHASE2",
        "overall_status": "ACTIVE_NOT_RECRUITING",
        "conditions": ["Hypertension"],
        "interventions": ["Drug B"],
        "eligibility_criteria": "Cached criteria text",
    }

    mock_table = MagicMock()
    mock_table.query.return_value = {
        "Items": [{"nct_metadata": cached_meta, "version": "20260101T000000Z"}],
    }
    mock_audit = MagicMock()
    mock_audit.invoke.return_value = None

    with (
        patch(
            "components.protocol_engine.requests.get",
            side_effect=Exception("API timeout"),
        ),
        patch("components.protocol_engine._protocol_table", mock_table),
        patch("components.protocol_engine.audit_log_event", mock_audit),
    ):
        result = clinicaltrials_gov_query.invoke({"nct_number": trial_id})

    assert result["source"] == "local_cache"
    assert result["title"] == "Cached Trial"
    assert result["eligibility_criteria"] == "Cached criteria text"

    # Audit must log the API failure
    mock_audit.invoke.assert_called_once()
    audit_args = mock_audit.invoke.call_args[0][0]
    assert audit_args["event_type"] == "data_access"
    assert "failed" in audit_args["event_data"]["action"]


@given(trial_id=_trial_id_st)
@settings(max_examples=10)
def test_p31_ctg_unavailable_with_no_cache(trial_id: str):
    """When both the API and local cache fail, the result must indicate
    unavailability with an error message."""
    mock_table = MagicMock()
    mock_table.query.side_effect = Exception("DynamoDB unavailable")
    mock_audit = MagicMock()
    mock_audit.invoke.return_value = None

    with (
        patch(
            "components.protocol_engine.requests.get",
            side_effect=Exception("API timeout"),
        ),
        patch("components.protocol_engine._protocol_table", mock_table),
        patch("components.protocol_engine.audit_log_event", mock_audit),
    ):
        result = clinicaltrials_gov_query.invoke({"nct_number": trial_id})

    assert result["source"] == "unavailable"
    assert "error" in result
    assert result["nct_number"] == trial_id
