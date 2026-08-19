"""Screening Agent — LangGraph StateGraph for adaptive eligibility screening.

Implements a 14-node state graph that conducts clinical trial eligibility
screening interviews. The agent loads patient FHIR data, walks through a
trial-specific FHIR Questionnaire, validates responses against medical records,
detects discrepancies, activates deep-dive modules via enableWhen logic,
resolves eligibility, escalates borderline cases, and persists the final
QuestionnaireResponse in HealthLake.

Nodes:
    load_patient, load_questionnaire, ask_question, validate_response,
    check_discrepancy, ask_clarification, check_enable_when, deep_dive_module,
    check_completion, prompt_missing, record_declined, resolve_eligibility,
    escalate, finalize_screening

Requirements: 2.1–2.7, 3.1–3.5, 4.1–4.5, 5.1–5.5, 6.1–6.4
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import boto3
from langgraph.graph import END, StateGraph

from components.eligibility_resolver import (
    deduplicate_fhir_resources,
    evaluate_criterion,
    resolve_eligibility as resolve_eligibility_tool,
)
from components.trial_config_store import get_trial_config
from config import settings
from config.settings import (
    AWS_REGION,
    ESCALATION_QUEUE,
)
from models.state import ScreeningState
from tools.audit import audit_log_event
from tools.ehr import ehr_fhir_query
from tools.healthlake import healthlake_query_patient, healthlake_store_resource
from tools.queues import sqs_send_message

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bedrock LLM helper
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Questionnaire item helpers
# ---------------------------------------------------------------------------


def _get_items_flat(questionnaire: dict) -> list[dict]:
    """Flatten all Questionnaire items (including nested group items) into a list."""
    items: list[dict] = []

    def _walk(item_list: list[dict]) -> None:
        for item in item_list:
            items.append(item)
            if item.get("type") == "group" and item.get("item"):
                _walk(item["item"])

    _walk(questionnaire.get("item", []))
    return items


def _get_required_items(questionnaire: dict) -> list[dict]:
    """Return only required, non-group items from the Questionnaire."""
    return [
        it for it in _get_items_flat(questionnaire)
        if it.get("required", False) and it.get("type") != "group"
    ]


def _get_current_item(state: ScreeningState) -> dict | None:
    """Return the Questionnaire item at the current index, or None if done."""
    items = _get_items_flat(state.get("questionnaire", {}))
    idx = state.get("current_item_index", 0)
    if 0 <= idx < len(items):
        return items[idx]
    return None


def _parse_eligibility_rule(item: dict) -> dict | None:
    """Extract the eligibility-rule extension from a Questionnaire item."""
    for ext in item.get("extension", []):
        if ext.get("url") == "http://example.org/fhir/StructureDefinition/eligibility-rule":
            try:
                return json.loads(ext["valueString"])
            except (json.JSONDecodeError, KeyError):
                return None
    return None


def _evaluate_enable_when(condition: dict, responses: list[dict]) -> bool:
    """Evaluate a single enableWhen condition against collected responses."""
    target_link_id = condition.get("question", "")
    operator = condition.get("operator", "=")

    for resp in responses:
        if resp.get("linkId") == target_link_id:
            answer = resp.get("answer")
            if operator == "=" and "answerBoolean" in condition:
                return answer == condition["answerBoolean"]
            if operator == "=" and "answerString" in condition:
                return str(answer).lower() == str(condition["answerString"]).lower()
            if operator == "!=":
                expected = condition.get("answerBoolean", condition.get("answerString"))
                return answer != expected
    return False


def _should_activate_item(item: dict, responses: list[dict]) -> bool:
    """Check whether an item's enableWhen conditions are satisfied."""
    enable_when = item.get("enableWhen")
    if not enable_when:
        return True  # no conditions → always active
    return all(_evaluate_enable_when(cond, responses) for cond in enable_when)



# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------


