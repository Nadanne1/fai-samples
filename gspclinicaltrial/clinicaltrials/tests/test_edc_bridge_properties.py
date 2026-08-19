"""Property-based tests for the EDC Bridge.

Properties tested:
  P15 — CDASH/SDTM Variable Mapping: deterministic mapping of data points
        to CDASH/SDTM variables.
  P23 — ODM-XML Export Round-Trip: export to ODM-XML and import back
        produces equivalent QuestionnaireResponse.
  P24 — EDC Transmission Retry: retry with exponential backoff, manual
        queue on final failure.
  P33 — SDTM Dataset Generation: valid SDTM datasets for DM, MH, CM, AE,
        VS with define.xml.
  P34 — ADaM Dataset Generation from SDTM: ADaM datasets (ADSL, ADAE)
        generated from SDTM with define.xml.

Validates Requirements: 5.5, 9.1–9.6, 14.1–14.5
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from components.edc_bridge import (
    _build_odm_xml,
    _collect_variable_mappings,
    _DEFAULT_CDASH_MAP,
    _extract_fhir_path_from_item,
    _extract_item_value,
    _generate_adae,
    _generate_adsl,
    _generate_define_xml,
    _import_odm_xml,
    _odm_to_string,
    _resolve_cdash_variable,
    _SDTM_DOMAIN_META,
    _ADAM_DATASET_META,
    _validate_odm_xml,
    _validate_sdtm_pinnacle21,
    _build_sdtm_record,
    _extract_sdtm_from_response,
    _transmit_with_retry,
    export_odm_xml,
    generate_sdtm_datasets,
    generate_adam_datasets,
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_trial_id_st = st.from_regex(r"NCT[0-9]{8}", fullmatch=True)
_patient_id_st = st.uuids().map(str)
_session_id_st = st.from_regex(r"scr_[0-9]{10}", fullmatch=True)
_link_id_st = st.from_regex(r"IE-[0-9]{3}", fullmatch=True)

_FHIR_PATHS = list(_DEFAULT_CDASH_MAP.keys())

_fhir_path_st = st.sampled_from(_FHIR_PATHS)

_answer_value_st = st.one_of(
    st.booleans(),
    st.integers(min_value=0, max_value=300),
    st.text(
        alphabet=st.characters(whitelist_categories=("L", "N")),
        min_size=1,
        max_size=20,
    ),
)

_SDTM_DOMAINS = ["DM", "MH", "CM", "AE", "VS"]


def _questionnaire_item_st(link_id=None, fhir_path=None):
    """Strategy for a single FHIR Questionnaire item with eligibility-rule extension."""
    lid = link_id or _link_id_st
    fp = fhir_path or _fhir_path_st

    return st.tuples(lid, fp).map(lambda t: {
        "linkId": t[0],
        "text": f"Question for {t[0]}",
        "type": "boolean",
        "required": True,
        "code": [],
        "extension": [{
            "url": "http://example.org/fhir/StructureDefinition/eligibility-rule",
            "valueString": json.dumps({
                "type": "inclusion",
                "fhir_path": t[1],
                "operator": "equals",
                "value": True,
            }),
        }],
    })


def _response_item_st(link_id=None):
    """Strategy for a single QuestionnaireResponse item with an answer."""
    lid = link_id or _link_id_st
    return st.tuples(lid, _answer_value_st).map(lambda t: _make_response_item(t[0], t[1]))


def _make_response_item(link_id: str, value) -> dict:
    """Build a QuestionnaireResponse item from a linkId and value."""
    if isinstance(value, bool):
        return {"linkId": link_id, "answer": [{"valueBoolean": value}]}
    if isinstance(value, int):
        return {"linkId": link_id, "answer": [{"valueInteger": value}]}
    return {"linkId": link_id, "answer": [{"valueString": str(value)}]}


def _paired_q_and_qr_st():
    """Strategy producing a matched (Questionnaire, QuestionnaireResponse) pair.

    Each pair has 1-6 items with consistent linkIds and FHIR paths.
    """
    return st.integers(min_size=1, max_size=6).flatmap(_build_paired)


def _build_paired(n: int):
    """Build n paired items."""
    link_ids = [f"IE-{i:03d}" for i in range(1, n + 1)]
    fhir_paths = st.lists(
        _fhir_path_st, min_size=n, max_size=n,
    )
    answer_values = st.lists(
        _answer_value_st, min_size=n, max_size=n,
    )
    return st.tuples(st.just(link_ids), fhir_paths, answer_values)


# Deterministic paired builder (non-strategy, for simpler tests)
def _make_paired(
    link_ids: list[str],
    fhir_paths: list[str],
    values: list,
) -> tuple[dict, dict]:
    """Build a matched Questionnaire + QuestionnaireResponse from explicit data."""
    q_items = []
    qr_items = []
    for lid, fp, val in zip(link_ids, fhir_paths, values):
        q_items.append({
            "linkId": lid,
            "text": f"Question for {lid}",
            "type": "boolean",
            "required": True,
            "code": [],
            "extension": [{
                "url": "http://example.org/fhir/StructureDefinition/eligibility-rule",
                "valueString": json.dumps({
                    "type": "inclusion",
                    "fhir_path": fp,
                    "operator": "equals",
                    "value": True,
                }),
            }],
        })
        qr_items.append(_make_response_item(lid, val))

    questionnaire = {
        "resourceType": "Questionnaire",
        "id": "test-q",
        "status": "active",
        "title": "Test Screening Questionnaire",
        "version": "1.0",
        "item": q_items,
    }
    questionnaire_response = {
        "resourceType": "QuestionnaireResponse",
        "questionnaire": "Questionnaire/test-q",
        "status": "completed",
        "subject": {"reference": "Patient/test-patient-001"},
        "item": qr_items,
    }
    return questionnaire, questionnaire_response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_audit():
    """Return a mock audit_log_event that succeeds."""
    mock = MagicMock()
    mock.invoke.return_value = None
    return mock


def _mock_protocol_table(cdash_mapping=None):
    """Return a mock DynamoDB protocol table."""
    mock = MagicMock()
    if cdash_mapping:
        mock.query.return_value = {"Items": [{"cdash_mapping": cdash_mapping}]}
    else:
        mock.query.return_value = {"Items": []}
    return mock



# ===================================================================
# Property 15 — CDASH/SDTM Variable Mapping
# For any collected data point in a QuestionnaireResponse, the EDC_Bridge
# should map it to the correct CDASH variable name and SDTM domain/variable
# pair. The mapping should be deterministic — the same data point always
# maps to the same CDASH/SDTM variables.
# Validates: Requirements 5.5, 9.2, 14.1
# ===================================================================


@given(fhir_path=_fhir_path_st)
@settings(max_examples=50)
def test_p15_mapping_is_deterministic(fhir_path: str):
    """Same FHIR path always resolves to the same CDASH/SDTM triple."""
    cdash_map = _DEFAULT_CDASH_MAP.copy()
    result_a = _resolve_cdash_variable(fhir_path, cdash_map)
    result_b = _resolve_cdash_variable(fhir_path, cdash_map)
    assert result_a is not None, f"No mapping for known path {fhir_path}"
    assert result_a == result_b, "Mapping must be deterministic"


@given(fhir_path=_fhir_path_st)
@settings(max_examples=50)
def test_p15_mapping_returns_three_tuple(fhir_path: str):
    """Every resolved mapping is a (cdash_var, sdtm_domain, sdtm_var) triple."""
    result = _resolve_cdash_variable(fhir_path, _DEFAULT_CDASH_MAP)
    assert result is not None
    cdash_var, sdtm_domain, sdtm_var = result
    assert isinstance(cdash_var, str) and len(cdash_var) > 0
    assert isinstance(sdtm_domain, str) and len(sdtm_domain) > 0
    assert isinstance(sdtm_var, str) and len(sdtm_var) > 0


@given(fhir_path=_fhir_path_st)
@settings(max_examples=50)
def test_p15_sdtm_domain_is_valid(fhir_path: str):
    """SDTM domain from mapping is a recognized CDISC domain."""
    valid_domains = {"DM", "MH", "CM", "AE", "VS", "IE"}
    result = _resolve_cdash_variable(fhir_path, _DEFAULT_CDASH_MAP)
    assert result is not None
    _, sdtm_domain, _ = result
    assert sdtm_domain in valid_domains, f"Unknown SDTM domain: {sdtm_domain}"


@given(
    link_ids=st.lists(
        st.from_regex(r"IE-[0-9]{3}", fullmatch=True),
        min_size=1, max_size=5, unique=True,
    ),
    fhir_paths=st.lists(_fhir_path_st, min_size=1, max_size=5),
    values=st.lists(_answer_value_st, min_size=1, max_size=5),
)
@settings(max_examples=30)
def test_p15_collect_variable_mappings_deterministic(
    link_ids: list[str], fhir_paths: list[str], values: list,
):
    """_collect_variable_mappings produces identical output on repeated calls."""
    n = min(len(link_ids), len(fhir_paths), len(values))
    assume(n >= 1)
    q, qr = _make_paired(link_ids[:n], fhir_paths[:n], values[:n])
    cdash_map = _DEFAULT_CDASH_MAP.copy()

    mappings_a = _collect_variable_mappings(qr, q, cdash_map)
    mappings_b = _collect_variable_mappings(qr, q, cdash_map)
    assert mappings_a == mappings_b, "Variable mappings must be deterministic"


@given(
    link_ids=st.lists(
        st.from_regex(r"IE-[0-9]{3}", fullmatch=True),
        min_size=1, max_size=5, unique=True,
    ),
    fhir_paths=st.lists(_fhir_path_st, min_size=1, max_size=5),
    values=st.lists(_answer_value_st, min_size=1, max_size=5),
)
@settings(max_examples=30)
def test_p15_every_mapping_has_required_fields(
    link_ids: list[str], fhir_paths: list[str], values: list,
):
    """Each mapping entry contains linkId, fhir_path, cdash_variable,
    sdtm_domain, sdtm_variable, and value."""
    n = min(len(link_ids), len(fhir_paths), len(values))
    assume(n >= 1)
    q, qr = _make_paired(link_ids[:n], fhir_paths[:n], values[:n])
    cdash_map = _DEFAULT_CDASH_MAP.copy()

    mappings = _collect_variable_mappings(qr, q, cdash_map)
    required_keys = {"linkId", "fhir_path", "cdash_variable", "sdtm_domain", "sdtm_variable", "value"}
    for m in mappings:
        assert required_keys.issubset(m.keys()), f"Missing keys: {required_keys - m.keys()}"



# ===================================================================
# Property 23 — ODM-XML Export Round-Trip
# For all valid QuestionnaireResponse resources, exporting to ODM-XML
# then importing back should produce an equivalent QuestionnaireResponse
# (same patient, same item values).
# Validates: Requirements 9.1, 9.6
# ===================================================================


@given(
    link_ids=st.lists(
        st.from_regex(r"IE-[0-9]{3}", fullmatch=True),
        min_size=1, max_size=5, unique=True,
    ),
    fhir_paths=st.lists(_fhir_path_st, min_size=1, max_size=5),
    values=st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=("L", "N")),
            min_size=1, max_size=15,
        ),
        min_size=1, max_size=5,
    ),
    trial_id=_trial_id_st,
)
@settings(max_examples=30)
def test_p23_round_trip_preserves_patient_id(
    link_ids: list[str],
    fhir_paths: list[str],
    values: list[str],
    trial_id: str,
):
    """Patient ID survives ODM-XML export → import round-trip."""
    n = min(len(link_ids), len(fhir_paths), len(values))
    assume(n >= 1)
    q, qr = _make_paired(link_ids[:n], fhir_paths[:n], values[:n])
    cdash_map = _DEFAULT_CDASH_MAP.copy()

    odm_element = _build_odm_xml(qr, q, trial_id, cdash_map)
    odm_xml = _odm_to_string(odm_element)
    imported = _import_odm_xml(odm_xml)

    original_patient = qr["subject"]["reference"]
    imported_patient = imported.get("subject", {}).get("reference", "")
    assert original_patient == imported_patient, (
        f"Patient mismatch: {original_patient} vs {imported_patient}"
    )


@given(
    link_ids=st.lists(
        st.from_regex(r"IE-[0-9]{3}", fullmatch=True),
        min_size=1, max_size=5, unique=True,
    ),
    fhir_paths=st.lists(_fhir_path_st, min_size=1, max_size=5),
    values=st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=("L", "N")),
            min_size=1, max_size=15,
        ),
        min_size=1, max_size=5,
    ),
    trial_id=_trial_id_st,
)
@settings(max_examples=30)
def test_p23_round_trip_preserves_item_count(
    link_ids: list[str],
    fhir_paths: list[str],
    values: list[str],
    trial_id: str,
):
    """Number of items with values survives the round-trip."""
    n = min(len(link_ids), len(fhir_paths), len(values))
    assume(n >= 1)
    q, qr = _make_paired(link_ids[:n], fhir_paths[:n], values[:n])
    cdash_map = _DEFAULT_CDASH_MAP.copy()

    # Count items that actually map to a CDASH variable (only those appear in ODM)
    mappings = _collect_variable_mappings(qr, q, cdash_map)
    expected_count = len(mappings)

    odm_element = _build_odm_xml(qr, q, trial_id, cdash_map)
    odm_xml = _odm_to_string(odm_element)
    imported = _import_odm_xml(odm_xml)

    assert len(imported.get("item", [])) == expected_count


@given(
    link_ids=st.lists(
        st.from_regex(r"IE-[0-9]{3}", fullmatch=True),
        min_size=1, max_size=5, unique=True,
    ),
    fhir_paths=st.lists(_fhir_path_st, min_size=1, max_size=5),
    values=st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=("L", "N")),
            min_size=1, max_size=15,
        ),
        min_size=1, max_size=5,
    ),
    trial_id=_trial_id_st,
)
@settings(max_examples=30)
def test_p23_round_trip_preserves_item_values(
    link_ids: list[str],
    fhir_paths: list[str],
    values: list[str],
    trial_id: str,
):
    """Each item value survives the round-trip (as string representation)."""
    n = min(len(link_ids), len(fhir_paths), len(values))
    assume(n >= 1)
    q, qr = _make_paired(link_ids[:n], fhir_paths[:n], values[:n])
    cdash_map = _DEFAULT_CDASH_MAP.copy()

    odm_element = _build_odm_xml(qr, q, trial_id, cdash_map)
    odm_xml = _odm_to_string(odm_element)
    imported = _import_odm_xml(odm_xml)

    imported_values = {
        item["answer"][0]["valueString"]
        for item in imported.get("item", [])
        if item.get("answer")
    }

    # Original values that were mapped
    mappings = _collect_variable_mappings(qr, q, cdash_map)
    original_values = {m["value"] for m in mappings}

    assert original_values == imported_values, (
        f"Value mismatch: original={original_values}, imported={imported_values}"
    )


@given(
    link_ids=st.lists(
        st.from_regex(r"IE-[0-9]{3}", fullmatch=True),
        min_size=1, max_size=4, unique=True,
    ),
    fhir_paths=st.lists(_fhir_path_st, min_size=1, max_size=4),
    values=st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=("L", "N")),
            min_size=1, max_size=15,
        ),
        min_size=1, max_size=4,
    ),
    trial_id=_trial_id_st,
)
@settings(max_examples=30)
def test_p23_odm_xml_validates_after_export(
    link_ids: list[str],
    fhir_paths: list[str],
    values: list[str],
    trial_id: str,
):
    """Exported ODM-XML passes structural validation."""
    n = min(len(link_ids), len(fhir_paths), len(values))
    assume(n >= 1)
    q, qr = _make_paired(link_ids[:n], fhir_paths[:n], values[:n])
    cdash_map = _DEFAULT_CDASH_MAP.copy()

    odm_element = _build_odm_xml(qr, q, trial_id, cdash_map)
    errors = _validate_odm_xml(odm_element)
    assert errors == [], f"ODM-XML validation errors: {errors}"


@given(trial_id=_trial_id_st)
@settings(max_examples=20)
def test_p23_round_trip_preserves_study_oid(trial_id: str):
    """StudyOID (trial_id) survives the round-trip."""
    q, qr = _make_paired(
        ["IE-001"], ["Patient.birthDate"], ["test-value"],
    )
    cdash_map = _DEFAULT_CDASH_MAP.copy()

    odm_element = _build_odm_xml(qr, q, trial_id, cdash_map)
    odm_xml = _odm_to_string(odm_element)
    imported = _import_odm_xml(odm_xml)

    assert imported.get("_source_study") == trial_id



# ===================================================================
# Property 24 — EDC Transmission Retry
# Verify retry with exponential backoff up to 3 attempts, manual queue
# on final failure.
# Validates: Requirements 9.4, 9.5
# ===================================================================


@given(trial_id=_trial_id_st, session_id=_session_id_st)
@settings(max_examples=20)
def test_p24_successful_transmission_on_first_attempt(
    trial_id: str, session_id: str,
):
    """Successful first attempt returns transmitted status without retries."""
    mock_audit = _mock_audit()
    transmit_mock = MagicMock(return_value={
        "edc_system": "medidata_rave",
        "status": "transmitted",
        "http_status": 200,
        "response_text": "OK",
    })

    with (
        patch("components.edc_bridge._transmit_to_medidata_rave", transmit_mock),
        patch("components.edc_bridge.audit_log_event", mock_audit),
    ):
        result = _transmit_with_retry(
            "<ODM/>", "medidata_rave", "https://example.com/api",
            trial_id, session_id,
        )

    assert result["status"] == "transmitted"
    assert transmit_mock.call_count == 1


@given(trial_id=_trial_id_st, session_id=_session_id_st)
@settings(max_examples=20)
def test_p24_retries_on_failure_then_succeeds(
    trial_id: str, session_id: str,
):
    """Transient failures are retried; success on later attempt is returned."""
    mock_audit = _mock_audit()
    transmit_mock = MagicMock(
        side_effect=[
            Exception("Connection timeout"),
            Exception("Server error"),
            {
                "edc_system": "medidata_rave",
                "status": "transmitted",
                "http_status": 200,
                "response_text": "OK",
            },
        ]
    )

    with (
        patch("components.edc_bridge._transmit_to_medidata_rave", transmit_mock),
        patch("components.edc_bridge.audit_log_event", mock_audit),
        patch("components.edc_bridge.time.sleep"),  # skip actual sleep
    ):
        result = _transmit_with_retry(
            "<ODM/>", "medidata_rave", "https://example.com/api",
            trial_id, session_id,
        )

    assert result["status"] == "transmitted"
    assert transmit_mock.call_count == 3


@given(trial_id=_trial_id_st, session_id=_session_id_st)
@settings(max_examples=20)
def test_p24_all_retries_exhausted_queues_for_manual_review(
    trial_id: str, session_id: str,
):
    """After 3 failed attempts, result indicates manual review queue."""
    mock_audit = _mock_audit()
    transmit_mock = MagicMock(side_effect=Exception("Persistent failure"))

    with (
        patch("components.edc_bridge._transmit_to_medidata_rave", transmit_mock),
        patch("components.edc_bridge.audit_log_event", mock_audit),
        patch("components.edc_bridge.time.sleep"),
    ):
        result = _transmit_with_retry(
            "<ODM/>", "medidata_rave", "https://example.com/api",
            trial_id, session_id,
        )

    assert result["status"] == "failed"
    assert result["queued_for_manual_review"] is True
    assert transmit_mock.call_count == 3


@given(trial_id=_trial_id_st, session_id=_session_id_st)
@settings(max_examples=20)
def test_p24_failure_logged_in_audit(
    trial_id: str, session_id: str,
):
    """Final failure is logged via audit_log_event with failure details."""
    mock_audit = _mock_audit()
    transmit_mock = MagicMock(side_effect=Exception("Network error"))

    with (
        patch("components.edc_bridge._transmit_to_medidata_rave", transmit_mock),
        patch("components.edc_bridge.audit_log_event", mock_audit),
        patch("components.edc_bridge.time.sleep"),
    ):
        _transmit_with_retry(
            "<ODM/>", "medidata_rave", "https://example.com/api",
            trial_id, session_id,
        )

    # Should have been called at least once for the failure log
    assert mock_audit.invoke.call_count >= 1
    last_call_args = mock_audit.invoke.call_args[0][0]
    assert last_call_args["event_data"]["status"] == "failed"
    assert last_call_args["event_data"]["queued_for_manual_review"] is True


@given(
    trial_id=_trial_id_st,
    session_id=_session_id_st,
    fail_attempt=st.integers(min_value=0, max_value=1),
)
@settings(max_examples=20)
def test_p24_exponential_backoff_sleep_called(
    trial_id: str, session_id: str, fail_attempt: int,
):
    """time.sleep is called with the correct backoff duration between retries."""
    mock_audit = _mock_audit()
    # Fail `fail_attempt + 1` times, then succeed
    effects: list = [Exception("fail")] * (fail_attempt + 1)
    effects.append({
        "edc_system": "medidata_rave",
        "status": "transmitted",
        "http_status": 200,
        "response_text": "OK",
    })
    transmit_mock = MagicMock(side_effect=effects)
    sleep_mock = MagicMock()

    with (
        patch("components.edc_bridge._transmit_to_medidata_rave", transmit_mock),
        patch("components.edc_bridge.audit_log_event", mock_audit),
        patch("components.edc_bridge.time.sleep", sleep_mock),
    ):
        _transmit_with_retry(
            "<ODM/>", "medidata_rave", "https://example.com/api",
            trial_id, session_id,
        )

    expected_backoffs = [5, 15, 45]
    assert sleep_mock.call_count == fail_attempt + 1
    for i in range(fail_attempt + 1):
        assert sleep_mock.call_args_list[i][0][0] == expected_backoffs[i]


@given(trial_id=_trial_id_st, session_id=_session_id_st)
@settings(max_examples=10)
def test_p24_oracle_clinical_one_retry_path(
    trial_id: str, session_id: str,
):
    """Oracle Clinical One EDC system uses the correct transmit function."""
    mock_audit = _mock_audit()
    transmit_mock = MagicMock(return_value={
        "edc_system": "oracle_clinical_one",
        "status": "transmitted",
        "http_status": 200,
        "response_text": "OK",
    })

    with (
        patch("components.edc_bridge._transmit_to_oracle_clinical_one", transmit_mock),
        patch("components.edc_bridge.audit_log_event", mock_audit),
    ):
        result = _transmit_with_retry(
            "<ODM/>", "oracle_clinical_one", "https://example.com/api",
            trial_id, session_id,
        )

    assert result["status"] == "transmitted"
    assert transmit_mock.call_count == 1



# ===================================================================
# Property 33 — SDTM Dataset Generation
# Verify valid SDTM datasets for DM, MH, CM, AE, VS with define.xml.
# Validates: Requirements 14.1, 14.2, 14.4, 14.5
# ===================================================================


@given(
    trial_id=_trial_id_st,
    subject_id=st.from_regex(r"[a-f0-9]{8}", fullmatch=True),
)
@settings(max_examples=30)
def test_p33_sdtm_record_has_required_key_variables(
    trial_id: str, subject_id: str,
):
    """Every SDTM record contains STUDYID, DOMAIN, USUBJID, and SEQ."""
    for domain in _SDTM_DOMAINS:
        record = _build_sdtm_record(
            domain, trial_id, subject_id, 1, {"TESTVAR": "val"},
        )
        assert record["STUDYID"] == trial_id
        assert record["DOMAIN"] == domain
        assert record["USUBJID"] == f"{trial_id}-{subject_id}"
        assert record[f"{domain}SEQ"] == 1


@given(trial_id=_trial_id_st)
@settings(max_examples=20)
def test_p33_sdtm_pinnacle21_passes_for_valid_records(trial_id: str):
    """Valid SDTM records pass Pinnacle 21 validation with no issues."""
    for domain in _SDTM_DOMAINS:
        meta = _SDTM_DOMAIN_META[domain]
        record = {
            "STUDYID": trial_id,
            "DOMAIN": domain,
            "USUBJID": f"{trial_id}-subj001",
            f"{domain}SEQ": 1,
        }
        issues = _validate_sdtm_pinnacle21([record], domain)
        assert issues == [], f"Unexpected issues for {domain}: {issues}"


@given(trial_id=_trial_id_st)
@settings(max_examples=20)
def test_p33_sdtm_pinnacle21_detects_missing_studyid(trial_id: str):
    """Pinnacle 21 flags records missing STUDYID."""
    record = {
        "DOMAIN": "DM",
        "USUBJID": f"{trial_id}-subj001",
        "DMSEQ": 1,
    }
    issues = _validate_sdtm_pinnacle21([record], "DM")
    assert any("STUDYID" in issue for issue in issues)


@given(trial_id=_trial_id_st)
@settings(max_examples=20)
def test_p33_sdtm_pinnacle21_detects_domain_mismatch(trial_id: str):
    """Pinnacle 21 flags records where DOMAIN value doesn't match expected."""
    record = {
        "STUDYID": trial_id,
        "DOMAIN": "XX",
        "USUBJID": f"{trial_id}-subj001",
        "DMSEQ": 1,
    }
    issues = _validate_sdtm_pinnacle21([record], "DM")
    assert any("DOMAIN" in issue for issue in issues)


