"""Property-based tests for the Safety Reporter.

Properties tested:
  P25 — E2B R3 ICSR Completeness and Validity: verify ICSR includes
        MedDRA coding, medications, and validates against schema.
  P26 — ICSR Transmission Logging: verify transmission status logged
        with ICSR ID and timestamp.
  P27 — Regulatory Reporting Timeline: verify 15-day deadline for
        serious AEs, 7-day for fatal/life-threatening.

Validates Requirements: 10.1–10.6
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, call

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from components.safety_reporter import (
    _build_icsr_xml,
    _calculate_regulatory_deadline,
    _code_ae_meddra,
    _code_medication_who,
    _map_outcome,
    _seriousness_flag,
    _validate_icsr,
    _icsr_to_string,
    _transmit_with_retry,
    generate_e2b_icsr,
    transmit_icsr,
    SERIOUS_AE_DEADLINE_DAYS,
    FATAL_LIFE_THREATENING_DEADLINE_DAYS,
    EXPEDITED_SERIOUSNESS,
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_trial_id_st = st.from_regex(r"NCT[0-9]{8}", fullmatch=True)
_patient_id_st = st.uuids().map(str)
_session_id_st = st.from_regex(r"sess_[0-9]{10}", fullmatch=True)
_ae_id_st = st.from_regex(r"AE-[0-9]{6}", fullmatch=True)

_severity_st = st.sampled_from(["mild", "moderate", "severe"])
_causality_st = st.sampled_from(["related", "possibly_related", "unlikely", "unrelated"])
_outcome_st = st.sampled_from(["recovered", "recovering", "not_recovered", "fatal", "unknown"])
_reporter_st = st.sampled_from(["patient", "investigator", "sponsor"])

_SERIOUSNESS_CRITERIA = [
    "hospitalization",
    "life-threatening",
    "death",
    "disability",
    "congenital_anomaly",
    "other",
]

_seriousness_st = st.lists(
    st.sampled_from(_SERIOUSNESS_CRITERIA), min_size=1, max_size=4, unique=True
)

_iso_date_st = st.dates(
    min_value=datetime(2020, 1, 1).date(),
    max_value=datetime(2027, 12, 31).date(),
).map(lambda d: d.isoformat())

_medication_st = st.fixed_dictionaries({
    "name": st.text(
        alphabet=st.characters(whitelist_categories=("L",)),
        min_size=3,
        max_size=20,
    ),
})

_description_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "Z")),
    min_size=5,
    max_size=80,
)


def _adverse_event_st():
    """Strategy producing a valid AdverseEvent dict."""
    return st.fixed_dictionaries({
        "ae_id": _ae_id_st,
        "patient_id": _patient_id_st,
        "trial_id": _trial_id_st,
        "session_id": _session_id_st,
        "description": _description_st,
        "onset_date": _iso_date_st,
        "severity": _severity_st,
        "seriousness": _seriousness_st,
        "causality": _causality_st,
        "outcome": _outcome_st,
        "suspect_medications": st.lists(_medication_st, min_size=1, max_size=3),
        "concomitant_medications": st.lists(_medication_st, min_size=0, max_size=3),
        "reporter": _reporter_st,
        "report_date": _iso_date_st,
    })


def _mock_terminology(term, source_system, target_systems, trial_id):
    """Fake terminology_map.invoke that returns deterministic MedDRA / WHO Drug codings."""
    if source_system == "adverse_event":
        return {
            "mappings": [{
                "display": f"PT-{term[:10]}",
                "code": "10000001",
                "soc_display": f"SOC-{term[:8]}",
                "soc_code": "20000001",
                "version": "26.0",
                "confidence": 0.95,
            }]
        }
    # medication → WHO Drug
    return {
        "mappings": [{
            "code": f"WHO-{term[:6]}",
            "display": term,
            "version": "2024-B2",
        }]
    }


def _mock_audit(**kwargs):
    """No-op audit stub."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Property 25 — E2B R3 ICSR Completeness and Validity
# ---------------------------------------------------------------------------