def _log_audit(
    state: ScreeningState,
    event_type: str,
    event_data: dict,
    fhir_refs: list[str] | None = None,
) -> None:
    """Fire-and-forget audit log for screening events."""
    try:
        audit_log_event.invoke({"event": {
            "session_id": state["session_id"],
            "event_type": event_type,
            "patient_id": state["patient_id"],
            "trial_id": state["trial_id"],
            "agent_id": "screening-agent",
            "user_identity": "screening-agent",
            "event_data": event_data,
            "fhir_resource_refs": fhir_refs or [],
        }})
    except Exception:
        logger.exception("Audit log failed for session %s", state.get("session_id"))


# ---------------------------------------------------------------------------
# Node 1: load_patient
# ---------------------------------------------------------------------------


def load_patient_node(state: ScreeningState) -> dict:
    """Retrieve patient FHIR records from HealthLake (and optionally EHR).

    Queries Patient, Condition, MedicationRequest, AllergyIntolerance,
    Procedure, and Observation resources.  If an external EHR endpoint is
    configured in the state, merges and deduplicates the two sources.
    """
    patient_id = state["patient_id"]

    result = healthlake_query_patient.invoke({
        "patient_id": patient_id,
        "resource_types": [
            "Patient", "Condition", "MedicationRequest",
            "AllergyIntolerance", "Procedure", "Observation",
        ],
    })

    fhir_data: dict = dict(result)

    # Optional external EHR merge
    ehr_endpoint = state.get("ehr_endpoint")  # type: ignore[typeddict-item]
    if ehr_endpoint:
        try:
            ehr_result = ehr_fhir_query.invoke({
                "patient_id": patient_id,
                "endpoint_url": ehr_endpoint,
            })
            if not ehr_result.get("ehr_query_failed"):
                fhir_data = deduplicate_fhir_resources(fhir_data, ehr_result)
        except Exception:
            logger.warning("EHR query failed for patient %s — using local data only", patient_id)

    _log_audit(state, "data_access", {
        "action": "load_patient",
        "resource_types_loaded": list(fhir_data.keys()),
    })

    return {"patient_fhir_data": fhir_data}


# ---------------------------------------------------------------------------
# Node 2: load_questionnaire
# ---------------------------------------------------------------------------


def load_questionnaire_node(state: ScreeningState) -> dict:
    """Load the trial-specific FHIR Questionnaire from HealthLake via ResearchStudy config."""
    from urllib.parse import urlencode
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.httpsession import URLLib3Session

    trial_id = state["trial_id"]
    questionnaire = {}

    try:
        # Step 1: Look up questionnaire ID from HealthLake ResearchStudy
        config = get_trial_config(trial_id)
        q_ref = config.get("questionnaire_id", "")  # e.g. "Questionnaire/abc-123"

        if q_ref:
            q_id = q_ref.split("/")[-1] if "/" in q_ref else q_ref
            # Step 2: Fetch the Questionnaire directly from HealthLake by ID
            session = boto3.Session(region_name=settings.AWS_REGION)
            creds = session.get_credentials().get_frozen_credentials()
            base = f"https://healthlake.{settings.AWS_REGION}.amazonaws.com/datastore/{settings.HEALTHLAKE_DATASTORE_ID}/r4"
            url = f"{base}/Questionnaire/{q_id}"
            req = AWSRequest(method="GET", url=url, headers={"Accept": "application/fhir+json"})
            SigV4Auth(creds, "healthlake", settings.AWS_REGION).add_auth(req)
            http = URLLib3Session()
            resp = http.send(req.prepare())
            if resp.status_code == 200:
                questionnaire = json.loads(resp.content.decode("utf-8"))
                logger.info("Loaded questionnaire %s for trial %s with %d items",
                            questionnaire.get("id", "?"), trial_id,
                            len(questionnaire.get("item", [])))
    except Exception:
        logger.exception("Failed to load questionnaire for trial %s", trial_id)

    # Fallback: search by identifier
    if not questionnaire or not questionnaire.get("item"):
        try:
            session = boto3.Session(region_name=settings.AWS_REGION)
            creds = session.get_credentials().get_frozen_credentials()
            base = f"https://healthlake.{settings.AWS_REGION}.amazonaws.com/datastore/{settings.HEALTHLAKE_DATASTORE_ID}/r4"
            url = f"{base}/Questionnaire?{urlencode({'identifier': f'https://clinicaltrials.gov|{trial_id}', '_count': '1'})}"
            req = AWSRequest(method="GET", url=url, headers={"Accept": "application/fhir+json"})
            SigV4Auth(creds, "healthlake", settings.AWS_REGION).add_auth(req)
            http = URLLib3Session()
            resp = http.send(req.prepare())
            if resp.status_code == 200:
                bundle = json.loads(resp.content.decode("utf-8"))
                entries = bundle.get("entry", [])
                if entries:
                    questionnaire = entries[0].get("resource", {})
        except Exception:
            logger.exception("Fallback questionnaire search failed for trial %s", trial_id)

    if not questionnaire or not questionnaire.get("item"):
        questionnaire = state.get("questionnaire", {})

    _log_audit(state, "data_access", {
        "action": "load_questionnaire",
        "questionnaire_id": questionnaire.get("id", ""),
        "item_count": len(questionnaire.get("item", [])),
    })

    return {
        "questionnaire": questionnaire,
        "current_item_index": 0,
        "responses": [],
        "discrepancies": [],
        "eligibility_criteria_results": [],
        "missing_items": [],
        "declined_items": [],
    }