@given(trial_id=_trial_id_st)
@settings(max_examples=20)
def test_p33_sdtm_pinnacle21_detects_duplicate_keys(trial_id: str):
    """Pinnacle 21 flags duplicate key combinations."""
    record = {
        "STUDYID": trial_id,
        "DOMAIN": "AE",
        "USUBJID": f"{trial_id}-subj001",
        "AESEQ": 1,
    }
    issues = _validate_sdtm_pinnacle21([record, record], "AE")
    assert any("Duplicate" in issue for issue in issues)


@given(
    trial_id=_trial_id_st,
    domains=st.lists(
        st.sampled_from(_SDTM_DOMAINS), min_size=1, max_size=5, unique=True,
    ),
)
@settings(max_examples=20)
def test_p33_define_xml_contains_all_requested_domains(
    trial_id: str, domains: list[str],
):
    """define.xml includes an ItemGroupDef for every requested domain."""
    define_xml = _generate_define_xml(trial_id, domains, "SDTM")
    root = ET.fromstring(define_xml)

    # Collect all ItemGroupDef Name attributes
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    item_groups = root.findall(f".//{ns}ItemGroupDef")
    if not item_groups:
        # Try without namespace
        item_groups = root.findall(".//ItemGroupDef")

    found_domains = {ig.get("Name") for ig in item_groups}
    for domain in domains:
        assert domain in found_domains, f"Domain {domain} missing from define.xml"


