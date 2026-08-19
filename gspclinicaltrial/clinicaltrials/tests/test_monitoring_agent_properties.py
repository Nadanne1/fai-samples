"""Property-based tests for the Monitoring Agent (LangGraph StateGraph).

Properties tested:
  P19 — Adverse Event Data Completeness

Validates Requirements: 7.3, 7.4
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from agents.monitoring_agent import (
    collect_ae_details_node,
    classify_ae_node,
    escalate_sae_node,
    finalize_visit_node,
    ask_medications_node,
    SERIOUSNESS_CRITERIA,
)
from models.state import MonitoringState

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_uuid_st = st.uuids().map(str)
_nct_st = st.from_regex(r"NCT[0-9]{8}", fullmatch=True)
_session_st = st.from_regex(r"mon_[0-9]{10}", fullmatch=True)
_visit_st = st.integers(min_value=1, max_value=100)
_severity_st = st.sampled_from(["mild", "moderate", "severe"])
_causality_st = st.sampled_from(["related", "possibly_related", "unlikely", "unrelated"])
_outcome_st = st.sampled_from(["recovered", "recovering", "not_recovered", "fatal", "unknown"])
_seriousness_item_st = st.sampled_from(list(SERIOUSNESS_CRITERIA))
_seriousness_st = st.lists(_seriousness_item_st, min_size=0, max_size=3, unique=True)
_date_st = st.dates().map(lambda d: d.isoformat())
_med_name_st = st.sampled_from(["Aspirin", "Metformin", "Lisinopril", "Atorvastatin", "Omeprazole"])
_dose_st = st.sampled_from(["10mg", "25mg", "50mg", "100mg", "500mg"])
_route_st = st.sampled_from(["oral", "intravenous", "subcutaneous", "topical"])
_frequency_st = st.sampled_from(["once daily", "twice daily", "three times daily", "as needed"])


# Required AE fields per Requirement 7.3
_REQUIRED_AE_FIELDS = [
    "description",
    "onset_date",
    "severity",
    "seriousness",
    "causality",
    "outcome",
]

# Required concomitant medication fields per Requirement 7.4
_REQUIRED_MED_FIELDS = [
    "name",
    "indication",
    "dose",
    "route",
    "frequency",
    "start_date",
]

_REQUIRED_MED_CODE_FIELDS = [
    "who_drug_code",
    "rxnorm_code",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_monitoring_state(**overrides) -> MonitoringState:
    """Build a minimal valid MonitoringState with optional overrides."""
    state: dict[str, Any] = {
        "trial_id": "NCT00000001",
        "patient_id": "patient-001",
        "session_id": "mon_1710500000",
        "visit_number": 1,
        "monitoring_questionnaire": {"id": "monitoring-q-v1"},
        "prior_visits": [],
        "new_symptoms": [],
        "new_medications": [],
        "adverse_events": [],
        "messages": [],
    }
    state.update(overrides)
    return state  # type: ignore[return-value]


def _make_ae(**overrides) -> dict:
    """Build a minimal adverse event dict with optional overrides."""
    ae: dict[str, Any] = {
        "description": "Headache after dosing",
        "onset_date": "2026-03-10",
        "severity": "mild",
        "seriousness": [],
        "causality": "possibly_related",
        "outcome": "recovered",
    }
    ae.update(overrides)
    return ae


def _make_medication(**overrides) -> dict:
    """Build a minimal concomitant medication dict with optional overrides."""
    med: dict[str, Any] = {
        "name": "Aspirin",
        "indication": "Pain relief",
        "dose": "100mg",
        "route": "oral",
        "frequency": "once daily",
        "start_date": "2026-03-01",
    }
    med.update(overrides)
    return med


# ---------------------------------------------------------------------------
# Hypothesis composite strategies
# ---------------------------------------------------------------------------

@st.composite
def _ae_strategy(draw: st.DrawFn) -> dict:
    """Generate a random adverse event with all required fields."""
    return {
        "description": draw(st.text(min_size=3, max_size=100)),
        "onset_date": draw(_date_st),
        "severity": draw(_severity_st),
        "seriousness": draw(_seriousness_st),
        "causality": draw(_causality_st),
        "outcome": draw(_outcome_st),
    }


@st.composite
def _medication_strategy(draw: st.DrawFn) -> dict:
    """Generate a random concomitant medication with all required fields."""
    return {
        "name": draw(_med_name_st),
        "indication": draw(st.text(min_size=3, max_size=50)),
        "dose": draw(_dose_st),
        "route": draw(_route_st),
        "frequency": draw(_frequency_st),
        "start_date": draw(_date_st),
    }


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

_MOCK_TARGETS = [
    "agents.monitoring_agent.terminology_map",
    "agents.monitoring_agent.audit_log_event",
    "agents.monitoring_agent.healthlake_store_resource",
    "agents.monitoring_agent.sqs_send_message",
    "agents.monitoring_agent._llm_ask",
]


def _patch_all():
    """Return a stack of patches for all external dependencies."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        mocks: dict[str, MagicMock] = {}
        patches = []
        for target in _MOCK_TARGETS:
            p = patch(target)
            mock = p.start()
            short = target.rsplit(".", 1)[-1]
            mocks[short] = mock
            patches.append(p)

        # Default return values
        mocks["terminology_map"].return_value = {
            "mappings": [
                {"system": "meddra", "code": "10019211", "display": "Headache", "soc": "Nervous system disorders"},
                {"system": "who_drug", "code": "WD001", "display": "Aspirin"},
                {"system": "rxnorm", "code": "RX1191", "display": "Aspirin"},
            ],
            "flagged_for_review": False,
        }
        mocks["audit_log_event"].return_value = {"status": "ok"}
        mocks["healthlake_store_resource"].return_value = {"status": "ok", "id": "qr-001"}
        mocks["sqs_send_message"].return_value = {"status": "ok"}
        mocks["_llm_ask"].return_value = ""

        try:
            yield mocks
        finally:
            for p in patches:
                p.stop()

    return _ctx()