# ---------------------------------------------------------------------------
# Node 3: ask_question
# ---------------------------------------------------------------------------

def ask_question_node(state: ScreeningState) -> dict:
    """Generate a conversational screening question for the current Questionnaire item."""
    item = _get_current_item(state)
    if not item:
        return {"messages": state.get("messages", [])}

    if not _should_activate_item(item, state.get("responses", [])):
        return {"current_item_index": state["current_item_index"] + 1}

    if item.get("type") == "group":
        return {"current_item_index": state["current_item_index"] + 1}

    # Generate a friendly, conversational question directly from the item text
    # (avoids PHI guardrail blocking by not using LLM for question generation)
    raw_text = item.get("text", "Please answer the following question.")
    link_id = item.get("linkId", "")
    item_type = item.get("type", "boolean")

    # Build conversational phrasing
    if item_type == "boolean":
        question_text = f"{raw_text} Please answer yes or no."
    elif item_type in ("decimal", "integer"):
        question_text = f"{raw_text} Please provide the numeric value."
    elif item_type == "choice":
        options = item.get("answerOption", [])
        option_labels = [o.get("valueCoding", {}).get("display", "") for o in options]
        if option_labels:
            question_text = f"{raw_text} Options: {', '.join(option_labels)}."
        else:
            question_text = raw_text
    else:
        question_text = raw_text

    new_messages = list(state.get("messages", []))
    new_messages.append({
        "role": "assistant",
        "content": question_text,
        "linkId": link_id,
    })

    _log_audit(state, "question_asked", {
        "linkId": link_id,
        "item_type": item_type,
    })

    return {"messages": new_messages}


# ---------------------------------------------------------------------------
# Node 4: validate_response
# ---------------------------------------------------------------------------

_VALID_TYPES = {
    "boolean": lambda v: str(v).lower() in ("true", "false", "yes", "no", "1", "0"),
    "integer": lambda v: str(v).lstrip("-").isdigit(),
    "decimal": lambda v: _is_decimal(v),
    "date": lambda v: _is_date(v),
    "string": lambda v: isinstance(v, str) and len(v.strip()) > 0,
    "choice": lambda v: isinstance(v, str) and len(v.strip()) > 0,
}


def _is_decimal(v: Any) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _is_date(v: Any) -> bool:
    if not isinstance(v, str):
        return False
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%m/%d/%Y"):
        try:
            datetime.strptime(v, fmt)
            return True
        except ValueError:
            continue
    return False


def _coerce_response(value: Any, item_type: str) -> Any:
    """Coerce a raw response value to the appropriate Python type."""
    if item_type == "boolean":
        return str(value).lower() in ("true", "yes", "1")
    if item_type == "integer":
        try:
            return int(float(str(value)))
        except (ValueError, TypeError):
            return 0
    if item_type == "decimal":
        try:
            return float(str(value))
        except (ValueError, TypeError):
            return 0.0
    return value





