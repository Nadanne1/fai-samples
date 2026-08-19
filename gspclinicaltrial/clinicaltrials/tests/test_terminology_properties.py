"""Property-based tests for the Terminology Service.

Properties tested:
  P4  — Terminology Mapping Coverage: at least one mapping returned per
        known term/system pair; terms below 0.8 confidence flagged for review.
  P32 — Terminology Version Selection: trial-specific terminology versions
        used when configured, not defaults.

Validates Requirements: 1.3, 13.1–13.6
"""

from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
from hypothesis import given, settings

from components.terminology import (
    CONFIDENCE_THRESHOLD,
    DEFAULT_TERMINOLOGY_VERSIONS,
    TERMINOLOGY_SYSTEMS,
    _CONDITION_ICD10,
    _CONDITION_SNOMED,
    _MEDICATION_RXNORM,
    _MEDICATION_WHO_DRUG,
    _AE_MEDDRA,
    _LAB_LOINC,
    terminology_map,
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Known terms that exist in the curated lookup tables, paired with their
# correct source_system hint and the target systems they should map to.
_KNOWN_CONDITION_TERMS = list(_CONDITION_SNOMED.keys())
_KNOWN_MEDICATION_TERMS = list(_MEDICATION_RXNORM.keys())
_KNOWN_AE_TERMS = list(_AE_MEDDRA.keys())
_KNOWN_LAB_TERMS = list(_LAB_LOINC.keys())

_condition_st = st.sampled_from(_KNOWN_CONDITION_TERMS)
_medication_st = st.sampled_from(_KNOWN_MEDICATION_TERMS)
_ae_st = st.sampled_from(_KNOWN_AE_TERMS)
_lab_st = st.sampled_from(_KNOWN_LAB_TERMS)

_trial_id_st = st.from_regex(r"NCT[0-9]{8}", fullmatch=True)

# Strategy for completely unknown terms that won't match any lookup
_unknown_term_st = st.from_regex(r"zzz_unknown_[a-z]{5}", fullmatch=True)

# Strategy for valid target system keys
_target_system_st = st.sampled_from(list(TERMINOLOGY_SYSTEMS.keys()))


# ---------------------------------------------------------------------------
# Helpers — mock AWS clients used by terminology module
# ---------------------------------------------------------------------------

def _make_mock_protocol_table(terminology_versions=None):
    """Return a mock DynamoDB table for TrialProtocolConfig."""
    table = MagicMock()
    if terminology_versions:
        table.query.return_value = {
            "Items": [{"terminology_versions": terminology_versions}]
        }
    else:
        table.query.return_value = {"Items": []}
    return table


def _make_mock_audit():
    """Return a mock audit_log_event tool."""
    mock = MagicMock()
    mock.invoke = MagicMock(return_value={"audit_id": "mock", "status": "recorded"})
    return mock


def _invoke_terminology(term, source_system, target_systems, trial_id=None,
                        protocol_table=None, mock_audit=None):
    """Invoke terminology_map with mocked AWS dependencies."""
    protocol_table = protocol_table or _make_mock_protocol_table()
    mock_audit = mock_audit or _make_mock_audit()

    with (
        patch("components.terminology._protocol_table", protocol_table),
        patch("components.terminology.audit_log_event", mock_audit),
    ):
        result = terminology_map.invoke({
            "term": term,
            "source_system": source_system,
            "target_systems": target_systems,
            "trial_id": trial_id,
        })

    return result, mock_audit


# ===================================================================
# Feature: clinical-trial-screening, Property 4: Terminology Mapping Coverage
#
# For any known clinical term and matching target system, at least one
# mapping must be returned with a confidence score. Terms with best
# confidence below 0.8 must be flagged for manual review and logged.
# Validates: Requirements 1.3, 13.1, 13.2, 13.3, 13.4, 13.5
# ===================================================================

@given(term=_condition_st)
@settings(max_examples=50)
def test_p4_condition_maps_to_snomed(term: str):
    """Every known condition term must produce at least one SNOMED CT mapping."""
    result, _ = _invoke_terminology(term, "condition", ["snomed"])

    assert len(result["mappings"]) >= 1, (
        f"No SNOMED mapping for known condition '{term}'"
    )
    mapping = result["mappings"][0]
    assert mapping["system"] == TERMINOLOGY_SYSTEMS["snomed"]["uri"]
    assert "code" in mapping
    assert "confidence" in mapping
    assert isinstance(mapping["confidence"], (int, float))


@given(term=_condition_st)
@settings(max_examples=50)
def test_p4_condition_maps_to_icd10(term: str):
    """Every known condition term must produce at least one ICD-10 mapping."""
    result, _ = _invoke_terminology(term, "condition", ["icd10"])

    assert len(result["mappings"]) >= 1, (
        f"No ICD-10 mapping for known condition '{term}'"
    )
    mapping = result["mappings"][0]
    assert mapping["system"] == TERMINOLOGY_SYSTEMS["icd10"]["uri"]
    assert "code" in mapping


@given(term=_medication_st)
@settings(max_examples=50)
def test_p4_medication_maps_to_rxnorm(term: str):
    """Every known medication term must produce at least one RxNorm mapping."""
    result, _ = _invoke_terminology(term, "medication", ["rxnorm"])

    assert len(result["mappings"]) >= 1, (
        f"No RxNorm mapping for known medication '{term}'"
    )
    mapping = result["mappings"][0]
    assert mapping["system"] == TERMINOLOGY_SYSTEMS["rxnorm"]["uri"]


@given(term=_medication_st)
@settings(max_examples=50)
def test_p4_medication_maps_to_who_drug(term: str):
    """Every known medication term must produce at least one WHO Drug mapping."""
    result, _ = _invoke_terminology(term, "medication", ["who_drug"])

    assert len(result["mappings"]) >= 1, (
        f"No WHO Drug mapping for known medication '{term}'"
    )
    mapping = result["mappings"][0]
    assert mapping["system"] == TERMINOLOGY_SYSTEMS["who_drug"]["uri"]


@given(term=_ae_st)
@settings(max_examples=50)
def test_p4_adverse_event_maps_to_meddra(term: str):
    """Every known AE term must produce at least one MedDRA mapping."""
    result, _ = _invoke_terminology(term, "adverse_event", ["meddra"])

    assert len(result["mappings"]) >= 1, (
        f"No MedDRA mapping for known AE '{term}'"
    )
    mapping = result["mappings"][0]
    assert mapping["system"] == TERMINOLOGY_SYSTEMS["meddra"]["uri"]
    # MedDRA mappings should include SOC
    assert "soc_code" in mapping
    assert "soc_display" in mapping


@given(term=_lab_st)
@settings(max_examples=50)
def test_p4_lab_maps_to_loinc(term: str):
    """Every known lab term must produce at least one LOINC mapping."""
    result, _ = _invoke_terminology(term, "lab", ["loinc"])

    assert len(result["mappings"]) >= 1, (
        f"No LOINC mapping for known lab '{term}'"
    )
    mapping = result["mappings"][0]
    assert mapping["system"] == TERMINOLOGY_SYSTEMS["loinc"]["uri"]


@given(term=_unknown_term_st, target=_target_system_st)
@settings(max_examples=50)
def test_p4_unknown_term_flagged_for_review(term: str, target: str):
    """Unknown terms must be flagged for review (best_confidence < 0.8)
    and the flagging must be logged via audit_log_event."""
    mock_audit = _make_mock_audit()
    result, _ = _invoke_terminology(
        term, "condition", [target], trial_id="NCT00000001",
        mock_audit=mock_audit,
    )

    # Unknown terms should have no mappings or very low confidence
    assert result["flagged_for_review"] is True, (
        f"Unknown term '{term}' was not flagged for review"
    )
    assert result["best_confidence"] < CONFIDENCE_THRESHOLD

    # Audit logger must have been called for the flagged term
    mock_audit.invoke.assert_called_once()
    audit_call = mock_audit.invoke.call_args[0][0]
    assert audit_call["event_type"] == "validation_performed"
    assert audit_call["event_data"]["action"] == "terminology_mapping_flagged"
    assert audit_call["event_data"]["term"] == term


@given(
    term=_condition_st,
    target=st.sampled_from(["snomed", "icd10"]),
)
@settings(max_examples=50)
def test_p4_high_confidence_not_flagged(term: str, target: str):
    """Known terms with confidence >= 0.8 must NOT be flagged for review."""
    mock_audit = _make_mock_audit()
    result, _ = _invoke_terminology(
        term, "condition", [target],
        mock_audit=mock_audit,
    )

    assert result["best_confidence"] >= CONFIDENCE_THRESHOLD
    assert result["flagged_for_review"] is False
    # Audit logger should NOT be called for high-confidence mappings
    mock_audit.invoke.assert_not_called()


@given(term=_condition_st)
@settings(max_examples=30)
def test_p4_multi_target_returns_mapping_per_system(term: str):
    """When multiple target systems are requested, each should produce a
    mapping for known terms."""
    result, _ = _invoke_terminology(term, "condition", ["snomed", "icd10"])

    systems_returned = {m["system"] for m in result["mappings"]}
    assert TERMINOLOGY_SYSTEMS["snomed"]["uri"] in systems_returned
    assert TERMINOLOGY_SYSTEMS["icd10"]["uri"] in systems_returned


# ===================================================================
# Feature: clinical-trial-screening, Property 32: Terminology Version Selection
#
# For any trial with configured terminology versions, the service must
# use the specified version, not the default.
# Validates: Requirements 13.6
# ===================================================================

@given(term=_condition_st)
@settings(max_examples=30)
def test_p32_trial_specific_versions_used(term: str):
    """When a trial has custom terminology versions configured, those
    versions must appear in the mapping results, not the defaults."""
    custom_versions = {
        "snomed": "2025-01-CUSTOM",
        "icd10": "2025-CUSTOM",
        "meddra": "27.0-CUSTOM",
        "loinc": "3.00-CUSTOM",
        "rxnorm": "2025-01-CUSTOM",
        "who_drug": "2025-Q1-CUSTOM",
    }
    mock_table = _make_mock_protocol_table(terminology_versions=custom_versions)

    result, _ = _invoke_terminology(
        term, "condition", ["snomed"],
        trial_id="NCT99999999",
        protocol_table=mock_table,
    )

    # The returned versions should reflect the custom config
    assert result["terminology_versions"]["snomed"] == "2025-01-CUSTOM"
    assert result["terminology_versions"]["icd10"] == "2025-CUSTOM"

    # The mapping itself should carry the custom version
    if result["mappings"]:
        assert result["mappings"][0]["version"] == "2025-01-CUSTOM"

    # Verify the protocol table was queried with the trial_id
    mock_table.query.assert_called_once()


@given(term=_condition_st)
@settings(max_examples=30)
def test_p32_default_versions_when_no_trial_config(term: str):
    """When no trial-specific config exists, default versions must be used."""
    mock_table = _make_mock_protocol_table(terminology_versions=None)

    result, _ = _invoke_terminology(
        term, "condition", ["snomed"],
        trial_id="NCT00000000",
        protocol_table=mock_table,
    )

    assert result["terminology_versions"]["snomed"] == DEFAULT_TERMINOLOGY_VERSIONS["snomed"]
    if result["mappings"]:
        assert result["mappings"][0]["version"] == DEFAULT_TERMINOLOGY_VERSIONS["snomed"]


@given(term=_condition_st)
@settings(max_examples=30)
def test_p32_partial_version_override_merges_with_defaults(term: str):
    """When trial config only overrides some systems, the rest should
    fall back to defaults."""
    partial_versions = {"snomed": "2099-OVERRIDE"}
    mock_table = _make_mock_protocol_table(terminology_versions=partial_versions)

    result, _ = _invoke_terminology(
        term, "condition", ["snomed", "icd10"],
        trial_id="NCT11111111",
        protocol_table=mock_table,
    )

    # Overridden system uses custom version
    assert result["terminology_versions"]["snomed"] == "2099-OVERRIDE"
    # Non-overridden system uses default
    assert result["terminology_versions"]["icd10"] == DEFAULT_TERMINOLOGY_VERSIONS["icd10"]


@given(term=_condition_st)
@settings(max_examples=20)
def test_p32_no_trial_id_uses_defaults(term: str):
    """When trial_id is None, default versions must be used without
    querying DynamoDB."""
    mock_table = _make_mock_protocol_table()

    result, _ = _invoke_terminology(
        term, "condition", ["snomed"],
        trial_id=None,
        protocol_table=mock_table,
    )

    assert result["terminology_versions"] == DEFAULT_TERMINOLOGY_VERSIONS
    # Should NOT query DynamoDB when trial_id is None
    mock_table.query.assert_not_called()


@given(term=_condition_st)
@settings(max_examples=20)
def test_p32_dynamodb_failure_falls_back_to_defaults(term: str):
    """When DynamoDB query fails, default versions must be used."""
    mock_table = MagicMock()
    mock_table.query.side_effect = Exception("DynamoDB unavailable")

    result, _ = _invoke_terminology(
        term, "condition", ["snomed"],
        trial_id="NCT22222222",
        protocol_table=mock_table,
    )

    assert result["terminology_versions"] == DEFAULT_TERMINOLOGY_VERSIONS
    # Should still return a valid mapping
    assert len(result["mappings"]) >= 1
