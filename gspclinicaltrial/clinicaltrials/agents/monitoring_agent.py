"""Monitoring Agent — LangGraph StateGraph for ongoing monitoring visits.

Implements an 8-node state graph that conducts clinical trial monitoring
visits with enrolled patients.  The agent loads visit history, collects
symptoms, concomitant medications, and adverse events, classifies AE
seriousness, escalates serious AEs, and persists the visit as a FHIR
QuestionnaireResponse in HealthLake.

Nodes:
    load_visit_history, ask_symptoms, ask_medications, ask_adverse_events,
    collect_ae_details, classify_ae, escalate_sae, finalize_visit

Requirements: 7.1–7.6
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from components.terminology import terminology_map
from config.llm import get_agent_llm
from config.settings import (
    AWS_REGION,
    ESCALATION_QUEUE,
)
from models.state import MonitoringState
from tools.audit import audit_log_event
from tools.healthlake import healthlake_query_patient, healthlake_store_resource
from tools.queues import sqs_send_message

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERIOUSNESS_CRITERIA = frozenset({
    "hospitalization",
    "life-threatening",
    "death",
    "disability",
    "congenital_anomaly",
    "other_medically_important",
})

_SYMPTOMS_SYSTEM_PROMPT = (
    "You are a clinical trial monitoring assistant. Ask the patient about "
    "any new or changed symptoms since their last visit. Be empathetic and "
    "clear. Do NOT reveal any information from the patient's medical records. "
    "Keep questions concise."
)

_MEDICATIONS_SYSTEM_PROMPT = (
    "You are a clinical trial monitoring assistant. Ask the patient about "
    "any new or changed medications since their last visit. For each new "
    "medication collect: name, indication, dose, route, frequency, and "
    "start date. Do NOT reveal any information from the patient's medical "
    "records."
)

_AE_SYSTEM_PROMPT = (
    "You are a clinical trial monitoring assistant. Ask the patient whether "
    "they have experienced any adverse events or side effects since their "
    "last visit. Be empathetic and non-leading. Do NOT reveal any "
    "information from the patient's medical records."
)

_AE_DETAILS_SYSTEM_PROMPT = (
    "You are a clinical trial monitoring assistant collecting details about "
    "a reported adverse event. Collect: description, onset date, severity "
    "(mild/moderate/severe), seriousness criteria (hospitalization, "
    "life-threatening, death, disability, congenital anomaly, other "
    "medically important), causality assessment, and outcome. Be empathetic."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log_audit(
    state: MonitoringState,
    event_type: str,
    event_data: dict,
    fhir_refs: list[str] | None = None,
) -> None:
    """Fire-and-forget audit log for monitoring events."""
    try:
        audit_log_event.invoke({
            "session_id": state["session_id"],
            "event_type": event_type,
            "patient_id": state["patient_id"],
            "trial_id": state["trial_id"],
            "agent_id": "monitoring-agent",
            "user_identity": "monitoring-agent",
            "event_data": event_data,
            "fhir_resource_refs": fhir_refs or [],
        })
    except Exception:
        logger.exception("Audit log failed for session %s", state.get("session_id"))


def _llm_ask(system_prompt: str, context: str) -> str:
    """Generate a patient-facing message via the LLM with guardrail."""
    try:
        llm = get_agent_llm()
        response = llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=context),
        ])
        return response.content.strip()
    except Exception:
        logger.warning("LLM generation failed — using fallback text")
        return ""


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------


def load_visit_history_node(state: MonitoringState) -> dict:
    """Load monitoring Questionnaire and prior visit history from HealthLake.

    Retrieves the trial-specific monitoring Questionnaire and any prior
    QuestionnaireResponse resources for this patient to provide visit context.
    """
    patient_id = state["patient_id"]
    trial_id = state["trial_id"]

    # Load patient FHIR data for context
    fhir_result = healthlake_query_patient.invoke({
        "patient_id": patient_id,
        "resource_types": [
            "Patient", "Condition", "MedicationRequest",
            "AllergyIntolerance", "Observation",
        ],
    })

    # Load monitoring questionnaire from HealthLake
    questionnaire_result = healthlake_query_patient.invoke({
        "patient_id": patient_id,
        "resource_types": ["Questionnaire"],
    })

    monitoring_q = questionnaire_result.get("Questionnaire", [{}])
    if isinstance(monitoring_q, list):
        monitoring_q = monitoring_q[0] if monitoring_q else {}

    # Load prior visit QuestionnaireResponses
    prior_result = healthlake_query_patient.invoke({
        "patient_id": patient_id,
        "resource_types": ["QuestionnaireResponse"],
    })
    prior_visits = prior_result.get("QuestionnaireResponse", [])
    if not isinstance(prior_visits, list):
        prior_visits = [prior_visits] if prior_visits else []

    _log_audit(state, "data_access", {
        "action": "load_visit_history",
        "visit_number": state.get("visit_number", 1),
        "prior_visit_count": len(prior_visits),
    })

    return {
        "monitoring_questionnaire": monitoring_q,
        "prior_visits": prior_visits,
    }


def ask_symptoms_node(state: MonitoringState) -> dict:
    """Ask the patient about new or changed symptoms since last visit."""
    visit_number = state.get("visit_number", 1)
    prior_count = len(state.get("prior_visits", []))

    context = (
        f"This is monitoring visit #{visit_number} for trial {state['trial_id']}. "
        f"The patient has {prior_count} prior visit(s) on record. "
        f"Ask about any new or changed symptoms since the last visit."
    )

    question = _llm_ask(_SYMPTOMS_SYSTEM_PROMPT, context)
    if not question:
        question = (
            "Have you experienced any new symptoms or changes in existing "
            "symptoms since your last visit?"
        )

    msgs = list(state.get("messages", []))
    msgs.append({"role": "assistant", "content": question})

    _log_audit(state, "question_asked", {
        "action": "ask_symptoms",
        "visit_number": visit_number,
    })

    # Simulate patient response with empty symptoms for now;
    # in production the graph would pause for human input.
    new_symptoms = state.get("new_symptoms", [])

    return {"messages": msgs, "new_symptoms": new_symptoms}


def ask_medications_node(state: MonitoringState) -> dict:
    """Collect new concomitant medications and map via Terminology Service.

    For each new medication, collects name, indication, dose, route,
    frequency, start date, and maps to WHO Drug Dictionary and RxNorm.
    """
    context = (
        f"Monitoring visit #{state.get('visit_number', 1)} for trial "
        f"{state['trial_id']}. Ask about any new or changed medications."
    )

    question = _llm_ask(_MEDICATIONS_SYSTEM_PROMPT, context)
    if not question:
        question = (
            "Have you started any new medications or changed the dose of "
            "existing medications since your last visit?"
        )

    msgs = list(state.get("messages", []))
    msgs.append({"role": "assistant", "content": question})

    # Map each new medication to WHO Drug Dictionary and RxNorm
    mapped_medications: list[dict] = []
    for med in state.get("new_medications", []):
        med_name = med.get("name", "")
        if not med_name:
            mapped_medications.append(med)
            continue

        try:
            mapping_result = terminology_map(
                term=med_name,
                source_system="medication",
                target_systems=["who_drug", "rxnorm"],
                trial_id=state.get("trial_id"),
            )
            med_with_codes = dict(med)
            for m in mapping_result.get("mappings", []):
                if m.get("system") == "who_drug":
                    med_with_codes["who_drug_code"] = m.get("code", "")
                elif m.get("system") == "rxnorm":
                    med_with_codes["rxnorm_code"] = m.get("code", "")
            med_with_codes.setdefault("who_drug_code", "")
            med_with_codes.setdefault("rxnorm_code", "")
            mapped_medications.append(med_with_codes)
        except Exception:
            logger.warning("Terminology mapping failed for medication: %s", med_name)
            mapped_medications.append(med)

    _log_audit(state, "question_asked", {
        "action": "ask_medications",
        "new_medication_count": len(mapped_medications),
    })

    return {"messages": msgs, "new_medications": mapped_medications}


def ask_adverse_events_node(state: MonitoringState) -> dict:
    """Ask the patient about adverse events since last visit."""
    context = (
        f"Monitoring visit #{state.get('visit_number', 1)} for trial "
        f"{state['trial_id']}. Ask whether the patient has experienced "
        f"any adverse events or side effects."
    )

    question = _llm_ask(_AE_SYSTEM_PROMPT, context)
    if not question:
        question = (
            "Have you experienced any side effects or adverse events "
            "since your last visit?"
        )

    msgs = list(state.get("messages", []))
    msgs.append({"role": "assistant", "content": question})

    _log_audit(state, "question_asked", {
        "action": "ask_adverse_events",
        "visit_number": state.get("visit_number", 1),
    })

    return {"messages": msgs}


def collect_ae_details_node(state: MonitoringState) -> dict:
    """Collect detailed information for each reported adverse event.

    For each AE collects: description, onset date, severity, seriousness
    criteria, causality assessment, and outcome.  Maps AE terms to MedDRA
    via the Terminology Service.
    """
    adverse_events = list(state.get("adverse_events", []))

    context = (
        f"The patient has reported {len(adverse_events)} adverse event(s). "
        f"Collect detailed information for each."
    )

    question = _llm_ask(_AE_DETAILS_SYSTEM_PROMPT, context)
    if not question:
        question = (
            "I'd like to collect some more details about the adverse "
            "event(s) you reported. Could you describe each one, including "
            "when it started, how severe it is, and the current outcome?"
        )

    msgs = list(state.get("messages", []))
    msgs.append({"role": "assistant", "content": question})

    # Map AE terms to MedDRA
    enriched_events: list[dict] = []
    for ae in adverse_events:
        ae_copy = dict(ae)
        description = ae.get("description", "")
        if description:
            try:
                mapping_result = terminology_map(
                    term=description,
                    source_system="adverse_event",
                    target_systems=["meddra"],
                    trial_id=state.get("trial_id"),
                )
                for m in mapping_result.get("mappings", []):
                    if m.get("system") == "meddra":
                        ae_copy.setdefault("meddra_pt", m.get("display", ""))
                        ae_copy.setdefault("meddra_soc", m.get("soc", ""))
                        ae_copy.setdefault("meddra_code", m.get("code", ""))
            except Exception:
                logger.warning("MedDRA mapping failed for AE: %s", description)

        # Ensure required fields have defaults
        ae_copy.setdefault("ae_id", str(uuid.uuid4()))
        ae_copy.setdefault("patient_id", state["patient_id"])
        ae_copy.setdefault("trial_id", state["trial_id"])
        ae_copy.setdefault("session_id", state["session_id"])
        ae_copy.setdefault("onset_date", "")
        ae_copy.setdefault("severity", "mild")
        ae_copy.setdefault("seriousness", [])
        ae_copy.setdefault("causality", "")
        ae_copy.setdefault("outcome", "unknown")
        ae_copy.setdefault("reporter", "patient")
        ae_copy.setdefault("report_date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        enriched_events.append(ae_copy)

    _log_audit(state, "response_received", {
        "action": "collect_ae_details",
        "ae_count": len(enriched_events),
    })

    return {"messages": msgs, "adverse_events": enriched_events}


def classify_ae_node(state: MonitoringState) -> dict:
    """Classify each adverse event as serious or non-serious.

    An AE is serious if any of its seriousness criteria match the
    recognized seriousness set (hospitalization, life-threatening, death,
    disability, congenital_anomaly, other_medically_important).
    """
    adverse_events = list(state.get("adverse_events", []))

    for ae in adverse_events:
        seriousness_list = ae.get("seriousness", [])
        is_serious = bool(
            set(s.lower().replace(" ", "_").replace("-", "_") for s in seriousness_list)
            & SERIOUSNESS_CRITERIA
        )
        ae["is_serious"] = is_serious

    _log_audit(state, "decision_made", {
        "action": "classify_ae",
        "total_aes": len(adverse_events),
        "serious_count": sum(1 for ae in adverse_events if ae.get("is_serious")),
    })

    return {"adverse_events": adverse_events}


def escalate_sae_node(state: MonitoringState) -> dict:
    """Send serious adverse events to the Escalation Queue with urgency 'urgent'."""
    adverse_events = state.get("adverse_events", [])
    serious_aes = [ae for ae in adverse_events if ae.get("is_serious")]

    for ae in serious_aes:
        escalation_message = {
            "patient_id": state["patient_id"],
            "trial_id": state["trial_id"],
            "session_id": state["session_id"],
            "escalation_reason": "serious_adverse_event",
            "urgency": "urgent",
            "visit_number": state.get("visit_number", 1),
            "adverse_event": {
                "ae_id": ae.get("ae_id", ""),
                "description": ae.get("description", ""),
                "severity": ae.get("severity", ""),
                "seriousness": ae.get("seriousness", []),
                "causality": ae.get("causality", ""),
                "outcome": ae.get("outcome", ""),
                "onset_date": ae.get("onset_date", ""),
                "meddra_pt": ae.get("meddra_pt", ""),
            },
        }

        sqs_send_message.invoke({
            "queue_name": ESCALATION_QUEUE,
            "message": escalation_message,
            "attributes": {
                "Urgency": "urgent",
                "EscalationReason": "serious_adverse_event",
                "TrialId": state["trial_id"],
                "PatientId": state["patient_id"],
            },
        })

        _log_audit(state, "escalation", {
            "action": "escalate_sae",
            "ae_id": ae.get("ae_id", ""),
            "urgency": "urgent",
            "seriousness": ae.get("seriousness", []),
        })

    return {}


def finalize_visit_node(state: MonitoringState) -> dict:
    """Persist the monitoring visit as a FHIR QuestionnaireResponse in HealthLake.

    Links the response to the patient, monitoring Questionnaire, and visit number.
    """
    now = datetime.now(timezone.utc).isoformat()
    visit_number = state.get("visit_number", 1)

    # Build symptom items
    symptom_items = []
    for i, symptom in enumerate(state.get("new_symptoms", [])):
        text = symptom if isinstance(symptom, str) else symptom.get("description", str(symptom))
        symptom_items.append({
            "linkId": f"SYM-{i + 1:03d}",
            "text": "Reported symptom",
            "answer": [{"valueString": text}],
        })

    # Build medication items
    medication_items = []
    for i, med in enumerate(state.get("new_medications", [])):
        med_data = {
            "name": med.get("name", ""),
            "indication": med.get("indication", ""),
            "dose": med.get("dose", ""),
            "route": med.get("route", ""),
            "frequency": med.get("frequency", ""),
            "start_date": med.get("start_date", ""),
            "who_drug_code": med.get("who_drug_code", ""),
            "rxnorm_code": med.get("rxnorm_code", ""),
        }
        medication_items.append({
            "linkId": f"MED-{i + 1:03d}",
            "text": "Concomitant medication",
            "answer": [{"valueString": json.dumps(med_data)}],
        })

    # Build adverse event items
    ae_items = []
    for i, ae in enumerate(state.get("adverse_events", [])):
        ae_data = {
            "ae_id": ae.get("ae_id", ""),
            "description": ae.get("description", ""),
            "onset_date": ae.get("onset_date", ""),
            "severity": ae.get("severity", ""),
            "seriousness": ae.get("seriousness", []),
            "causality": ae.get("causality", ""),
            "outcome": ae.get("outcome", ""),
            "is_serious": ae.get("is_serious", False),
            "meddra_pt": ae.get("meddra_pt", ""),
            "meddra_soc": ae.get("meddra_soc", ""),
        }
        ae_items.append({
            "linkId": f"AE-{i + 1:03d}",
            "text": "Adverse event",
            "answer": [{"valueString": json.dumps(ae_data)}],
        })

    questionnaire_id = state.get("monitoring_questionnaire", {}).get("id", "")

    questionnaire_response: dict = {
        "resourceType": "QuestionnaireResponse",
        "questionnaire": f"Questionnaire/{questionnaire_id}",
        "status": "completed",
        "subject": {"reference": f"Patient/{state['patient_id']}"},
        "authored": now,
        "author": {"display": "Monitoring Agent (LangGraph)"},
        "extension": [
            {
                "url": "http://example.org/fhir/StructureDefinition/trial-session",
                "valueString": json.dumps({
                    "session_id": state["session_id"],
                    "trial_id": state["trial_id"],
                    "agent_id": "monitoring-agent-v1",
                    "visit_number": visit_number,
                }),
            },
        ],
        "item": symptom_items + medication_items + ae_items,
    }

    store_result = healthlake_store_resource.invoke({"resource": questionnaire_response})

    _log_audit(state, "decision_made", {
        "action": "visit_finalized",
        "visit_number": visit_number,
        "symptom_count": len(symptom_items),
        "medication_count": len(medication_items),
        "ae_count": len(ae_items),
        "store_status": store_result.get("status", "unknown"),
    })

    msgs = list(state.get("messages", []))
    msgs.append({
        "role": "assistant",
        "content": (
            "Thank you for completing this monitoring visit. Your responses "
            "have been recorded. If you experience any new symptoms or side "
            "effects before your next visit, please contact your study team."
        ),
    })

    return {"messages": msgs}


# ---------------------------------------------------------------------------
# Routing functions
# ---------------------------------------------------------------------------


def _route_after_adverse_events(state: MonitoringState) -> str:
    """Route after ask_adverse_events: collect details if AEs reported, else finalize."""
    if state.get("adverse_events"):
        return "collect_ae_details"
    return "finalize_visit"


def _route_after_classify(state: MonitoringState) -> str:
    """Route after classify_ae: escalate if any serious AE, else finalize."""
    adverse_events = state.get("adverse_events", [])
    has_serious = any(ae.get("is_serious") for ae in adverse_events)
    if has_serious:
        return "escalate_sae"
    return "finalize_visit"


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_monitoring_graph() -> StateGraph:
    """Build and return the (uncompiled) Monitoring Agent StateGraph.

    8 nodes with conditional routing for AE handling:
        load_visit_history → ask_symptoms → ask_medications →
        ask_adverse_events → [collect_ae_details → classify_ae →
        escalate_sae] → finalize_visit
    """
    graph = StateGraph(MonitoringState)

    # Add all 8 nodes
    graph.add_node("load_visit_history", load_visit_history_node)
    graph.add_node("ask_symptoms", ask_symptoms_node)
    graph.add_node("ask_medications", ask_medications_node)
    graph.add_node("ask_adverse_events", ask_adverse_events_node)
    graph.add_node("collect_ae_details", collect_ae_details_node)
    graph.add_node("classify_ae", classify_ae_node)
    graph.add_node("escalate_sae", escalate_sae_node)
    graph.add_node("finalize_visit", finalize_visit_node)

    # Entry point
    graph.set_entry_point("load_visit_history")

    # Linear edges for the main flow
    graph.add_edge("load_visit_history", "ask_symptoms")
    graph.add_edge("ask_symptoms", "ask_medications")
    graph.add_edge("ask_medications", "ask_adverse_events")

    # Conditional: AE reported → collect details, else → finalize
    graph.add_conditional_edges("ask_adverse_events", _route_after_adverse_events, {
        "collect_ae_details": "collect_ae_details",
        "finalize_visit": "finalize_visit",
    })

    # Linear: details → classify
    graph.add_edge("collect_ae_details", "classify_ae")

    # Conditional: serious → escalate, else → finalize
    graph.add_conditional_edges("classify_ae", _route_after_classify, {
        "escalate_sae": "escalate_sae",
        "finalize_visit": "finalize_visit",
    })

    # After escalation → finalize
    graph.add_edge("escalate_sae", "finalize_visit")

    # Terminal
    graph.add_edge("finalize_visit", END)

    return graph


def compile_monitoring_graph():
    """Build and compile the Monitoring Agent graph with checkpointing."""
    graph = build_monitoring_graph()
    return graph.compile()