@given(trial_id=_trial_id_st)
@settings(max_examples=20)
def test_p33_define_xml_has_valid_structure(trial_id: str):
    """define.xml has ODM root with Study and MetaDataVersion."""
    define_xml = _generate_define_xml(trial_id, _SDTM_DOMAINS, "SDTM")
    root = ET.fromstring(define_xml)

    tag = root.tag
    if "}" in tag:
        tag = tag.split("}")[1]
    assert tag == "ODM"

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    study = root.find(f"{ns}Study")
    if study is None:
        study = root.find("Study")
    assert study is not None, "define.xml must contain a Study element"

    mdv = study.find(f"{ns}MetaDataVersion")
    if mdv is None:
        mdv = study.find("MetaDataVersion")
    assert mdv is not None, "define.xml must contain a MetaDataVersion element"


@given(
    link_ids=st.lists(
        st.from_regex(r"IE-[0-9]{3}", fullmatch=True),
        min_size=1, max_size=4, unique=True,
    ),
    fhir_paths=st.lists(_fhir_path_st, min_size=1, max_size=4),
    values=st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=("L", "N")),
            min_size=1, max_size=15,
        ),
        min_size=1, max_size=4,
    ),
    trial_id=_trial_id_st,
)
@settings(max_examples=20)
def test_p33_extract_sdtm_from_response_produces_valid_records(
    link_ids: list[str],
    fhir_paths: list[str],
    values: list[str],
    trial_id: str,
):
    """Records extracted from a QuestionnaireResponse pass Pinnacle 21 validation."""
    n = min(len(link_ids), len(fhir_paths), len(values))
    assume(n >= 1)
    q, qr = _make_paired(link_ids[:n], fhir_paths[:n], values[:n])
    cdash_map = _DEFAULT_CDASH_MAP.copy()

    for domain in _SDTM_DOMAINS:
        records = _extract_sdtm_from_response(qr, q, domain, trial_id, cdash_map)
        if records:
            issues = _validate_sdtm_pinnacle21(records, domain)
            assert issues == [], f"Pinnacle 21 issues for {domain}: {issues}"