def validate_response_node(state: ScreeningState) -> dict:
    """Validate the latest patient response against Questionnaire item constraints.

    Extracts the structured value from natural language for validation
    while preserving the conversational text in messages.
    """
    import re

    item = _get_current_item(state)
    msgs = list(state.get("messages", []))

    if not item:
        return {"messages": msgs}

    last_msg = msgs[-1] if msgs else {}
    if last_msg.get("role") != "user":
        return {"messages": msgs}

    raw_value = last_msg.get("content", "")
    item_type = item.get("type", "string")

    # First try direct validation on the raw value
    validator = _VALID_TYPES.get(item_type, _VALID_TYPES["string"])
    if validator(raw_value):
        coerced = _coerce_response(raw_value, item_type)
        msgs[-1]["_valid"] = True
        msgs[-1]["_coerced"] = coerced

        new_responses = list(state.get("responses", []))
        new_responses.append({
            "linkId": item.get("linkId", ""),
            "answer": coerced,
            "raw": raw_value,
        })

        _log_audit(state, "response_received", {
            "linkId": item.get("linkId", ""),
            "valid": True,
        })

        return {"messages": msgs, "responses": new_responses}

    # Try NLP extraction for natural language responses
    text_lower = raw_value.strip().lower()

    if item_type == "boolean":
        yes_patterns = ["yes", "yeah", "yep", "correct", "that's right", "i have", "i do", "i was", "i've been", "unfortunately"]
        no_patterns = ["no", "nope", "not", "haven't", "don't", "doesn't apply", "i'm not", "never"]
        extracted = None
        for p in yes_patterns:
            if text_lower.startswith(p) or f" {p}" in text_lower[:30]:
                extracted = "true"
                break
        if extracted is None:
            for p in no_patterns:
                if text_lower.startswith(p) or f" {p}" in text_lower[:30]:
                    extracted = "false"
                    break
    elif item_type in ("integer", "decimal"):
        numbers = re.findall(r"[\d]+\.?[\d]*", raw_value)
        if numbers:
            extracted = numbers[0]
        elif text_lower in ("true", "false"):
            extracted = "1" if text_lower == "true" else "0"
        else:
            extracted = None
    else:
        extracted = raw_value

    # Validate the extracted value
    if extracted is not None and validator(extracted):
        coerced = _coerce_response(extracted, item_type)
        msgs[-1]["_valid"] = True
        msgs[-1]["_coerced"] = coerced

        new_responses = list(state.get("responses", []))
        new_responses.append({
            "linkId": item.get("linkId", ""),
            "answer": coerced,
            "raw": raw_value,
        })

        _log_audit(state, "response_received", {
            "linkId": item.get("linkId", ""),
            "valid": True,
        })

        return {"messages": msgs, "responses": new_responses}

    # Invalid response
    msgs[-1]["_valid"] = False
    return {"messages": msgs}


# ---------------------------------------------------------------------------
# Node 5: check_discrepancy
# ---------------------------------------------------------------------------


def check_discrepancy_node(state: ScreeningState) -> dict:
    """Compare the latest response with FHIR records via Eligibility Resolver."""
    item = _get_current_item(state)
    if not item:
        return {}

    responses = state.get("responses", [])
    if not responses:
        return {}

    latest_resp = responses[-1]
    patient_value = latest_resp.get("answer")

    result = evaluate_criterion.invoke({
        "criterion": item,
        "patient_response": patient_value,
        "fhir_data": state.get("patient_fhir_data", {}),
    })

    new_discrepancies = list(state.get("discrepancies", []))
    new_criteria_results = list(state.get("eligibility_criteria_results", []))
    new_criteria_results.append(result)

    discrepancy = result.get("discrepancy")
    if discrepancy:
        new_discrepancies.append(discrepancy)
        _log_audit(state, "validation_performed", {
            "action": "discrepancy_detected",
            "linkId": item.get("linkId", ""),
            "severity": discrepancy.get("severity", ""),
        })

    return {
        "discrepancies": new_discrepancies,
        "eligibility_criteria_results": new_criteria_results,
    }


# ---------------------------------------------------------------------------
# Node 6: ask_clarification
# ---------------------------------------------------------------------------