@given(ae=_adverse_event_st(), trial_id=_trial_id_st)
@settings(max_examples=50, deadline=None)
def test_p25_icsr_contains_meddra_coding(ae: dict, trial_id: str):
    """Generated ICSR must include MedDRA PT and version in the reaction element."""
    with (
        patch("components.safety_reporter.terminology_map") as mock_term,
        patch("components.safety_reporter.audit_log_event") as mock_audit_tool,
    ):
        mock_term.invoke = MagicMock(side_effect=lambda args: _mock_terminology(**args))
        mock_audit_tool.invoke = MagicMock(return_value={"status": "ok"})

        result = generate_e2b_icsr.invoke({"adverse_event": ae, "trial_id": trial_id})

    assert result["meddra_coding"]["pt"] != ""
    assert result["meddra_coding"]["pt_code"] != ""
    assert result["meddra_coding"]["version"] != ""


@given(ae=_adverse_event_st(), trial_id=_trial_id_st)
@settings(max_examples=50, deadline=None)
def test_p25_icsr_contains_suspect_medications(ae: dict, trial_id: str):
    """ICSR must include WHO Drug coded suspect medications."""
    with (
        patch("components.safety_reporter.terminology_map") as mock_term,
        patch("components.safety_reporter.audit_log_event") as mock_audit_tool,
    ):
        mock_term.invoke = MagicMock(side_effect=lambda args: _mock_terminology(**args))
        mock_audit_tool.invoke = MagicMock(return_value={"status": "ok"})

        result = generate_e2b_icsr.invoke({"adverse_event": ae, "trial_id": trial_id})

    assert len(result["suspect_med_codings"]) == len(ae["suspect_medications"])
    for coding in result["suspect_med_codings"]:
        assert coding["who_drug_code"] != ""


@given(ae=_adverse_event_st(), trial_id=_trial_id_st)
@settings(max_examples=50, deadline=None)
def test_p25_icsr_contains_concomitant_medications(ae: dict, trial_id: str):
    """ICSR must include WHO Drug coded concomitant medications."""
    with (
        patch("components.safety_reporter.terminology_map") as mock_term,
        patch("components.safety_reporter.audit_log_event") as mock_audit_tool,
    ):
        mock_term.invoke = MagicMock(side_effect=lambda args: _mock_terminology(**args))
        mock_audit_tool.invoke = MagicMock(return_value={"status": "ok"})

        result = generate_e2b_icsr.invoke({"adverse_event": ae, "trial_id": trial_id})

    assert len(result["concomitant_med_codings"]) == len(ae["concomitant_medications"])


@given(ae=_adverse_event_st(), trial_id=_trial_id_st)
@settings(max_examples=50, deadline=None)
def test_p25_icsr_validates_against_schema(ae: dict, trial_id: str):
    """Generated ICSR must pass E2B R3 structural validation."""
    with (
        patch("components.safety_reporter.terminology_map") as mock_term,
        patch("components.safety_reporter.audit_log_event") as mock_audit_tool,
    ):
        mock_term.invoke = MagicMock(side_effect=lambda args: _mock_terminology(**args))
        mock_audit_tool.invoke = MagicMock(return_value={"status": "ok"})

        result = generate_e2b_icsr.invoke({"adverse_event": ae, "trial_id": trial_id})

    assert result["valid"] is True, f"Validation errors: {result['validation_errors']}"
    assert result["validation_errors"] == []


@given(ae=_adverse_event_st(), trial_id=_trial_id_st)
@settings(max_examples=50, deadline=None)
def test_p25_icsr_xml_has_safetyreport_element(ae: dict, trial_id: str):
    """Generated ICSR XML must contain a <safetyreport> root structure."""
    with (
        patch("components.safety_reporter.terminology_map") as mock_term,
        patch("components.safety_reporter.audit_log_event") as mock_audit_tool,
    ):
        mock_term.invoke = MagicMock(side_effect=lambda args: _mock_terminology(**args))
        mock_audit_tool.invoke = MagicMock(return_value={"status": "ok"})

        result = generate_e2b_icsr.invoke({"adverse_event": ae, "trial_id": trial_id})

    root = ET.fromstring(result["icsr_xml"])
    assert root.tag == "ichicsr"
    assert root.find("safetyreport") is not None
    assert root.find("ichicsrmessageheader") is not None