@given(trial_id=_trial_id_st)
@settings(max_examples=15)
def test_p33_generate_sdtm_datasets_returns_all_domains(trial_id: str):
    """generate_sdtm_datasets returns entries for all requested domains."""
    q, qr = _make_paired(
        ["IE-001", "IE-002"],
        ["Patient.birthDate", "Condition.code"],
        ["1990-01-01", "Diabetes"],
    )
    mock_audit = _mock_audit()
    mock_table = _mock_protocol_table()

    with (
        patch("components.edc_bridge.audit_log_event", mock_audit),
        patch("components.edc_bridge._protocol_table", mock_table),
    ):
        result = generate_sdtm_datasets.invoke({
            "questionnaire_responses": [qr],
            "questionnaires": [q],
            "trial_id": trial_id,
            "domains": _SDTM_DOMAINS,
        })

    assert "datasets" in result
    for domain in _SDTM_DOMAINS:
        assert domain in result["datasets"], f"Missing domain {domain}"
    assert "define_xml" in result
    assert len(result["define_xml"]) > 0
    assert "validation_issues" in result



# ===================================================================
# Property 34 — ADaM Dataset Generation from SDTM
# Verify ADaM datasets (ADSL, ADAE) generated from SDTM with define.xml.
# Validates: Requirements 14.3, 14.5
# ===================================================================