def ask_clarification_node(state: ScreeningState) -> dict:
    """Request clarification for an invalid or discrepant response.

    Uses generic phrasing — never reveals FHIR record content (Req 4.2).
    """
    item = _get_current_item(state)
    msgs = list(state.get("messages", []))

    last_msg = msgs[-1] if msgs else {}
    is_invalid = last_msg.get("_valid") is False

    if is_invalid:
        item_type = item.get("type", "string") if item else "string"
        clarification = (
            f"I didn't quite catch that. Could you provide your answer as a "
            f"{item_type} value? For example, "
        )
        if item_type == "boolean":
            clarification += "'yes' or 'no'."
        elif item_type == "integer":
            clarification += "a whole number like '42'."
        elif item_type == "decimal":
            clarification += "a number like '5.7'."
        elif item_type == "date":
            clarification += "a date like '2025-01-15'."
        else:
            clarification += "a short text answer."
    else:
        # Discrepancy-driven clarification — generic phrasing (Req 3.3, 4.2)
        clarification = (
            "I notice a difference between your response and our records. "
            "Could you double-check and clarify your answer?"
        )

    msgs.append({"role": "assistant", "content": clarification})

    _log_audit(state, "question_asked", {
        "action": "clarification_requested",
        "linkId": item.get("linkId", "") if item else "",
        "reason": "invalid_response" if is_invalid else "discrepancy",
    })

    return {"messages": msgs}


# ---------------------------------------------------------------------------
# Node 7: check_enable_when
# ---------------------------------------------------------------------------


def check_enable_when_node(state: ScreeningState) -> dict:
    """Evaluate enableWhen conditions, advance to next item, and check for deep-dive activation."""
    questionnaire = state.get("questionnaire", {})
    responses = state.get("responses", [])
    items = _get_items_flat(questionnaire)
    idx = state.get("current_item_index", 0)

    # Advance to the next item after processing the current one
    next_idx = idx + 1

    # Look ahead for group items with enableWhen that just became active
    for i in range(next_idx, len(items)):
        item = items[i]
        if item.get("type") == "group" and item.get("enableWhen"):
            if _should_activate_item(item, responses):
                link_id = item.get("linkId", "")
                module_name = link_id.replace("DD-", "").lower() if link_id.startswith("DD-") else None
                if module_name:
                    return {"deep_dive_active": module_name, "current_item_index": next_idx}

    return {"deep_dive_active": None, "current_item_index": next_idx}


# ---------------------------------------------------------------------------
# Node 8: deep_dive_module
# ---------------------------------------------------------------------------


def deep_dive_module_node(state: ScreeningState) -> dict:
    """Activate a deep-dive module — the graph will loop back to ask_question
    which naturally walks through the nested group items."""
    module = state.get("deep_dive_active")
    _log_audit(state, "decision_made", {
        "action": "deep_dive_activated",
        "module": module or "unknown",
    })
    # The current_item_index already points into the group's sub-items
    # via the flat item list, so we just advance to the next item.
    return {"current_item_index": state["current_item_index"] + 1}


# ---------------------------------------------------------------------------
# Node 9: check_completion
# ---------------------------------------------------------------------------


def check_completion_node(state: ScreeningState) -> dict:
    """Verify all required Questionnaire items have responses."""
    questionnaire = state.get("questionnaire", {})
    responses = state.get("responses", [])
    declined = state.get("declined_items", [])

    required_items = _get_required_items(questionnaire)
    answered_ids = {r["linkId"] for r in responses}
    declined_ids = {d["linkId"] for d in declined}

    missing = [
        it for it in required_items
        if it["linkId"] not in answered_ids
        and it["linkId"] not in declined_ids
        and _should_activate_item(it, responses)
    ]

    return {"missing_items": [{"linkId": it["linkId"], "text": it.get("text", "")} for it in missing]}


# ---------------------------------------------------------------------------
# Node 10: prompt_missing
# ---------------------------------------------------------------------------