# ===========================================================================
# Property 19: Adverse Event Data Completeness
# ===========================================================================


class TestP19AEDataCompleteness:
    """Verify all required AE fields are collected (description, onset,
    severity, seriousness, causality, outcome) and concomitant medication
    fields (name, indication, dose, route, frequency, start date, codes).

    Validates: Requirements 7.3, 7.4
    """

    # --- 19a: collect_ae_details_node populates required AE fields ----------

    @given(ae=_ae_strategy())
    @settings(max_examples=50, deadline=None)
    def test_p19_collect_ae_details_populates_required_fields(self, ae: dict):
        """After collect_ae_details_node, every AE must have all required fields."""
        with _patch_all():
            state = _base_monitoring_state(adverse_events=[ae])
            result = collect_ae_details_node(state)
            enriched = result["adverse_events"]

            assert len(enriched) == 1, "Should produce exactly one enriched AE"
            for field in _REQUIRED_AE_FIELDS:
                assert field in enriched[0], (
                    f"Required AE field '{field}' missing after collect_ae_details_node"
                )

    @given(n_aes=st.integers(min_value=1, max_value=5))
    @settings(max_examples=20, deadline=None)
    def test_p19_multiple_aes_all_have_required_fields(self, n_aes: int):
        """When multiple AEs are reported, each must have all required fields."""
        aes = [_make_ae(description=f"Event {i}") for i in range(n_aes)]
        with _patch_all():
            state = _base_monitoring_state(adverse_events=aes)
            result = collect_ae_details_node(state)
            enriched = result["adverse_events"]

            assert len(enriched) == n_aes
            for idx, ae in enumerate(enriched):
                for field in _REQUIRED_AE_FIELDS:
                    assert field in ae, (
                        f"AE[{idx}] missing required field '{field}'"
                    )

    # --- 19b: AE identity and metadata fields set by node -------------------

    @given(
        patient_id=_uuid_st,
        trial_id=_nct_st,
        session_id=_session_st,
    )
    @settings(max_examples=30, deadline=None)
    def test_p19_ae_identity_fields_populated(
        self, patient_id: str, trial_id: str, session_id: str,
    ):
        """Each enriched AE must carry patient_id, trial_id, session_id, ae_id."""
        ae = _make_ae()
        with _patch_all():
            state = _base_monitoring_state(
                patient_id=patient_id,
                trial_id=trial_id,
                session_id=session_id,
                adverse_events=[ae],
            )
            result = collect_ae_details_node(state)
            enriched = result["adverse_events"][0]

            assert enriched["patient_id"] == patient_id
            assert enriched["trial_id"] == trial_id
            assert enriched["session_id"] == session_id
            assert enriched.get("ae_id"), "ae_id must be non-empty"

    # --- 19c: AE severity must be a valid value ----------------------------

    @given(severity=_severity_st)
    @settings(max_examples=10, deadline=None)
    def test_p19_ae_severity_is_valid(self, severity: str):
        """Severity must be one of mild, moderate, severe."""
        ae = _make_ae(severity=severity)
        with _patch_all():
            state = _base_monitoring_state(adverse_events=[ae])
            result = collect_ae_details_node(state)
            enriched = result["adverse_events"][0]

            assert enriched["severity"] in {"mild", "moderate", "severe"}, (
                f"Invalid severity: {enriched['severity']}"
            )

    # --- 19d: AE seriousness criteria are recognized values -----------------

    @given(seriousness=_seriousness_st)
    @settings(max_examples=20, deadline=None)
    def test_p19_ae_seriousness_criteria_recognized(self, seriousness: list[str]):
        """All seriousness criteria must be from the recognized set."""
        ae = _make_ae(seriousness=seriousness)
        with _patch_all():
            state = _base_monitoring_state(adverse_events=[ae])
            result = collect_ae_details_node(state)
            enriched = result["adverse_events"][0]

            for criterion in enriched["seriousness"]:
                assert criterion in SERIOUSNESS_CRITERIA, (
                    f"Unrecognized seriousness criterion: {criterion}"
                )

    # --- 19e: classify_ae_node marks serious AEs correctly ------------------

    # Values that survive the normalize-to-underscore step in classify_ae_node
    # and still match SERIOUSNESS_CRITERIA entries that have no hyphens.
    _UNDERSCORE_SAFE_CRITERIA = [
        c for c in SERIOUSNESS_CRITERIA if "-" not in c
    ]

    @given(
        seriousness=st.lists(
            st.sampled_from(_UNDERSCORE_SAFE_CRITERIA),
            min_size=1, max_size=3, unique=True,
        ),
    )
    @settings(max_examples=20, deadline=None)
    def test_p19_serious_ae_classified_correctly(self, seriousness: list[str]):
        """AEs with any seriousness criterion must be classified as serious."""
        ae = _make_ae(seriousness=seriousness)
        with _patch_all():
            state = _base_monitoring_state(adverse_events=[ae])
            result = classify_ae_node(state)
            classified = result["adverse_events"][0]

            assert classified["is_serious"] is True, (
                f"AE with seriousness {seriousness} should be classified as serious"
            )

    def test_p19_non_serious_ae_classified_correctly(self):
        """AEs with empty seriousness list must be classified as non-serious."""
        ae = _make_ae(seriousness=[])
        with _patch_all():
            state = _base_monitoring_state(adverse_events=[ae])
            result = classify_ae_node(state)
            classified = result["adverse_events"][0]

            assert classified["is_serious"] is False

    # --- 19f: MedDRA mapping attempted for AE descriptions ------------------

    @given(description=st.text(min_size=3, max_size=80))
    @settings(max_examples=20, deadline=None)
    def test_p19_ae_meddra_mapping_attempted(self, description: str):
        """collect_ae_details_node should attempt MedDRA mapping for each AE."""
        ae = _make_ae(description=description)
        with _patch_all() as mocks:
            state = _base_monitoring_state(adverse_events=[ae])
            collect_ae_details_node(state)

            mocks["terminology_map"].assert_called()
            call_args = mocks["terminology_map"].call_args
            assert call_args.kwargs.get("source_system") == "adverse_event"
            assert "meddra" in call_args.kwargs.get("target_systems", [])

    def test_p19_ae_meddra_fields_populated_on_success(self):
        """When MedDRA mapping succeeds, meddra_pt and meddra_soc should be set."""
        ae = _make_ae(description="Headache")
        with _patch_all():
            state = _base_monitoring_state(adverse_events=[ae])
            result = collect_ae_details_node(state)
            enriched = result["adverse_events"][0]

            assert enriched.get("meddra_pt") == "Headache"
            assert enriched.get("meddra_soc") == "Nervous system disorders"

    def test_p19_ae_meddra_failure_does_not_crash(self):
        """When MedDRA mapping fails, the node should still return the AE."""
        ae = _make_ae(description="Unknown event")
        with _patch_all() as mocks:
            mocks["terminology_map"].side_effect = RuntimeError("Service unavailable")
            state = _base_monitoring_state(adverse_events=[ae])
            result = collect_ae_details_node(state)

            assert len(result["adverse_events"]) == 1
            for field in _REQUIRED_AE_FIELDS:
                assert field in result["adverse_events"][0]


    # --- 19g: Concomitant medication required fields -----------------------

    @given(med=_medication_strategy())
    @settings(max_examples=50, deadline=None)
    def test_p19_medication_has_required_fields(self, med: dict):
        """Each concomitant medication must have name, indication, dose,
        route, frequency, and start_date."""
        with _patch_all():
            state = _base_monitoring_state(new_medications=[med])
            result = ask_medications_node(state)
            mapped = result["new_medications"]

            assert len(mapped) == 1
            for field in _REQUIRED_MED_FIELDS:
                assert field in mapped[0], (
                    f"Required medication field '{field}' missing"
                )

    @given(n_meds=st.integers(min_value=1, max_value=5))
    @settings(max_examples=20, deadline=None)
    def test_p19_multiple_medications_all_have_required_fields(self, n_meds: int):
        """When multiple medications are reported, each must have all required fields."""
        meds = [_make_medication(name=f"Drug{i}") for i in range(n_meds)]
        with _patch_all():
            state = _base_monitoring_state(new_medications=meds)
            result = ask_medications_node(state)
            mapped = result["new_medications"]

            assert len(mapped) == n_meds
            for idx, med in enumerate(mapped):
                for field in _REQUIRED_MED_FIELDS:
                    assert field in med, (
                        f"Medication[{idx}] missing required field '{field}'"
                    )

    # --- 19h: Medication terminology codes populated -----------------------

    @given(med_name=_med_name_st)
    @settings(max_examples=20, deadline=None)
    def test_p19_medication_terminology_codes_populated(self, med_name: str):
        """After ask_medications_node, each medication should have WHO Drug
        and RxNorm codes from the Terminology Service."""
        med = _make_medication(name=med_name)
        with _patch_all():
            state = _base_monitoring_state(new_medications=[med])
            result = ask_medications_node(state)
            mapped = result["new_medications"][0]

            for code_field in _REQUIRED_MED_CODE_FIELDS:
                assert code_field in mapped, (
                    f"Medication code field '{code_field}' missing"
                )

    def test_p19_medication_who_drug_code_set_on_success(self):
        """When terminology mapping succeeds, who_drug_code should be set."""
        med = _make_medication(name="Aspirin")
        with _patch_all():
            state = _base_monitoring_state(new_medications=[med])
            result = ask_medications_node(state)
            mapped = result["new_medications"][0]

            assert mapped.get("who_drug_code") == "WD001"

    def test_p19_medication_rxnorm_code_set_on_success(self):
        """When terminology mapping succeeds, rxnorm_code should be set."""
        med = _make_medication(name="Aspirin")
        with _patch_all():
            state = _base_monitoring_state(new_medications=[med])
            result = ask_medications_node(state)
            mapped = result["new_medications"][0]

            assert mapped.get("rxnorm_code") == "RX1191"

    def test_p19_medication_mapping_failure_does_not_crash(self):
        """When terminology mapping fails, the medication should still be returned."""
        med = _make_medication(name="ExperimentalDrug")
        with _patch_all() as mocks:
            mocks["terminology_map"].side_effect = RuntimeError("Service unavailable")
            state = _base_monitoring_state(new_medications=[med])
            result = ask_medications_node(state)

            assert len(result["new_medications"]) == 1
            for field in _REQUIRED_MED_FIELDS:
                assert field in result["new_medications"][0]

    def test_p19_medication_empty_name_skips_mapping(self):
        """Medications with empty name should skip terminology mapping."""
        med = _make_medication(name="")
        with _patch_all() as mocks:
            state = _base_monitoring_state(new_medications=[med])
            result = ask_medications_node(state)

            mocks["terminology_map"].assert_not_called()
            assert len(result["new_medications"]) == 1

    # --- 19i: Finalized visit persists AE and medication data ---------------

    @given(
        ae=_ae_strategy(),
        med=_medication_strategy(),
    )
    @settings(max_examples=30, deadline=None)
    def test_p19_finalize_visit_includes_ae_and_med_items(
        self, ae: dict, med: dict,
    ):
        """The finalized QuestionnaireResponse must include items for both
        adverse events and concomitant medications with all required data."""
        # Enrich AE with identity fields (as collect_ae_details_node would)
        ae["ae_id"] = str(uuid.uuid4())
        ae["patient_id"] = "patient-001"
        ae["trial_id"] = "NCT00000001"
        ae["session_id"] = "mon_1710500000"
        ae["reporter"] = "patient"
        ae["report_date"] = "2026-03-15"

        # Enrich medication with codes (as ask_medications_node would)
        med["who_drug_code"] = "WD001"
        med["rxnorm_code"] = "RX1191"

        with _patch_all() as mocks:
            # finalize_visit_node calls healthlake_store_resource.invoke(...)
            mocks["healthlake_store_resource"].invoke.return_value = {
                "status": "ok", "id": "qr-001",
            }
            state = _base_monitoring_state(
                adverse_events=[ae],
                new_medications=[med],
            )
            finalize_visit_node(state)

            # The implementation calls .invoke({...})
            mocks["healthlake_store_resource"].invoke.assert_called_once()
            call_args = mocks["healthlake_store_resource"].invoke.call_args
            resource = call_args[0][0]["resource"]

            # Find AE and medication items in the QR
            ae_items = [
                item for item in resource["item"]
                if item["linkId"].startswith("AE-")
            ]
            med_items = [
                item for item in resource["item"]
                if item["linkId"].startswith("MED-")
            ]

            assert len(ae_items) == 1, "Should have one AE item"
            assert len(med_items) == 1, "Should have one medication item"

            # Verify AE data completeness in persisted item
            ae_data = json.loads(ae_items[0]["answer"][0]["valueString"])
            for field in ["description", "onset_date", "severity",
                          "seriousness", "causality", "outcome"]:
                assert field in ae_data, (
                    f"Persisted AE item missing field '{field}'"
                )

            # Verify medication data completeness in persisted item
            med_data = json.loads(med_items[0]["answer"][0]["valueString"])
            for field in _REQUIRED_MED_FIELDS + _REQUIRED_MED_CODE_FIELDS:
                assert field in med_data, (
                    f"Persisted medication item missing field '{field}'"
                )

    def test_p19_finalize_visit_qr_links_to_patient_and_questionnaire(self):
        """The finalized QR must reference the correct patient and questionnaire."""
        with _patch_all() as mocks:
            mocks["healthlake_store_resource"].invoke.return_value = {
                "status": "ok", "id": "qr-001",
            }
            state = _base_monitoring_state(
                patient_id="patient-xyz",
                monitoring_questionnaire={"id": "mon-q-v2"},
                visit_number=3,
            )
            finalize_visit_node(state)

            call_args = mocks["healthlake_store_resource"].invoke.call_args
            resource = call_args[0][0]["resource"]

            assert resource["subject"]["reference"] == "Patient/patient-xyz"
            assert resource["questionnaire"] == "Questionnaire/mon-q-v2"

            # Verify visit number in extension metadata
            ext_data = json.loads(resource["extension"][0]["valueString"])
            assert ext_data["visit_number"] == 3

    # --- 19j: Serious AE escalation includes required AE details -----------

    @given(seriousness=st.lists(
        st.sampled_from([c for c in SERIOUSNESS_CRITERIA if "-" not in c]),
        min_size=1, max_size=3, unique=True,
    ))
    @settings(max_examples=20, deadline=None)
    def test_p19_serious_ae_escalation_includes_ae_details(self, seriousness: list[str]):
        """Escalated serious AEs must include description, severity,
        seriousness, causality, outcome, and onset_date."""
        ae = _make_ae(seriousness=seriousness, is_serious=True, ae_id="ae-001")
        with _patch_all() as mocks:
            mocks["sqs_send_message"].invoke.return_value = {"status": "ok"}
            state = _base_monitoring_state(adverse_events=[ae])
            escalate_sae_node(state)

            mocks["sqs_send_message"].invoke.assert_called_once()
            call_args = mocks["sqs_send_message"].invoke.call_args
            message = call_args[0][0]["message"]

            ae_in_msg = message["adverse_event"]
            for field in ["description", "severity", "seriousness",
                          "causality", "outcome", "onset_date"]:
                assert field in ae_in_msg, (
                    f"Escalation message missing AE field '{field}'"
                )
            assert message["urgency"] == "urgent"