@given(
    trial_id=_trial_id_st,
    subject_ids=st.lists(
        st.from_regex(r"[a-f0-9]{8}", fullmatch=True),
        min_size=1, max_size=3, unique=True,
    ),
)
@settings(max_examples=30)
def test_p34_adsl_one_record_per_subject(
    trial_id: str, subject_ids: list[str],
):
    """ADSL contains exactly one record per subject from DM."""
    dm_records = [
        {
            "STUDYID": trial_id,
            "DOMAIN": "DM",
            "USUBJID": f"{trial_id}-{sid}",
            "DMSEQ": i + 1,
            "SEX": "M",
            "RACE": "WHITE",
        }
        for i, sid in enumerate(subject_ids)
    ]
    adsl = _generate_adsl(dm_records, [], trial_id)
    assert len(adsl) == len(subject_ids)
    adsl_subjects = {r["USUBJID"] for r in adsl}
    expected_subjects = {f"{trial_id}-{sid}" for sid in subject_ids}
    assert adsl_subjects == expected_subjects


@given(
    trial_id=_trial_id_st,
    subject_id=st.from_regex(r"[a-f0-9]{8}", fullmatch=True),
)
@settings(max_examples=30)
def test_p34_adsl_has_required_variables(
    trial_id: str, subject_id: str,
):
    """Each ADSL record contains required ADaM variables."""
    dm = [{
        "STUDYID": trial_id,
        "DOMAIN": "DM",
        "USUBJID": f"{trial_id}-{subject_id}",
        "DMSEQ": 1,
        "SEX": "F",
        "RACE": "ASIAN",
        "ETHNIC": "NOT HISPANIC",
    }]
    adsl = _generate_adsl(dm, [], trial_id)
    assert len(adsl) == 1
    rec = adsl[0]
    for var in ("STUDYID", "USUBJID", "SUBJID", "SEX", "RACE", "SAFFL", "ITTFL"):
        assert var in rec, f"ADSL missing required variable {var}"