def prompt_missing_node(state: ScreeningState) -> dict:
    """Prompt the patient for missing required items."""
    missing = state.get("missing_items", [])
    msgs = list(state.get("messages", []))

    if missing:
        first_missing = missing[0]
        prompt = (
            f"We still need your answer for: {first_missing.get('text', 'a required question')}. "
            f"Could you please provide your response? If you prefer not to answer, "
            f"just say 'decline'."
        )
        msgs.append({"role": "assistant", "content": prompt, "linkId": first_missing.get("linkId", "")})

        # Point current_item_index to this missing item
        items = _get_items_flat(state.get("questionnaire", {}))
        for i, it in enumerate(items):
            if it.get("linkId") == first_missing.get("linkId"):
                return {"messages": msgs, "current_item_index": i}

    return {"messages": msgs}


# ---------------------------------------------------------------------------
# Node 11: record_declined
# ---------------------------------------------------------------------------


def record_declined_node(state: ScreeningState) -> dict:
    """Record a declined item with timestamp and flag for PI review."""
    item = _get_current_item(state)
    new_declined = list(state.get("declined_items", []))

    if item:
        declined_entry = {
            "linkId": item.get("linkId", ""),
            "text": item.get("text", ""),
            "declined_at": datetime.now(timezone.utc).isoformat(),
            "flagged_for_pi_review": True,
        }
        new_declined.append(declined_entry)

        _log_audit(state, "decision_made", {
            "action": "item_declined",
            "linkId": item.get("linkId", ""),
        })

    # Remove this item from missing
    new_missing = [
        m for m in state.get("missing_items", [])
        if m.get("linkId") != (item.get("linkId", "") if item else "")
    ]

    return {
        "declined_items": new_declined,
        "missing_items": new_missing,
        "current_item_index": state["current_item_index"] + 1,
    }


# ---------------------------------------------------------------------------
# Node 12: resolve_eligibility
# ---------------------------------------------------------------------------


def resolve_eligibility_node(state: ScreeningState) -> dict:
    """Invoke the Eligibility Resolver for a final determination."""
    criteria_results = state.get("eligibility_criteria_results", [])

    result = resolve_eligibility_tool.invoke({"criteria_results": criteria_results})

    determination = result.get("determination", "borderline")

    _log_audit(state, "decision_made", {
        "action": "eligibility_resolved",
        "determination": determination,
        "criteria_count": len(criteria_results),
        "has_critical_discrepancies": result.get("has_critical_discrepancies", False),
    }, fhir_refs=result.get("fhir_refs", []))

    return {"eligibility_determination": determination}


# ---------------------------------------------------------------------------
# Node 13: escalate
# ---------------------------------------------------------------------------


def escalate_node(state: ScreeningState) -> dict:
    """Send borderline cases and safety signals to the Escalation Queue."""
    determination = state.get("eligibility_determination", "borderline")
    criteria_results = state.get("eligibility_criteria_results", [])
    discrepancies = state.get("discrepancies", [])

    # Determine urgency
    has_safety_signal = any(
        d.get("severity") == "critical" for d in discrepancies
    )
    urgency = "high" if has_safety_signal else "standard"

    escalation_message = {
        "patient_id": state["patient_id"],
        "trial_id": state["trial_id"],
        "session_id": state["session_id"],
        "escalation_reason": "borderline_eligibility",
        "urgency": urgency,
        "eligibility_determination": determination,
        "criteria_breakdown": criteria_results,
        "discrepancy_flags": discrepancies,
        "questionnaire_response_ref": f"QuestionnaireResponse/{state['session_id']}",
        "agent_reasoning": (
            f"Eligibility determination is '{determination}' with "
            f"{len(discrepancies)} discrepancies detected. "
            f"PI review required for final determination."
        ),
    }

    sqs_send_message.invoke({
        "queue_name": ESCALATION_QUEUE,
        "message": escalation_message,
        "attributes": {
            "Urgency": urgency,
            "EscalationReason": "borderline_eligibility",
            "TrialId": state["trial_id"],
            "PatientId": state["patient_id"],
        },
    })

    _log_audit(state, "escalation", {
        "action": "escalation_sent",
        "urgency": urgency,
        "determination": determination,
        "discrepancy_count": len(discrepancies),
    })

    return {}


# ---------------------------------------------------------------------------
# Node 14: finalize_screening
# ---------------------------------------------------------------------------