@given(ae=_adverse_event_st(), trial_id=_trial_id_st)
@settings(max_examples=50, deadline=None)
def test_p25_icsr_xml_drug_count_matches_input(ae: dict, trial_id: str):
    """Number of <drug> elements must equal suspect + concomitant medications."""
    with (
        patch("components.safety_reporter.terminology_map") as mock_term,
        patch("components.safety_reporter.audit_log_event") as mock_audit_tool,
    ):
        mock_term.invoke = MagicMock(side_effect=lambda args: _mock_terminology(**args))
        mock_audit_tool.invoke = MagicMock(return_value={"status": "ok"})

        result = generate_e2b_icsr.invoke({"adverse_event": ae, "trial_id": trial_id})

    root = ET.fromstring(result["icsr_xml"])
    patient_el = root.find(".//patient")
    drugs = patient_el.findall("drug")
    expected = len(ae["suspect_medications"]) + len(ae["concomitant_medications"])
    assert len(drugs) == expected

    # Suspect drugs have characterization "1", concomitant "2"
    suspect_count = sum(1 for d in drugs if d.findtext("drugcharacterization") == "1")
    concomitant_count = sum(1 for d in drugs if d.findtext("drugcharacterization") == "2")
    assert suspect_count == len(ae["suspect_medications"])
    assert concomitant_count == len(ae["concomitant_medications"])


@given(ae=_adverse_event_st(), trial_id=_trial_id_st)
@settings(max_examples=50, deadline=None)
def test_p25_icsr_seriousness_flags_match_input(ae: dict, trial_id: str):
    """Seriousness flag elements must reflect the input seriousness criteria."""
    with (
        patch("components.safety_reporter.terminology_map") as mock_term,
        patch("components.safety_reporter.audit_log_event") as mock_audit_tool,
    ):
        mock_term.invoke = MagicMock(side_effect=lambda args: _mock_terminology(**args))
        mock_audit_tool.invoke = MagicMock(return_value={"status": "ok"})

        result = generate_e2b_icsr.invoke({"adverse_event": ae, "trial_id": trial_id})

    root = ET.fromstring(result["icsr_xml"])
    report = root.find("safetyreport")

    normalised = {s.lower().replace("_", "-").replace(" ", "-") for s in ae["seriousness"]}

    flag_map = {
        "death": "seriousnessdeath",
        "life-threatening": "seriousnesslifethreatening",
        "hospitalization": "seriousnesshospitalization",
        "disability": "seriousnessdisabling",
        "congenital-anomaly": "seriousnesscongenitalanomali",
        "other": "seriousnessother",
    }
    for criterion, tag in flag_map.items():
        expected = "1" if criterion in normalised else "2"
        assert report.findtext(tag) == expected, (
            f"{tag}: expected {expected} for seriousness={ae['seriousness']}"
        )


# ---------------------------------------------------------------------------
# Property 26 — ICSR Transmission Logging
# ---------------------------------------------------------------------------