@given(
    trial_id=_trial_id_st,
    subject_id=st.from_regex(r"[a-f0-9]{8}", fullmatch=True),
    mh_count=st.integers(min_value=0, max_value=5),
)
@settings(max_examples=30)
def test_p34_adsl_derives_mh_count(
    trial_id: str, subject_id: str, mh_count: int,
):
    """ADSL derives medical history count from MH records for the subject."""
    usubjid = f"{trial_id}-{subject_id}"
    dm = [{"STUDYID": trial_id, "DOMAIN": "DM", "USUBJID": usubjid, "DMSEQ": 1}]
    mh = [
        {"STUDYID": trial_id, "DOMAIN": "MH", "USUBJID": usubjid, "MHSEQ": i + 1, "MHTERM": f"cond{i}"}
        for i in range(mh_count)
    ]
    adsl = _generate_adsl(dm, mh, trial_id)
    assert adsl[0]["MHCNT"] == mh_count


@given(
    trial_id=_trial_id_st,
    ae_count=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=30)
def test_p34_adae_one_record_per_ae(
    trial_id: str, ae_count: int,
):
    """ADAE contains one record per adverse event."""
    usubjid = f"{trial_id}-subj001"
    dm = [{"STUDYID": trial_id, "DOMAIN": "DM", "USUBJID": usubjid, "DMSEQ": 1, "SEX": "M"}]
    ae = [
        {
            "STUDYID": trial_id,
            "DOMAIN": "AE",
            "USUBJID": usubjid,
            "AESEQ": i + 1,
            "AETERM": f"Headache type {i}",
            "AESEV": "MILD",
            "AESER": "N",
        }
        for i in range(ae_count)
    ]
    adae = _generate_adae(ae, dm, trial_id)
    assert len(adae) == ae_count