def finalize_screening_node(state: ScreeningState) -> dict:
    """Persist QuestionnaireResponse in HealthLake and log completion."""
    now = datetime.now(timezone.utc).isoformat()

    # Build FHIR QuestionnaireResponse
    qr_items = []
    for resp in state.get("responses", []):
        answer_key = "valueBoolean" if isinstance(resp.get("answer"), bool) else "valueString"
        qr_items.append({
            "linkId": resp["linkId"],
            "answer": [{answer_key: resp["answer"]}],
        })

    # Include declined items
    for dec in state.get("declined_items", []):
        qr_items.append({
            "linkId": dec["linkId"],
            "answer": [{"valueString": "declined"}],
            "extension": [{
                "url": "http://example.org/fhir/StructureDefinition/declined-item",
                "valueString": json.dumps({
                    "declined_at": dec.get("declined_at", now),
                    "flagged_for_pi_review": True,
                }),
            }],
        })

    questionnaire_response: dict = {
        "resourceType": "QuestionnaireResponse",
        "questionnaire": f"Questionnaire/{state.get('questionnaire', {}).get('id', '')}",
        "status": "completed",
        "subject": {"reference": f"Patient/{state['patient_id']}"},
        "authored": now,
        "author": {"display": "Screening Agent (LangGraph)"},
        "extension": [
            {
                "url": "http://example.org/fhir/StructureDefinition/trial-session",
                "valueString": json.dumps({
                    "session_id": state["session_id"],
                    "trial_id": state["trial_id"],
                    "agent_id": "screening-agent-v1",
                }),
            },
            {
                "url": "http://example.org/fhir/StructureDefinition/eligibility-determination",
                "valueString": json.dumps({
                    "determination": state.get("eligibility_determination", ""),
                    "criteria_results": state.get("eligibility_criteria_results", []),
                }),
            },
        ],
        "item": qr_items,
    }

    # Persist in HealthLake
    store_result = healthlake_store_resource.invoke({"resource": questionnaire_response})

    _log_audit(state, "decision_made", {
        "action": "screening_finalized",
        "determination": state.get("eligibility_determination", ""),
        "response_count": len(state.get("responses", [])),
        "declined_count": len(state.get("declined_items", [])),
        "store_status": store_result.get("status", "unknown"),
    })

    msgs = list(state.get("messages", []))
    msgs.append({
        "role": "assistant",
        "content": "Thank you for completing the screening questionnaire. Your responses have been recorded.",
    })

    return {"messages": msgs}


# ---------------------------------------------------------------------------
# Conditional edge routers
# ---------------------------------------------------------------------------


def _route_after_validate(state: ScreeningState) -> str:
    """Route after validate_response: invalid → clarify, declined → record, else → check discrepancy."""
    msgs = state.get("messages", [])
    if msgs:
        last = msgs[-1]
        if last.get("role") == "user" and last.get("_valid") is False:
            return "ask_clarification"
        content = str(last.get("content", "")).strip().lower()
        if content in ("decline", "declined", "skip", "prefer not to answer"):
            return "record_declined"
    return "check_discrepancy"


def _route_after_discrepancy(state: ScreeningState) -> str:
    """Route after check_discrepancy: critical → ask_clarification, else → check_enable_when."""
    discrepancies = state.get("discrepancies", [])
    if discrepancies:
        latest = discrepancies[-1]
        if latest.get("severity") == "critical":
            return "ask_clarification"
    return "check_enable_when"


def _route_after_enable_when(state: ScreeningState) -> str:
    """Route after check_enable_when: if more items remain, loop to ask_question; else check_completion."""
    if state.get("deep_dive_active"):
        return "deep_dive_module"
    items = _get_items_flat(state.get("questionnaire", {}))
    idx = state.get("current_item_index", 0)
    if idx < len(items):
        return "ask_question"
    return "check_completion"


def _route_after_completion(state: ScreeningState) -> str:
    """Route after check_completion: prompt for missing items, else resolve eligibility."""
    if state.get("missing_items"):
        return "prompt_missing"
    return "resolve_eligibility"