@given(ae=_adverse_event_st(), trial_id=_trial_id_st)
@settings(max_examples=30, deadline=None)
def test_p26_successful_transmission_logs_status_with_icsr_id(ae: dict, trial_id: str):
    """On successful transmission, audit must log the ICSR ID and status."""
    with (
        patch("components.safety_reporter.terminology_map") as mock_term,
        patch("components.safety_reporter.audit_log_event") as mock_audit_tool,
        patch("components.safety_reporter._transmit_to_endpoint") as mock_tx,
    ):
        mock_term.invoke = MagicMock(side_effect=lambda args: _mock_terminology(**args))
        mock_audit_tool.invoke = MagicMock(return_value={"status": "ok"})
        mock_tx.return_value = {"status": "acknowledged", "endpoint": "https://safety.example.com"}

        # Generate ICSR first
        icsr_result = generate_e2b_icsr.invoke({"adverse_event": ae, "trial_id": trial_id})
        icsr_id = icsr_result["icsr_id"]

        # Reset audit mock to only capture transmission calls
        mock_audit_tool.invoke.reset_mock()

        # Transmit
        tx_result = transmit_icsr.invoke({
            "icsr_xml": icsr_result["icsr_xml"],
            "endpoint_url": "https://safety.example.com",
            "icsr_id": icsr_id,
            "trial_id": trial_id,
            "session_id": ae["session_id"],
        })

    assert tx_result["status"] in ("sent", "acknowledged")
    assert tx_result["icsr_id"] == icsr_id

    # Verify audit was called with ICSR ID and status
    audit_calls = mock_audit_tool.invoke.call_args_list
    assert len(audit_calls) >= 1
    logged_icsr_ids = []
    for c in audit_calls:
        event_data = c[0][0].get("event_data", {})
        if event_data.get("icsr_id"):
            logged_icsr_ids.append(event_data["icsr_id"])
            assert "status" in event_data
    assert icsr_id in logged_icsr_ids, "ICSR ID must appear in audit log"


@given(trial_id=_trial_id_st, session_id=_session_id_st)
@settings(max_examples=30, deadline=None)
def test_p26_failed_transmission_logs_failure(trial_id: str, session_id: str):
    """On all retries exhausted, audit must log the failure with ICSR ID."""
    icsr_id = "ICSR-TEST000001"

    with (
        patch("components.safety_reporter._transmit_to_endpoint") as mock_tx,
        patch("components.safety_reporter.audit_log_event") as mock_audit_tool,
        patch("components.safety_reporter.time.sleep"),
    ):
        mock_tx.side_effect = ConnectionError("endpoint down")
        mock_audit_tool.invoke = MagicMock(return_value={"status": "ok"})

        result = _transmit_with_retry(
            icsr_xml="<ichicsr/>",
            endpoint_url="https://safety.example.com",
            trial_id=trial_id,
            session_id=session_id,
            icsr_id=icsr_id,
        )

    assert result["status"] == "failed"
    assert result["queued_for_manual_review"] is True

    # Audit must have logged the failure with the ICSR ID
    audit_calls = mock_audit_tool.invoke.call_args_list
    assert len(audit_calls) >= 1
    failure_logged = any(
        c[0][0].get("event_data", {}).get("icsr_id") == icsr_id
        and c[0][0].get("event_data", {}).get("status") == "failed"
        for c in audit_calls
    )
    assert failure_logged, "Failed transmission must be logged with ICSR ID"


@given(trial_id=_trial_id_st, session_id=_session_id_st)
@settings(max_examples=30, deadline=None)
def test_p26_transmission_audit_includes_endpoint(trial_id: str, session_id: str):
    """Audit log entries for transmission must include the endpoint URL."""
    endpoint = "https://safety.example.com/e2b"

    with (
        patch("components.safety_reporter._transmit_to_endpoint") as mock_tx,
        patch("components.safety_reporter.audit_log_event") as mock_audit_tool,
    ):
        mock_tx.return_value = {"status": "acknowledged", "endpoint": endpoint}
        mock_audit_tool.invoke = MagicMock(return_value={"status": "ok"})

        _transmit_with_retry(
            icsr_xml="<ichicsr/>",
            endpoint_url=endpoint,
            trial_id=trial_id,
            session_id=session_id,
            icsr_id="ICSR-EP001",
        )

    audit_calls = mock_audit_tool.invoke.call_args_list
    endpoint_logged = any(
        c[0][0].get("event_data", {}).get("endpoint") == endpoint
        for c in audit_calls
    )
    assert endpoint_logged, "Endpoint URL must appear in audit log"


# ---------------------------------------------------------------------------
# Property 27 — Regulatory Reporting Timeline
# ---------------------------------------------------------------------------