@given(
    trial_id=_trial_id_st,
    subject_id=st.from_regex(r"[a-f0-9]{8}", fullmatch=True),
)
@settings(max_examples=30)
def test_p34_adae_merges_dm_demographics(
    trial_id: str, subject_id: str,
):
    """ADAE records include demographics (AGE, SEX, RACE) from DM."""
    usubjid = f"{trial_id}-{subject_id}"
    dm = [{
        "STUDYID": trial_id,
        "DOMAIN": "DM",
        "USUBJID": usubjid,
        "DMSEQ": 1,
        "SEX": "F",
        "RACE": "BLACK",
        "AGE": "45",
        "TRT01A": "Drug A",
        "TRT01P": "Drug A",
    }]
    ae = [{
        "STUDYID": trial_id,
        "DOMAIN": "AE",
        "USUBJID": usubjid,
        "AESEQ": 1,
        "AETERM": "Nausea",
        "AESEV": "MODERATE",
    }]
    adae = _generate_adae(ae, dm, trial_id)
    assert len(adae) == 1
    rec = adae[0]
    assert rec["SEX"] == "F"
    assert rec["RACE"] == "BLACK"
    assert rec["AGE"] == "45"
    assert rec["TRTA"] == "Drug A"


@given(
    trial_id=_trial_id_st,
    datasets=st.lists(
        st.sampled_from(["ADSL", "ADAE"]), min_size=1, max_size=2, unique=True,
    ),
)
@settings(max_examples=20)
def test_p34_adam_define_xml_contains_requested_datasets(
    trial_id: str, datasets: list[str],
):
    """ADaM define.xml includes ItemGroupDef for every requested dataset."""
    define_xml = _generate_define_xml(trial_id, datasets, "ADaM")
    root = ET.fromstring(define_xml)

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    item_groups = root.findall(f".//{ns}ItemGroupDef")
    if not item_groups:
        item_groups = root.findall(".//ItemGroupDef")

    found = {ig.get("Name") for ig in item_groups}
    for ds in datasets:
        assert ds in found, f"ADaM dataset {ds} missing from define.xml"