def _route_after_eligibility(state: ScreeningState) -> str:
    """Route after resolve_eligibility: borderline → escalate, else → finalize."""
    determination = state.get("eligibility_determination", "")
    if determination == "borderline":
        return "escalate"
    return "finalize_screening"


def _route_after_ask_question(state: ScreeningState) -> str:
    """Route after ask_question: if index advanced (skipped item), loop back; else wait for response."""
    # If the item was skipped (enableWhen not met or group), the node advanced the index
    # and we should loop back to ask_question for the next item.
    items = _get_items_flat(state.get("questionnaire", {}))
    idx = state.get("current_item_index", 0)
    if idx >= len(items):
        return "check_completion"
    item = items[idx]
    if item.get("type") == "group" or not _should_activate_item(item, state.get("responses", [])):
        return "ask_question"  # loop to skip
    return "validate_response"


def _route_after_clarification(state: ScreeningState) -> str:
    """After clarification, wait for the patient's next response → validate_response."""
    return "validate_response"


def _route_after_prompt_missing(state: ScreeningState) -> str:
    """After prompting for missing items, wait for response → validate_response."""
    return "validate_response"


def _route_after_deep_dive(state: ScreeningState) -> str:
    """After deep-dive activation, go to ask_question for the sub-items."""
    return "ask_question"


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------


def build_screening_graph() -> StateGraph:
    """Build and return the (uncompiled) Screening Agent StateGraph."""
    graph = StateGraph(ScreeningState)

    # Add all 14 nodes
    graph.add_node("load_patient", load_patient_node)
    graph.add_node("load_questionnaire", load_questionnaire_node)
    graph.add_node("ask_question", ask_question_node)
    graph.add_node("validate_response", validate_response_node)
    graph.add_node("check_discrepancy", check_discrepancy_node)
    graph.add_node("ask_clarification", ask_clarification_node)
    graph.add_node("check_enable_when", check_enable_when_node)
    graph.add_node("deep_dive_module", deep_dive_module_node)
    graph.add_node("check_completion", check_completion_node)
    graph.add_node("prompt_missing", prompt_missing_node)
    graph.add_node("record_declined", record_declined_node)
    graph.add_node("resolve_eligibility", resolve_eligibility_node)
    graph.add_node("escalate", escalate_node)
    graph.add_node("finalize_screening", finalize_screening_node)

    # Entry point
    graph.set_entry_point("load_patient")

    # Linear edges
    graph.add_edge("load_patient", "load_questionnaire")
    graph.add_edge("load_questionnaire", "ask_question")
    graph.add_edge("escalate", "finalize_screening")
    graph.add_edge("finalize_screening", END)

    # Conditional edges
    graph.add_conditional_edges("ask_question", _route_after_ask_question, {
        "validate_response": "validate_response",
        "check_completion": "check_completion",
        "ask_question": "ask_question",
    })

    graph.add_conditional_edges("validate_response", _route_after_validate, {
        "check_discrepancy": "check_discrepancy",
        "ask_clarification": "ask_clarification",
        "record_declined": "record_declined",
    })

    graph.add_conditional_edges("check_discrepancy", _route_after_discrepancy, {
        "ask_clarification": "ask_clarification",
        "check_enable_when": "check_enable_when",
    })

    graph.add_conditional_edges("check_enable_when", _route_after_enable_when, {
        "deep_dive_module": "deep_dive_module",
        "check_completion": "check_completion",
        "ask_question": "ask_question",
    })

    graph.add_conditional_edges("check_completion", _route_after_completion, {
        "prompt_missing": "prompt_missing",
        "resolve_eligibility": "resolve_eligibility",
    })

    graph.add_conditional_edges("resolve_eligibility", _route_after_eligibility, {
        "escalate": "escalate",
        "finalize_screening": "finalize_screening",
    })

    # Edges back into the loop
    graph.add_edge("ask_clarification", "validate_response")
    graph.add_edge("prompt_missing", "validate_response")
    graph.add_edge("deep_dive_module", "ask_question")
    graph.add_edge("record_declined", "check_completion")

    return graph


def compile_screening_graph():
    """Build and compile the Screening Agent graph with checkpointing."""
    graph = build_screening_graph()
    return graph.compile()

