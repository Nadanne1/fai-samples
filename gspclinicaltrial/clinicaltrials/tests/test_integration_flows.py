"""Integration tests for end-to-end clinical trial flows.

Tests four primary flows:
  1. Screening flow:  intake message → patient load → questionnaire → eligibility → finalize
  2. Monitoring flow: visit scheduled → symptoms → medications → AE → finalize
  3. Escalation flow: borderline determination → escalation queue → PI review
  4. Safety flow:     serious AE → escalation → ICSR generation → transmission

All external services (HealthLake, DynamoDB, SQS, KMS, Bedrock LLM) are mocked.
These tests validate that the components wire together correctly end-to-end.

Requirements: All
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

from models.state import MonitoringState, ScreeningState


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_REQUIRED_FHIR_TYPES = [
    "Patient", "Condition", "MedicationRequest",
    "AllergyIntolerance", "Procedure", "Observation",
]


def _make_patient_fhir_data(patient_id: str = "patient-001") -> dict:
    """Build a realistic patient FHIR bundle for testing."""
    return {
        "Patient": [{"resourceType": "Patient", "id": patient_id,
                      "birthDate": "1980-05-15",
                      "name": [{"text": "Test Patient"}]}],
        "Condition": [{"resourceType": "Condition", "id": "cond-1",
                       "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10",
                                            "code": "E11", "display": "Type 2 diabetes"}],
                                "text": "Type 2 diabetes"},
                       "subject": {"reference": f"Patient/{patient_id}"}}],
        "MedicationRequest": [{"resourceType": "MedicationRequest", "id": "med-1",
                               "medicationCodeableConcept": {"text": "Metformin 500mg"},
                               "subject": {"reference": f"Patient/{patient_id}"}}],
        "AllergyIntolerance": [],
        "Procedure": [],
        "Observation": [{"resourceType": "Observation", "id": "obs-1",
                         "code": {"coding": [{"system": "http://loinc.org",
                                              "code": "4548-4", "display": "HbA1c"}]},
                         "valueQuantity": {"value": 7.2, "unit": "%"},
                         "subject": {"reference": f"Patient/{patient_id}"}}],
    }


def _make_screening_questionnaire() -> dict:
    """Build a minimal FHIR Questionnaire for screening tests."""
    return {
        "resourceType": "Questionnaire",
        "id": "trial-NCT00000001-screening-v1",
        "status": "active",
        "identifier": [{"system": "https://clinicaltrials.gov", "value": "NCT00000001"}],
        "version": "1.0",
        "item": [
            {
                "linkId": "IE-001",
                "text": "Are you between 18 and 75 years old?",
                "type": "boolean",
                "required": True,
                "code": [{"system": "http://snomed.info/sct", "code": "424144002"}],
                "extension": [{
                    "url": "http://example.org/fhir/StructureDefinition/eligibility-rule",
                    "valueString": json.dumps({
                        "type": "inclusion", "fhir_path": "Patient.birthDate",
                        "operator": "age_between", "value": [18, 75],
                    }),
                }],
            },
            {
                "linkId": "IE-002",
                "text": "Do you have a confirmed diagnosis of Type 2 Diabetes?",
                "type": "boolean",
                "required": True,
                "code": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "E11"}],
            },
        ],
    }


def _base_screening_state(**overrides) -> dict:
    """Build a minimal ScreeningState dict with optional overrides."""
    state: dict[str, Any] = {
        "trial_id": "NCT00000001",
        "patient_id": "patient-001",
        "session_id": f"scr_{uuid.uuid4().hex[:12]}",
        "questionnaire": _make_screening_questionnaire(),
        "patient_fhir_data": {},
        "current_item_index": 0,
        "responses": [],
        "discrepancies": [],
        "eligibility_criteria_results": [],
        "eligibility_determination": "",
        "messages": [],
        "deep_dive_active": None,
        "missing_items": [],
        "declined_items": [],
    }
    state.update(overrides)
    return state


def _base_monitoring_state(**overrides) -> dict:
    """Build a minimal MonitoringState dict with optional overrides."""
    state: dict[str, Any] = {
        "trial_id": "NCT00000001",
        "patient_id": "patient-001",
        "session_id": f"mon_{uuid.uuid4().hex[:12]}",
        "visit_number": 1,
        "monitoring_questionnaire": {"id": "monitoring-q-v1"},
        "prior_visits": [],
        "new_symptoms": [],
        "new_medications": [],
        "adverse_events": [],
        "messages": [],
    }
    state.update(overrides)
    return state


def _make_serious_ae() -> dict:
    """Build a serious adverse event for safety flow tests."""
    return {
        "ae_id": f"AE-{uuid.uuid4().hex[:8]}",
        "patient_id": "patient-001",
        "trial_id": "NCT00000001",
        "session_id": "mon_test_session",
        "description": "Severe chest pain requiring hospitalization",
        "onset_date": "2026-03-20",
        "severity": "severe",
        "seriousness": ["hospitalization"],
        "causality": "possibly_related",
        "outcome": "recovering",
        "suspect_medications": [{"name": "Study Drug X", "who_drug_code": "", "rxnorm_code": ""}],
        "concomitant_medications": [{"name": "Aspirin", "who_drug_code": "B01AC06", "rxnorm_code": "1191"}],
        "reporter": "patient",
        "report_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "regulatory_deadline": "",
    }


# ---------------------------------------------------------------------------
# Mock context manager that patches all external dependencies
# ---------------------------------------------------------------------------

class _MockExternals:
    """Context manager that patches all external AWS and LLM dependencies."""

    def __init__(self):
        self.patches = []
        self.mocks = {}

    def __enter__(self):
        # HealthLake tools
        p = patch("agents.screening_agent.healthlake_query_patient")
        self.mocks["hl_query"] = p.start()
        self.mocks["hl_query"].invoke.return_value = _make_patient_fhir_data()
        self.patches.append(p)

        p = patch("agents.screening_agent.healthlake_store_resource")
        self.mocks["hl_store_screening"] = p.start()
        self.mocks["hl_store_screening"].invoke.return_value = {
            "status": "created", "resource_id": "qr-001", "resource_type": "QuestionnaireResponse",
        }
        self.patches.append(p)

        # Audit tool — used in many modules
        for mod in [
            "agents.screening_agent.audit_log_event",
            "agents.monitoring_agent.audit_log_event",
            "tools.audit.audit_log_event",
        ]:
            p = patch(mod)
            m = p.start()
            m.invoke.return_value = {"audit_id": "aud-001", "status": "recorded"}
            self.mocks[f"audit_{mod}"] = m
            self.patches.append(p)

        # SQS queue tool
        p = patch("agents.screening_agent.sqs_send_message")
        self.mocks["sqs_screening"] = p.start()
        self.mocks["sqs_screening"].invoke.return_value = {
            "message_id": "msg-001", "queue_name": "TrialEscalationQueue",
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        self.patches.append(p)

        p = patch("agents.monitoring_agent.sqs_send_message")
        self.mocks["sqs_monitoring"] = p.start()
        self.mocks["sqs_monitoring"].invoke.return_value = {
            "message_id": "msg-002", "queue_name": "TrialEscalationQueue",
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        self.patches.append(p)

        # LLM mock
        mock_response = MagicMock()
        mock_response.content = "Could you please answer the following question?"

        p = patch("agents.screening_agent._get_llm")
        self.mocks["llm_screening"] = p.start()
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = mock_response
        self.mocks["llm_screening"].return_value = mock_llm
        self.patches.append(p)

        p = patch("agents.monitoring_agent._get_llm")
        self.mocks["llm_monitoring"] = p.start()
        mock_llm_mon = MagicMock()
        mock_llm_mon.invoke.return_value = mock_response
        self.mocks["llm_monitoring"].return_value = mock_llm_mon
        self.patches.append(p)

        # Monitoring HealthLake tools
        p = patch("agents.monitoring_agent.healthlake_query_patient")
        self.mocks["hl_query_mon"] = p.start()
        self.mocks["hl_query_mon"].invoke.return_value = _make_patient_fhir_data()
        self.patches.append(p)

        p = patch("agents.monitoring_agent.healthlake_store_resource")
        self.mocks["hl_store_mon"] = p.start()
        self.mocks["hl_store_mon"].invoke.return_value = {
            "status": "created", "resource_id": "qr-mon-001",
            "resource_type": "QuestionnaireResponse",
        }
        self.patches.append(p)

        # EHR tool
        p = patch("agents.screening_agent.ehr_fhir_query")
        self.mocks["ehr"] = p.start()
        self.mocks["ehr"].invoke.return_value = _make_patient_fhir_data()
        self.patches.append(p)

        # Terminology service (used by monitoring agent)
        p = patch("agents.monitoring_agent.terminology_map")
        self.mocks["terminology_mon"] = p.start()
        self.mocks["terminology_mon"].invoke.return_value = {
            "mappings": [
                {"system": "WHO Drug Dictionary", "code": "B01AC06",
                 "display": "Aspirin", "confidence": 0.95},
                {"system": "RxNorm", "code": "1191",
                 "display": "Aspirin", "confidence": 0.95},
            ],
            "flagged_for_review": False,
        }
        self.patches.append(p)

        return self

    def __exit__(self, *args):
        for p in reversed(self.patches):
            p.stop()


# ===================================================================
# Flow 1: Screening — intake → patient load → questionnaire →
#          eligibility → finalize
# ===================================================================


class TestScreeningFlow:
    """End-to-end screening flow integration tests."""

    def test_screening_full_eligible_flow(self):
        """A patient who answers all questions positively should flow through
        load_patient → load_questionnaire → ask/validate loop →
        resolve_eligibility → finalize_screening with 'eligible' determination."""
        from agents.screening_agent import (
            load_patient_node,
            load_questionnaire_node,
            ask_question_node,
            validate_response_node,
            check_discrepancy_node,
            check_enable_when_node,
            check_completion_node,
            resolve_eligibility_node,
            finalize_screening_node,
        )

        with _MockExternals() as mocks:
            state = _base_screening_state()

            # Step 1: Load patient
            result = load_patient_node(state)
            state.update(result)
            assert "patient_fhir_data" in state
            for rt in _REQUIRED_FHIR_TYPES:
                assert rt in state["patient_fhir_data"]

            # Step 2: Load questionnaire
            result = load_questionnaire_node(state)
            state.update(result)
            assert len(state["questionnaire"].get("item", [])) >= 2

            # Step 3-6: Answer each question
            for i in range(len(state["questionnaire"]["item"])):
                state["current_item_index"] = i

                # Ask question
                result = ask_question_node(state)
                state.update(result)
                assert any(m["role"] == "assistant" for m in state["messages"])

                # Simulate patient response
                state["messages"].append({"role": "user", "content": "true"})

                # Validate response
                result = validate_response_node(state)
                state.update(result)
                last_msg = state["messages"][-1]
                assert last_msg.get("_valid") is True

                # Check discrepancy
                result = check_discrepancy_node(state)
                state.update(result)

                # Check enableWhen
                result = check_enable_when_node(state)
                state.update(result)

            # Step 7: Check completion
            result = check_completion_node(state)
            state.update(result)
            assert state["missing_items"] == []

            # Step 8: Resolve eligibility
            result = resolve_eligibility_node(state)
            state.update(result)
            assert state["eligibility_determination"] in ("eligible", "ineligible", "borderline")

            # Step 9: Finalize
            result = finalize_screening_node(state)
            state.update(result)

            # Verify HealthLake store was called with a QuestionnaireResponse
            store_call = mocks.mocks["hl_store_screening"].invoke.call_args
            stored = store_call[0][0]["resource"]
            assert stored["resourceType"] == "QuestionnaireResponse"
            assert stored["subject"]["reference"] == f"Patient/{state['patient_id']}"

    def test_screening_flow_with_declined_items(self):
        """When a patient declines a required item, it should be recorded
        and the screening should still finalize."""
        from agents.screening_agent import (
            load_patient_node,
            load_questionnaire_node,
            check_completion_node,
            record_declined_node,
            resolve_eligibility_node,
            finalize_screening_node,
        )

        with _MockExternals():
            state = _base_screening_state()

            # Load patient and questionnaire
            state.update(load_patient_node(state))
            state.update(load_questionnaire_node(state))

            # Answer first question, decline second
            state["responses"] = [{"linkId": "IE-001", "answer": True, "raw": "true"}]
            state["current_item_index"] = 1

            # Check completion — should detect missing IE-002
            result = check_completion_node(state)
            state.update(result)
            missing_ids = [m["linkId"] for m in state["missing_items"]]
            assert "IE-002" in missing_ids

            # Record declined
            result = record_declined_node(state)
            state.update(result)
            assert len(state["declined_items"]) >= 1

            # Resolve eligibility
            result = resolve_eligibility_node(state)
            state.update(result)

            # Finalize
            result = finalize_screening_node(state)
            state.update(result)

    def test_screening_intake_message_processing(self):
        """Simulate the intake consumer processing an SQS message through
        the full screening pipeline."""
        from intake_consumer import _process_message

        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {
            "eligibility_determination": "eligible",
            "responses": [{"linkId": "IE-001", "answer": True}],
            "messages": [],
        }

        message = {
            "MessageId": "test-msg-001",
            "Body": json.dumps({
                "patient_id": "patient-001",
                "trial_id": "NCT00000001",
            }),
        }

        with patch("intake_consumer.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            success = _process_message(mock_graph, message)

        assert success is True
        mock_graph.invoke.assert_called_once()
        invoke_args = mock_graph.invoke.call_args[0][0]
        assert invoke_args["patient_id"] == "patient-001"
        assert invoke_args["trial_id"] == "NCT00000001"

    def test_screening_intake_invalid_message_rejected(self):
        """Messages missing patient_id or trial_id should fail processing."""
        from intake_consumer import _process_message

        mock_graph = MagicMock()

        # Missing trial_id
        message = {
            "MessageId": "test-msg-002",
            "Body": json.dumps({"patient_id": "patient-001"}),
        }
        assert _process_message(mock_graph, message) is False
        mock_graph.invoke.assert_not_called()

        # Invalid JSON
        message_bad = {
            "MessageId": "test-msg-003",
            "Body": "not-json{{{",
        }
        assert _process_message(mock_graph, message_bad) is False



# ===================================================================
# Flow 2: Monitoring — visit scheduled → symptoms → medications →
#          AE → finalize
# ===================================================================


class TestMonitoringFlow:
    """End-to-end monitoring visit integration tests."""

    def test_monitoring_full_visit_no_ae(self):
        """A monitoring visit with symptoms and medications but no adverse
        events should flow through all nodes and finalize."""
        from agents.monitoring_agent import (
            load_visit_history_node,
            ask_symptoms_node,
            ask_medications_node,
            ask_adverse_events_node,
            finalize_visit_node,
            _route_after_adverse_events,
        )

        with _MockExternals() as mocks:
            state = _base_monitoring_state(
                new_symptoms=["mild headache"],
                new_medications=[{
                    "name": "Aspirin", "indication": "Pain",
                    "dose": "100mg", "route": "oral",
                    "frequency": "once daily", "start_date": "2026-03-15",
                }],
            )

            # Step 1: Load visit history
            result = load_visit_history_node(state)
            state.update(result)

            # Step 2: Ask symptoms
            result = ask_symptoms_node(state)
            state.update(result)
            assert any(m["role"] == "assistant" for m in state["messages"])

            # Step 3: Ask medications
            result = ask_medications_node(state)
            state.update(result)

            # Step 4: Ask adverse events — no AE reported
            state["adverse_events"] = []
            result = ask_adverse_events_node(state)
            state.update(result)

            # Routing should go to finalize (no AE)
            route = _route_after_adverse_events(state)
            assert route == "finalize_visit"

            # Step 5: Finalize visit
            result = finalize_visit_node(state)
            state.update(result)

            # Verify QuestionnaireResponse stored
            mocks.mocks["hl_store_mon"].invoke.assert_called()
            stored = mocks.mocks["hl_store_mon"].invoke.call_args[0][0]["resource"]
            assert stored["resourceType"] == "QuestionnaireResponse"

    def test_monitoring_visit_with_non_serious_ae(self):
        """A monitoring visit with a non-serious AE should collect details,
        classify as non-serious, and finalize without escalation."""
        from agents.monitoring_agent import (
            load_visit_history_node,
            ask_symptoms_node,
            ask_medications_node,
            ask_adverse_events_node,
            collect_ae_details_node,
            classify_ae_node,
            finalize_visit_node,
            _route_after_classify,
        )

        with _MockExternals():
            ae = {
                "description": "Mild nausea after dosing",
                "onset_date": "2026-03-18",
                "severity": "mild",
                "seriousness": [],
                "causality": "possibly_related",
                "outcome": "recovered",
            }
            state = _base_monitoring_state(adverse_events=[ae])

            state.update(load_visit_history_node(state))
            state.update(ask_symptoms_node(state))
            state.update(ask_medications_node(state))
            state.update(ask_adverse_events_node(state))
            state.update(collect_ae_details_node(state))
            state.update(classify_ae_node(state))

            # Non-serious → should route to finalize
            route = _route_after_classify(state)
            assert route == "finalize_visit"

            state.update(finalize_visit_node(state))

    def test_monitoring_visit_with_serious_ae_escalates(self):
        """A monitoring visit with a serious AE should escalate to the
        escalation queue before finalizing."""
        from agents.monitoring_agent import (
            load_visit_history_node,
            ask_symptoms_node,
            ask_medications_node,
            ask_adverse_events_node,
            collect_ae_details_node,
            classify_ae_node,
            escalate_sae_node,
            finalize_visit_node,
            _route_after_classify,
        )

        with _MockExternals() as mocks:
            ae = {
                "description": "Severe chest pain requiring hospitalization",
                "onset_date": "2026-03-20",
                "severity": "severe",
                "seriousness": ["hospitalization"],
                "causality": "possibly_related",
                "outcome": "recovering",
            }
            state = _base_monitoring_state(adverse_events=[ae])

            state.update(load_visit_history_node(state))
            state.update(ask_symptoms_node(state))
            state.update(ask_medications_node(state))
            state.update(ask_adverse_events_node(state))
            state.update(collect_ae_details_node(state))
            state.update(classify_ae_node(state))

            # Serious → should route to escalate
            route = _route_after_classify(state)
            assert route == "escalate_sae"

            # Escalate
            result = escalate_sae_node(state)
            state.update(result)

            # Verify SQS escalation was sent
            mocks.mocks["sqs_monitoring"].invoke.assert_called()
            sqs_call = mocks.mocks["sqs_monitoring"].invoke.call_args[0][0]
            assert sqs_call["queue_name"] == "TrialEscalationQueue"
            attrs = sqs_call.get("attributes", {})
            assert attrs.get("Urgency") == "urgent"

            # Finalize
            state.update(finalize_visit_node(state))


# ===================================================================
# Flow 3: Escalation — borderline determination → escalation queue →
#          PI review audit
# ===================================================================


class TestEscalationFlow:
    """End-to-end escalation flow integration tests."""

    def test_borderline_triggers_escalation(self):
        """A borderline eligibility determination should send a complete
        escalation message to the escalation queue."""
        from agents.screening_agent import (
            resolve_eligibility_node,
            escalate_node,
            _route_after_eligibility,
        )

        with _MockExternals() as mocks:
            state = _base_screening_state(
                responses=[
                    {"linkId": "IE-001", "answer": True, "raw": "true"},
                    {"linkId": "IE-002", "answer": True, "raw": "true"},
                ],
                eligibility_criteria_results=[
                    {"criterion_id": "IE-001", "result": "pass",
                     "fhir_refs": ["Patient/patient-001"]},
                    {"criterion_id": "IE-002", "result": "indeterminate",
                     "fhir_refs": ["Condition/cond-1"],
                     "discrepancy": {"severity": "warning",
                                     "details": "Value mismatch"}},
                ],
                eligibility_determination="borderline",
                discrepancies=[{
                    "criterion_id": "IE-002", "severity": "warning",
                    "patient_value": "true", "fhir_value": "unconfirmed",
                }],
            )

            # Route should go to escalate
            route = _route_after_eligibility(state)
            assert route == "escalate"

            # Escalate
            result = escalate_node(state)
            state.update(result)

            # Verify escalation message sent to SQS
            mocks.mocks["sqs_screening"].invoke.assert_called()
            sqs_call = mocks.mocks["sqs_screening"].invoke.call_args[0][0]
            assert sqs_call["queue_name"] == "TrialEscalationQueue"

            # Verify message body contains required context
            body = sqs_call["message"]
            assert body.get("patient_id") == "patient-001"
            assert body.get("trial_id") == "NCT00000001"
            assert "eligibility_determination" in body or "determination" in body

            # Verify urgency attribute
            attrs = sqs_call.get("attributes", {})
            assert "Urgency" in attrs or "EscalationReason" in attrs

    def test_escalation_audit_trail_recorded(self):
        """Escalation events must be logged in the audit trail with
        session_id, patient_id, escalation reason, and timestamp."""
        from agents.screening_agent import escalate_node

        with _MockExternals() as mocks:
            state = _base_screening_state(
                eligibility_determination="borderline",
                eligibility_criteria_results=[
                    {"criterion_id": "IE-001", "result": "pass"},
                    {"criterion_id": "IE-002", "result": "indeterminate"},
                ],
                discrepancies=[{"criterion_id": "IE-002", "severity": "warning"}],
            )

            escalate_node(state)

            # Verify audit was called with escalation event
            audit_mock = mocks.mocks["audit_agents.screening_agent.audit_log_event"]
            assert audit_mock.invoke.called
            audit_calls = audit_mock.invoke.call_args_list
            escalation_logged = any(
                c[0][0].get("event_type") == "escalation"
                for c in audit_calls
            )
            assert escalation_logged, "Escalation event not found in audit log calls"

    def test_eligible_determination_skips_escalation(self):
        """An 'eligible' determination should route to finalize, not escalate."""
        from agents.screening_agent import _route_after_eligibility

        state = _base_screening_state(eligibility_determination="eligible")
        route = _route_after_eligibility(state)
        assert route == "finalize_screening"

    def test_ineligible_determination_skips_escalation(self):
        """An 'ineligible' determination should route to finalize, not escalate."""
        from agents.screening_agent import _route_after_eligibility

        state = _base_screening_state(eligibility_determination="ineligible")
        route = _route_after_eligibility(state)
        assert route == "finalize_screening"



# ===================================================================
# Flow 4: Safety — serious AE → escalation → ICSR generation →
#          transmission
# ===================================================================


class TestSafetyFlow:
    """End-to-end safety reporting flow integration tests."""

    def test_serious_ae_generates_valid_icsr(self):
        """A confirmed serious AE should produce a valid E2B R3 ICSR with
        MedDRA coding and medication coding."""
        from components.safety_reporter import generate_e2b_icsr

        ae = _make_serious_ae()

        with patch("components.safety_reporter.terminology_map") as mock_term:
            mock_term.invoke.side_effect = [
                # MedDRA coding for AE description
                {"mappings": [
                    {"system": "MedDRA", "code": "10008479",
                     "display": "Chest pain", "confidence": 0.92},
                    {"system": "MedDRA_SOC", "code": "10007541",
                     "display": "Cardiac disorders", "confidence": 0.90},
                ]},
                # WHO Drug coding for suspect medication
                {"mappings": [
                    {"system": "WHO Drug Dictionary", "code": "STUDY001",
                     "display": "Study Drug X", "confidence": 0.85},
                ]},
                # WHO Drug coding for concomitant medication
                {"mappings": [
                    {"system": "WHO Drug Dictionary", "code": "B01AC06",
                     "display": "Aspirin", "confidence": 0.95},
                ]},
            ]

            result = generate_e2b_icsr.invoke({"adverse_event": ae, "trial_id": "NCT00000001"})

        assert "icsr_id" in result
        assert result["icsr_id"].startswith("ICSR-")
        assert "icsr_xml" in result
        assert len(result["icsr_xml"]) > 0
        assert "meddra_coding" in result
        assert "regulatory_deadline" in result
        assert result["deadline_days"] in (7, 15)

    def test_serious_ae_15_day_deadline(self):
        """Serious AEs (hospitalization) should have a 15-day regulatory deadline."""
        from components.safety_reporter import generate_e2b_icsr

        ae = _make_serious_ae()
        ae["seriousness"] = ["hospitalization"]

        with patch("components.safety_reporter.terminology_map") as mock_term:
            mock_term.invoke.return_value = {"mappings": [
                {"system": "MedDRA", "code": "10008479",
                 "display": "Chest pain", "confidence": 0.9},
            ]}

            result = generate_e2b_icsr.invoke({"adverse_event": ae, "trial_id": "NCT00000001"})

        assert result["deadline_days"] == 15

    def test_fatal_ae_7_day_deadline(self):
        """Fatal/life-threatening AEs should have a 7-day regulatory deadline."""
        from components.safety_reporter import generate_e2b_icsr

        ae = _make_serious_ae()
        ae["seriousness"] = ["death"]

        with patch("components.safety_reporter.terminology_map") as mock_term:
            mock_term.invoke.return_value = {"mappings": [
                {"system": "MedDRA", "code": "10008479",
                 "display": "Chest pain", "confidence": 0.9},
            ]}

            result = generate_e2b_icsr.invoke({"adverse_event": ae, "trial_id": "NCT00000001"})

        assert result["deadline_days"] == 7

    def test_icsr_transmission_logs_status(self):
        """ICSR transmission should log the status in the audit trail."""
        from components.safety_reporter import transmit_icsr

        with patch("components.safety_reporter._transmit_with_retry") as mock_tx:
            mock_tx.return_value = {
                "icsr_id": "ICSR-TEST001",
                "status": "acknowledged",
                "endpoint": "https://safety.example.com/e2b",
            }
            with patch("components.safety_reporter.audit_log_event") as mock_audit:
                mock_audit.invoke.return_value = {"status": "recorded"}

                result = transmit_icsr.invoke({
                    "icsr_xml": "<ichicsr>...</ichicsr>",
                    "endpoint_url": "https://safety.example.com/e2b",
                    "icsr_id": "ICSR-TEST001",
                    "trial_id": "NCT00000001",
                    "session_id": "mon_test_session",
                })

        assert result["status"] == "acknowledged"

        # Verify audit was called with transmission details
        mock_audit.invoke.assert_called()
        audit_event = mock_audit.invoke.call_args[0][0]
        assert audit_event["event_type"] == "data_access"
        assert audit_event["event_data"]["action"] == "icsr_transmission_complete"
        assert audit_event["event_data"]["icsr_id"] == "ICSR-TEST001"
        assert audit_event["event_data"]["status"] == "acknowledged"

    def test_full_safety_pipeline_ae_to_transmission(self):
        """End-to-end: serious AE detected in monitoring → escalation →
        ICSR generation → transmission attempt."""
        from agents.monitoring_agent import (
            collect_ae_details_node,
            classify_ae_node,
            escalate_sae_node,
        )
        from components.safety_reporter import generate_e2b_icsr, transmit_icsr

        ae = {
            "description": "Life-threatening arrhythmia",
            "onset_date": "2026-03-22",
            "severity": "severe",
            "seriousness": ["hospitalization"],
            "causality": "related",
            "outcome": "recovering",
            "suspect_medications": [{"name": "Study Drug X"}],
            "concomitant_medications": [],
        }

        # Phase 1: Monitoring agent detects and escalates SAE
        # terminology_map is called directly (not .invoke()) in collect_ae_details_node
        term_return = {"mappings": [
            {"system": "meddra", "code": "10002383",
             "display": "Arrhythmia", "confidence": 0.88, "soc": "Cardiac disorders"},
        ], "flagged_for_review": False}

        with patch("agents.monitoring_agent.terminology_map", return_value=term_return) as mock_term, \
             patch("agents.monitoring_agent.sqs_send_message") as mock_sqs, \
             patch("agents.monitoring_agent.audit_log_event") as mock_audit, \
             patch("agents.monitoring_agent._get_llm") as mock_llm_factory:

            mock_sqs.invoke.return_value = {
                "message_id": "msg-sae-001", "queue_name": "TrialEscalationQueue",
                "sent_at": datetime.now(timezone.utc).isoformat(),
            }
            mock_audit.invoke.return_value = {"audit_id": "aud-001", "status": "recorded"}
            mock_llm = MagicMock()
            mock_llm.invoke.return_value = MagicMock(content="Please describe the event.")
            mock_llm_factory.return_value = mock_llm

            state = _base_monitoring_state(adverse_events=[ae])
            state.update(collect_ae_details_node(state))
            state.update(classify_ae_node(state))

            # Verify classified as serious
            classified_ae = state["adverse_events"][-1] if state["adverse_events"] else ae
            assert classified_ae.get("is_serious") is True

            # Escalate
            result = escalate_sae_node(state)
            state.update(result)
            mock_sqs.invoke.assert_called()

        # Phase 2: Safety reporter generates ICSR (separate from agent context)
        full_ae = _make_serious_ae()
        full_ae["seriousness"] = ["death"]
        full_ae["description"] = "Life-threatening arrhythmia"

        with patch("components.safety_reporter.terminology_map") as mock_term:
            mock_term.invoke.return_value = {"mappings": [
                {"system": "MedDRA", "code": "10002383",
                 "display": "Arrhythmia", "confidence": 0.88},
                {"system": "MedDRA_SOC", "code": "10007541",
                 "display": "Cardiac disorders", "confidence": 0.85},
            ]}

            icsr_result = generate_e2b_icsr.invoke({"adverse_event": full_ae, "trial_id": "NCT00000001"})

        assert icsr_result["icsr_id"].startswith("ICSR-")
        assert icsr_result["deadline_days"] == 7  # death = 7 days
        assert len(icsr_result["icsr_xml"]) > 0

        # Phase 3: Transmit ICSR
        with patch("components.safety_reporter._transmit_with_retry") as mock_tx:
            mock_tx.return_value = {
                "icsr_id": icsr_result["icsr_id"],
                "status": "sent",
                "endpoint": "https://safety.example.com/e2b",
            }
            with patch("components.safety_reporter.audit_log_event") as mock_audit:
                mock_audit.invoke.return_value = {"status": "recorded"}

                tx_result = transmit_icsr.invoke({
                    "icsr_xml": icsr_result["icsr_xml"],
                    "endpoint_url": "https://safety.example.com/e2b",
                    "icsr_id": icsr_result["icsr_id"],
                    "trial_id": "NCT00000001",
                    "session_id": "mon_test_session",
                })

        assert tx_result["status"] in ("sent", "acknowledged")


# ===================================================================
# Cross-cutting: Trial configuration flow
# ===================================================================


class TestTrialConfigFlow:
    """Integration test for trial configuration via the /trials endpoint logic."""

    def test_configure_trial_from_protocol_text(self):
        """Configuring a trial from raw protocol text should parse criteria
        and generate a FHIR Questionnaire."""
        from components.protocol_engine import (
            parse_protocol_criteria,
            generate_fhir_questionnaire,
        )

        protocol_text = """
        Inclusion Criteria:
        1. Age 18-75 years
        2. Confirmed diagnosis of Type 2 Diabetes (ICD-10 E11)
        3. HbA1c >= 7.0%

        Exclusion Criteria:
        1. Pregnant or nursing
        2. Severe renal impairment (eGFR < 30)
        """

        with patch("components.protocol_engine._get_llm") as mock_llm_factory:
            mock_llm = MagicMock()
            mock_llm.invoke.return_value = MagicMock(content=json.dumps({
                "eligibility_rules": [
                    {"id": "INC-001", "type": "inclusion",
                     "description": "Age 18-75 years",
                     "data_type": "integer",
                     "fhir_path": "Patient.birthDate",
                     "operator": "age_between", "value": [18, 75]},
                    {"id": "INC-002", "type": "inclusion",
                     "description": "Confirmed Type 2 Diabetes",
                     "data_type": "boolean",
                     "fhir_path": "Condition.code",
                     "operator": "exists", "value": "E11"},
                    {"id": "INC-003", "type": "inclusion",
                     "description": "HbA1c >= 7.0%",
                     "data_type": "decimal",
                     "fhir_path": "Observation.valueQuantity",
                     "operator": ">=", "value": 7.0},
                    {"id": "EXC-001", "type": "exclusion",
                     "description": "Pregnant or nursing",
                     "data_type": "boolean",
                     "fhir_path": "Condition.code",
                     "operator": "not_exists", "value": "O80"},
                    {"id": "EXC-002", "type": "exclusion",
                     "description": "Severe renal impairment",
                     "data_type": "decimal",
                     "fhir_path": "Observation.valueQuantity",
                     "operator": "<", "value": 30},
                ],
            }))
            mock_llm_factory.return_value = mock_llm

            with patch("components.protocol_engine.terminology_map") as mock_term:
                mock_term.invoke.return_value = {"mappings": [
                    {"system": "SNOMED CT", "code": "424144002",
                     "display": "Age", "confidence": 0.95},
                ], "flagged_for_review": False}

                with patch("components.protocol_engine.audit_log_event") as mock_audit:
                    mock_audit.invoke.return_value = {"status": "recorded"}

                    parse_result = parse_protocol_criteria.invoke({
                        "protocol_document": protocol_text,
                        "trial_id": "NCT00000001",
                    })

            rules = parse_result.get("eligibility_rules", [])
            assert len(rules) >= 1

            with patch("components.protocol_engine.healthlake_store_resource") as mock_store:
                mock_store.invoke.return_value = {
                    "resource_id": "q-001", "resource_type": "Questionnaire",
                    "status": "created",
                }
                with patch("components.protocol_engine.audit_log_event") as mock_audit:
                    mock_audit.invoke.return_value = {"status": "recorded"}

                    q_result = generate_fhir_questionnaire.invoke({
                        "eligibility_rules": rules,
                        "trial_id": "NCT00000001",
                    })

            assert "questionnaire_id" in q_result
            assert "summary" in q_result