@given(trial_id=_trial_id_st)
@settings(max_examples=15)
def test_p34_generate_adam_datasets_returns_all_requested(trial_id: str):
    """generate_adam_datasets returns entries for all requested datasets."""
    sdtm_datasets = {
        "DM": [{
            "STUDYID": trial_id, "DOMAIN": "DM",
            "USUBJID": f"{trial_id}-subj001", "DMSEQ": 1, "SEX": "M",
        }],
        "MH": [],
        "AE": [{
            "STUDYID": trial_id, "DOMAIN": "AE",
            "USUBJID": f"{trial_id}-subj001", "AESEQ": 1, "AETERM": "Headache",
        }],
    }
    mock_audit = _mock_audit()

    with patch("components.edc_bridge.audit_log_event", mock_audit):
        result = generate_adam_datasets.invoke({
            "sdtm_datasets": sdtm_datasets,
            "trial_id": trial_id,
            "datasets": ["ADSL", "ADAE"],
        })

    assert "datasets" in result
    assert "ADSL" in result["datasets"]
    assert "ADAE" in result["datasets"]
    assert "define_xml" in result
    assert len(result["define_xml"]) > 0
    assert result["datasets"]["ADSL"], "ADSL should have at least one record"
    assert result["datasets"]["ADAE"], "ADAE should have at least one record"