@given(
    report_date=_iso_date_st,
    seriousness=st.lists(
        st.sampled_from(["hospitalization", "disability", "congenital_anomaly", "other"]),
        min_size=1,
        max_size=3,
        unique=True,
    ),
)
@settings(max_examples=50, deadline=None)
def test_p27_serious_ae_gets_15_day_deadline(report_date: str, seriousness: list[str]):
    """Non-fatal, non-life-threatening serious AEs get a 15-day deadline."""
    # Exclude fatal/life-threatening
    assume(not {"death", "life-threatening"} & {s.lower() for s in seriousness})

    deadline_date, deadline_days = _calculate_regulatory_deadline(seriousness, report_date)

    assert deadline_days == SERIOUS_AE_DEADLINE_DAYS
    expected = (
        datetime.fromisoformat(report_date) + timedelta(days=SERIOUS_AE_DEADLINE_DAYS)
    ).date().isoformat()
    assert deadline_date == expected


@given(
    report_date=_iso_date_st,
    fatal_criterion=st.sampled_from(["death", "life-threatening"]),
    extra=st.lists(
        st.sampled_from(["hospitalization", "disability", "other"]),
        min_size=0,
        max_size=2,
        unique=True,
    ),
)
@settings(max_examples=50, deadline=None)
def test_p27_fatal_life_threatening_gets_7_day_deadline(
    report_date: str, fatal_criterion: str, extra: list[str]
):
    """Fatal or life-threatening AEs get a 7-day expedited deadline."""
    seriousness = [fatal_criterion] + extra

    deadline_date, deadline_days = _calculate_regulatory_deadline(seriousness, report_date)

    assert deadline_days == FATAL_LIFE_THREATENING_DEADLINE_DAYS
    expected = (
        datetime.fromisoformat(report_date) + timedelta(days=FATAL_LIFE_THREATENING_DEADLINE_DAYS)
    ).date().isoformat()
    assert deadline_date == expected


@given(ae=_adverse_event_st(), trial_id=_trial_id_st)
@settings(max_examples=50, deadline=None)
def test_p27_generate_icsr_includes_correct_deadline(ae: dict, trial_id: str):
    """generate_e2b_icsr must return the correct deadline based on seriousness."""
    with (
        patch("components.safety_reporter.terminology_map") as mock_term,
        patch("components.safety_reporter.audit_log_event") as mock_audit_tool,
    ):
        mock_term.invoke = MagicMock(side_effect=lambda args: _mock_terminology(**args))
        mock_audit_tool.invoke = MagicMock(return_value={"status": "ok"})

        result = generate_e2b_icsr.invoke({"adverse_event": ae, "trial_id": trial_id})

    seriousness_lower = {s.lower().replace("_", "-") for s in ae["seriousness"]}
    if seriousness_lower & EXPEDITED_SERIOUSNESS:
        assert result["deadline_days"] == FATAL_LIFE_THREATENING_DEADLINE_DAYS
    else:
        assert result["deadline_days"] == SERIOUS_AE_DEADLINE_DAYS

    expected_deadline = (
        datetime.fromisoformat(ae["report_date"]) + timedelta(days=result["deadline_days"])
    ).date().isoformat()
    assert result["regulatory_deadline"] == expected_deadline


@given(ae=_adverse_event_st(), trial_id=_trial_id_st)
@settings(max_examples=30, deadline=None)
def test_p27_deadline_present_in_icsr_xml(ae: dict, trial_id: str):
    """The ICSR XML must contain the regulatory deadline element."""
    with (
        patch("components.safety_reporter.terminology_map") as mock_term,
        patch("components.safety_reporter.audit_log_event") as mock_audit_tool,
    ):
        mock_term.invoke = MagicMock(side_effect=lambda args: _mock_terminology(**args))
        mock_audit_tool.invoke = MagicMock(return_value={"status": "ok"})

        result = generate_e2b_icsr.invoke({"adverse_event": ae, "trial_id": trial_id})

    root = ET.fromstring(result["icsr_xml"])
    deadline_el = root.find(".//regulatorydeadline")
    assert deadline_el is not None
    assert deadline_el.findtext("deadlinedate") == result["regulatory_deadline"]
    assert deadline_el.findtext("deadlinedays") == str(result["deadline_days"])
