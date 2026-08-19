"""Property-based tests for the Screening Agent (LangGraph StateGraph).

Properties tested:
  P5  — Patient FHIR Data Retrieval Completeness
  P6  — Response Validation Against Questionnaire Constraints
  P7  — EnableWhen Conditional Activation
  P8  — Screening Completeness Invariant
  P10 — PHI Non-Disclosure in Agent Outputs
  P12 — Blinding Enforcement
  P14 — QuestionnaireResponse Persistence Integrity
  P16 — Escalation Message Completeness
  P17 — Safety Signal Escalation
  P18 — Escalation Audit Trail

Validates Requirements: 2.1–2.7, 3.2–3.5, 4.1–4.3, 5.1–5.4, 6.1–6.5
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from agents.screening_agent import (
    _evaluate_enable_when,
    _get_items_flat,
    _get_required_items,
    _should_activate_item,
    ask_clarification_node,
    ask_question_node,
    check_completion_node,
    check_discrepancy_node,
    check_enable_when_node,
    escalate_node,
    finalize_screening_node,
    load_patient_node,
    record_declined_node,
    resolve_eligibility_node,
    validate_response_node,
)
from models.state import ScreeningState

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_link_id_st = st.from_regex(r"IE-[0-9]{3}", fullmatch=True)
_item_type_st = st.sampled_from(["boolean", "integer", "decimal", "date", "string"])
_uuid_st = st.uuids().map(str)
_nct_st = st.from_regex(r"NCT[0-9]{8}", fullmatch=True)
_session_st = st.from_regex(r"scr_[0-9]{10}", fullmatch=True)

_REQUIRED_FHIR_TYPES = [
    "Patient", "Condition", "MedicationRequest",
    "AllergyIntolerance", "Procedure", "Observation",
]

_PHI_PATTERNS = [
    r"\b\d{3}-\d{2}-\d{4}\b",          # SSN
    r"\b\d{2}/\d{2}/\d{4}\b",          # DOB format
    r"\bMRN[:\s]*\d+\b",               # MRN
]

_TREATMENT_ARM_TERMS = [
    "treatment arm", "arm a", "arm b", "placebo arm", "active arm",
    "control group", "experimental group", "drug arm", "comparator arm",
]


def _base_state(**overrides) -> ScreeningState:
    """Build a minimal valid ScreeningState with optional overrides."""
    state: dict[str, Any] = {
        "trial_id": "NCT00000001",
        "patient_id": "patient-001",
        "session_id": "scr_1710500000",
        "questionnaire": {},
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
    return state  # type: ignore[return-value]


def _make_questionnaire_item(
    link_id: str,
    item_type: str = "boolean",
    required: bool = True,
    enable_when: list[dict] | None = None,
    text: str | None = None,
) -> dict:
    """Build a minimal FHIR Questionnaire item."""
    item: dict[str, Any] = {
        "linkId": link_id,
        "text": text or f"Question for {link_id}",
        "type": item_type,
        "required": required,
    }
    if enable_when:
        item["enableWhen"] = enable_when
    return item


def _make_questionnaire(items: list[dict]) -> dict:
    """Wrap items into a FHIR Questionnaire resource."""
    return {
        "resourceType": "Questionnaire",
        "id": "trial-NCT00000001-screening-v1",
        "status": "active",
        "item": items,
    }


def _questionnaire_item_st(
    link_id: str | None = None,
    item_type: str | None = None,
    required: bool = True,
):
    """Strategy for a single Questionnaire item."""
    return st.builds(
        _make_questionnaire_item,
        link_id=st.just(link_id) if link_id else _link_id_st,
        item_type=st.just(item_type) if item_type else _item_type_st,
        required=st.just(required),
    )


# ===================================================================
# Property 5 — Patient FHIR Data Retrieval Completeness
# Verify all required resource types retrieved.
# Validates: Requirements 2.1, 3.1
# ===================================================================


@given(patient_id=_uuid_st, trial_id=_nct_st, session_id=_session_st)
@settings(max_examples=30)
def test_p5_all_required_resource_types_retrieved(
    patient_id: str, trial_id: str, session_id: str
):
    """load_patient_node must query all 6 required FHIR resource types and
    return them in patient_fhir_data."""
    mock_result = {rt: [{"resourceType": rt, "id": f"{rt}-1"}] for rt in _REQUIRED_FHIR_TYPES}

    with patch("agents.screening_agent.healthlake_query_patient") as mock_hl:
        mock_hl.invoke.return_value = mock_result
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}

            state = _base_state(
                patient_id=patient_id, trial_id=trial_id, session_id=session_id,
            )
            result = load_patient_node(state)

    fhir_data = result["patient_fhir_data"]
    for rt in _REQUIRED_FHIR_TYPES:
        assert rt in fhir_data, f"Missing resource type {rt} in patient_fhir_data"
        assert len(fhir_data[rt]) >= 1, f"No resources for {rt}"


@given(patient_id=_uuid_st)
@settings(max_examples=20)
def test_p5_query_includes_all_resource_types(patient_id: str):
    """The HealthLake query must request all 6 required resource types."""
    with patch("agents.screening_agent.healthlake_query_patient") as mock_hl:
        mock_hl.invoke.return_value = {rt: [] for rt in _REQUIRED_FHIR_TYPES}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}

            state = _base_state(patient_id=patient_id)
            load_patient_node(state)

    call_args = mock_hl.invoke.call_args[0][0]
    requested_types = set(call_args["resource_types"])
    for rt in _REQUIRED_FHIR_TYPES:
        assert rt in requested_types, f"Resource type {rt} not requested"


@given(patient_id=_uuid_st)
@settings(max_examples=20)
def test_p5_ehr_failure_falls_back_to_local(patient_id: str):
    """When EHR query fails, load_patient_node must still return local data."""
    local_data = {rt: [{"resourceType": rt, "id": f"local-{rt}"}] for rt in _REQUIRED_FHIR_TYPES}

    with patch("agents.screening_agent.healthlake_query_patient") as mock_hl:
        mock_hl.invoke.return_value = local_data
        with patch("agents.screening_agent.ehr_fhir_query") as mock_ehr:
            mock_ehr.invoke.side_effect = Exception("EHR unavailable")
            with patch("agents.screening_agent.audit_log_event") as mock_audit:
                mock_audit.invoke.return_value = {"status": "recorded"}

                state = _base_state(patient_id=patient_id, ehr_endpoint="https://ehr.example.com")
                result = load_patient_node(state)

    fhir_data = result["patient_fhir_data"]
    for rt in _REQUIRED_FHIR_TYPES:
        assert rt in fhir_data, f"Missing {rt} after EHR failure"

# ===================================================================
# Property 6 — Response Validation Against Questionnaire Constraints
# Verify valid responses accepted, invalid rejected.
# Validates: Requirements 2.4, 2.6
# ===================================================================


@given(link_id=_link_id_st)
@settings(max_examples=30)
def test_p6_valid_boolean_accepted(link_id: str):
    """Boolean items must accept 'true', 'false', 'yes', 'no', '1', '0'."""
    item = _make_questionnaire_item(link_id, "boolean")
    questionnaire = _make_questionnaire([item])

    for value in ("true", "false", "yes", "no", "1", "0"):
        state = _base_state(
            questionnaire=questionnaire,
            current_item_index=0,
            messages=[{"role": "user", "content": value}],
        )
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            result = validate_response_node(state)

        last_msg = result["messages"][-1]
        assert last_msg.get("_valid") is True, (
            f"Boolean value '{value}' should be accepted for {link_id}"
        )


@given(
    link_id=_link_id_st,
    bad_value=st.text(min_size=3, max_size=10).filter(
        lambda v: v.lower() not in ("true", "false", "yes", "no", "1", "0")
    ),
)
@settings(max_examples=30)
def test_p6_invalid_boolean_rejected(link_id: str, bad_value: str):
    """Non-boolean text must be rejected for boolean items."""
    assume(not bad_value.strip().lstrip("-").isdigit())

    item = _make_questionnaire_item(link_id, "boolean")
    questionnaire = _make_questionnaire([item])

    state = _base_state(
        questionnaire=questionnaire,
        current_item_index=0,
        messages=[{"role": "user", "content": bad_value}],
    )
    with patch("agents.screening_agent.audit_log_event") as mock_audit:
        mock_audit.invoke.return_value = {"status": "recorded"}
        result = validate_response_node(state)

    last_msg = result["messages"][-1]
    assert last_msg.get("_valid") is False, (
        f"Non-boolean '{bad_value}' should be rejected"
    )


@given(link_id=_link_id_st, value=st.integers(min_value=-1000, max_value=1000))
@settings(max_examples=30)
def test_p6_valid_integer_accepted(link_id: str, value: int):
    """Integer items must accept valid integer strings."""
    item = _make_questionnaire_item(link_id, "integer")
    questionnaire = _make_questionnaire([item])

    state = _base_state(
        questionnaire=questionnaire,
        current_item_index=0,
        messages=[{"role": "user", "content": str(value)}],
    )
    with patch("agents.screening_agent.audit_log_event") as mock_audit:
        mock_audit.invoke.return_value = {"status": "recorded"}
        result = validate_response_node(state)

    last_msg = result["messages"][-1]
    assert last_msg.get("_valid") is True, (
        f"Integer '{value}' should be accepted"
    )


@given(link_id=_link_id_st)
@settings(max_examples=20)
def test_p6_invalid_integer_rejected(link_id: str):
    """Non-numeric text must be rejected for integer items."""
    item = _make_questionnaire_item(link_id, "integer")
    questionnaire = _make_questionnaire([item])

    for bad in ("abc", "12.5", "yes", ""):
        state = _base_state(
            questionnaire=questionnaire,
            current_item_index=0,
            messages=[{"role": "user", "content": bad}],
        )
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            result = validate_response_node(state)

        last_msg = result["messages"][-1]
        assert last_msg.get("_valid") is False, (
            f"Non-integer '{bad}' should be rejected for integer item"
        )


@given(
    link_id=_link_id_st,
    value=st.floats(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=30)
def test_p6_valid_decimal_accepted(link_id: str, value: float):
    """Decimal items must accept valid numeric strings."""
    item = _make_questionnaire_item(link_id, "decimal")
    questionnaire = _make_questionnaire([item])

    state = _base_state(
        questionnaire=questionnaire,
        current_item_index=0,
        messages=[{"role": "user", "content": str(value)}],
    )
    with patch("agents.screening_agent.audit_log_event") as mock_audit:
        mock_audit.invoke.return_value = {"status": "recorded"}
        result = validate_response_node(state)

    last_msg = result["messages"][-1]
    assert last_msg.get("_valid") is True, (
        f"Decimal '{value}' should be accepted"
    )


@given(link_id=_link_id_st)
@settings(max_examples=20)
def test_p6_valid_date_accepted(link_id: str):
    """Date items must accept ISO and common date formats."""
    item = _make_questionnaire_item(link_id, "date")
    questionnaire = _make_questionnaire([item])

    for date_str in ("2025-01-15", "2025-01-15T10:30:00Z", "01/15/2025"):
        state = _base_state(
            questionnaire=questionnaire,
            current_item_index=0,
            messages=[{"role": "user", "content": date_str}],
        )
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            result = validate_response_node(state)

        last_msg = result["messages"][-1]
        assert last_msg.get("_valid") is True, (
            f"Date '{date_str}' should be accepted"
        )


@given(link_id=_link_id_st)
@settings(max_examples=20)
def test_p6_invalid_date_rejected(link_id: str):
    """Malformed date strings must be rejected for date items."""
    item = _make_questionnaire_item(link_id, "date")
    questionnaire = _make_questionnaire([item])

    for bad in ("not-a-date", "2025/13/40", "yesterday", ""):
        state = _base_state(
            questionnaire=questionnaire,
            current_item_index=0,
            messages=[{"role": "user", "content": bad}],
        )
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            result = validate_response_node(state)

        last_msg = result["messages"][-1]
        assert last_msg.get("_valid") is False, (
            f"Invalid date '{bad}' should be rejected"
        )


@given(link_id=_link_id_st)
@settings(max_examples=20)
def test_p6_empty_string_rejected(link_id: str):
    """Empty strings must be rejected for string items."""
    item = _make_questionnaire_item(link_id, "string")
    questionnaire = _make_questionnaire([item])

    state = _base_state(
        questionnaire=questionnaire,
        current_item_index=0,
        messages=[{"role": "user", "content": ""}],
    )
    with patch("agents.screening_agent.audit_log_event") as mock_audit:
        mock_audit.invoke.return_value = {"status": "recorded"}
        result = validate_response_node(state)

    last_msg = result["messages"][-1]
    assert last_msg.get("_valid") is False, "Empty string should be rejected"


@given(link_id=_link_id_st)
@settings(max_examples=20)
def test_p6_invalid_response_triggers_clarification_flag(link_id: str):
    """When a response is invalid, _valid must be False so routing sends
    to ask_clarification."""
    item = _make_questionnaire_item(link_id, "integer")
    questionnaire = _make_questionnaire([item])

    state = _base_state(
        questionnaire=questionnaire,
        current_item_index=0,
        messages=[{"role": "user", "content": "not_a_number"}],
    )
    with patch("agents.screening_agent.audit_log_event") as mock_audit:
        mock_audit.invoke.return_value = {"status": "recorded"}
        result = validate_response_node(state)

    last_msg = result["messages"][-1]
    assert last_msg["_valid"] is False
    # Verify clarification node produces a message
    clar_state = _base_state(
        questionnaire=questionnaire,
        current_item_index=0,
        messages=result["messages"],
    )
    with patch("agents.screening_agent.audit_log_event") as mock_audit:
        mock_audit.invoke.return_value = {"status": "recorded"}
        clar_result = ask_clarification_node(clar_state)

    assert any(m["role"] == "assistant" for m in clar_result["messages"])


# ===================================================================
# Property 7 — EnableWhen Conditional Activation
# Verify deep-dive modules activate/deactivate correctly.
# Validates: Requirements 2.5
# ===================================================================


@given(trigger_link_id=_link_id_st, deep_dive_link_id=_link_id_st)
@settings(max_examples=30)
def test_p7_enable_when_true_activates_item(trigger_link_id: str, deep_dive_link_id: str):
    """When a response satisfies an enableWhen condition, the conditional
    item must be activated."""
    assume(trigger_link_id != deep_dive_link_id)

    condition = {"question": trigger_link_id, "operator": "=", "answerBoolean": True}
    responses = [{"linkId": trigger_link_id, "answer": True}]

    assert _evaluate_enable_when(condition, responses) is True
    assert _should_activate_item(
        {"linkId": deep_dive_link_id, "enableWhen": [condition]}, responses
    ) is True


@given(trigger_link_id=_link_id_st, deep_dive_link_id=_link_id_st)
@settings(max_examples=30)
def test_p7_enable_when_false_deactivates_item(trigger_link_id: str, deep_dive_link_id: str):
    """When a response does NOT satisfy an enableWhen condition, the
    conditional item must NOT be activated."""
    assume(trigger_link_id != deep_dive_link_id)

    condition = {"question": trigger_link_id, "operator": "=", "answerBoolean": True}
    responses = [{"linkId": trigger_link_id, "answer": False}]

    assert _evaluate_enable_when(condition, responses) is False
    assert _should_activate_item(
        {"linkId": deep_dive_link_id, "enableWhen": [condition]}, responses
    ) is False


@given(trigger_link_id=_link_id_st)
@settings(max_examples=20)
def test_p7_no_enable_when_always_active(trigger_link_id: str):
    """Items without enableWhen conditions must always be active."""
    item = {"linkId": trigger_link_id, "type": "boolean"}
    assert _should_activate_item(item, []) is True
    assert _should_activate_item(item, [{"linkId": "IE-999", "answer": True}]) is True


@given(
    trigger1=_link_id_st,
    trigger2=_link_id_st,
    target=_link_id_st,
)
@settings(max_examples=30)
def test_p7_multiple_enable_when_all_must_match(
    trigger1: str, trigger2: str, target: str
):
    """When an item has multiple enableWhen conditions, ALL must be satisfied
    for the item to activate (AND logic)."""
    assume(len({trigger1, trigger2, target}) == 3)

    conditions = [
        {"question": trigger1, "operator": "=", "answerBoolean": True},
        {"question": trigger2, "operator": "=", "answerBoolean": True},
    ]
    item = {"linkId": target, "enableWhen": conditions}

    # Both satisfied → active
    both_true = [
        {"linkId": trigger1, "answer": True},
        {"linkId": trigger2, "answer": True},
    ]
    assert _should_activate_item(item, both_true) is True

    # Only one satisfied → not active
    one_false = [
        {"linkId": trigger1, "answer": True},
        {"linkId": trigger2, "answer": False},
    ]
    assert _should_activate_item(item, one_false) is False


@given(trigger_link_id=_link_id_st)
@settings(max_examples=20)
def test_p7_enable_when_missing_response_deactivates(trigger_link_id: str):
    """When the referenced question has no response yet, the enableWhen
    condition must evaluate to False."""
    condition = {"question": trigger_link_id, "operator": "=", "answerBoolean": True}
    # Empty responses — the trigger question hasn't been answered
    assert _evaluate_enable_when(condition, []) is False


@given(
    trigger_link_id=_link_id_st,
    answer_str=st.text(
        alphabet=st.characters(whitelist_categories=("L", "N")),
        min_size=1,
        max_size=10,
    ),
)
@settings(max_examples=20)
def test_p7_enable_when_string_equality(trigger_link_id: str, answer_str: str):
    """enableWhen with answerString must match case-insensitively."""
    # Implementation compares via .lower(), so use .lower() variant for match
    condition = {"question": trigger_link_id, "operator": "=", "answerString": answer_str}
    responses = [{"linkId": trigger_link_id, "answer": answer_str}]
    assert _evaluate_enable_when(condition, responses) is True

    # Mismatch: append a character so the strings differ
    responses_mismatch = [{"linkId": trigger_link_id, "answer": answer_str + "X"}]
    assert _evaluate_enable_when(condition, responses_mismatch) is False


def test_p7_check_enable_when_node_activates_deep_dive():
    """check_enable_when_node must set deep_dive_active when a group item's
    enableWhen is satisfied."""
    items = [
        _make_questionnaire_item("IE-001", "boolean"),
        {
            "linkId": "DD-cardiovascular",
            "text": "Cardiovascular deep-dive",
            "type": "group",
            "enableWhen": [{"question": "IE-001", "operator": "=", "answerBoolean": True}],
            "item": [_make_questionnaire_item("IE-CV-001", "boolean")],
        },
    ]
    questionnaire = _make_questionnaire(items)
    state = _base_state(
        questionnaire=questionnaire,
        current_item_index=0,
        responses=[{"linkId": "IE-001", "answer": True}],
    )
    result = check_enable_when_node(state)
    assert result.get("deep_dive_active") == "cardiovascular"


def test_p7_check_enable_when_node_no_activation():
    """check_enable_when_node must set deep_dive_active to None when no
    group item's enableWhen is satisfied."""
    items = [
        _make_questionnaire_item("IE-001", "boolean"),
        {
            "linkId": "DD-cardiovascular",
            "text": "Cardiovascular deep-dive",
            "type": "group",
            "enableWhen": [{"question": "IE-001", "operator": "=", "answerBoolean": True}],
            "item": [_make_questionnaire_item("IE-CV-001", "boolean")],
        },
    ]
    questionnaire = _make_questionnaire(items)
    state = _base_state(
        questionnaire=questionnaire,
        current_item_index=0,
        responses=[{"linkId": "IE-001", "answer": False}],
    )
    result = check_enable_when_node(state)
    assert result.get("deep_dive_active") is None


# ===================================================================
# Property 8 — Screening Completeness Invariant
# Verify finalized QuestionnaireResponse has entry for every required item.
# Validates: Requirements 2.7, 5.1, 5.2, 5.3
# ===================================================================


@given(
    n_items=st.integers(min_value=1, max_value=8),
    patient_id=_uuid_st,
    session_id=_session_st,
)
@settings(max_examples=30)
def test_p8_finalized_qr_covers_all_required_items(
    n_items: int, patient_id: str, session_id: str
):
    """A finalized QuestionnaireResponse must contain an entry for every
    required Questionnaire item — either an answer or a 'declined' status."""
    items = [_make_questionnaire_item(f"IE-{i:03d}", "boolean") for i in range(n_items)]
    questionnaire = _make_questionnaire(items)

    # Simulate: first half answered, second half declined
    mid = n_items // 2 or 1
    responses = [
        {"linkId": f"IE-{i:03d}", "answer": True, "raw": "true"}
        for i in range(mid)
    ]
    declined = [
        {"linkId": f"IE-{i:03d}", "text": f"Q {i}", "declined_at": "2026-01-01T00:00:00Z", "flagged_for_pi_review": True}
        for i in range(mid, n_items)
    ]

    state = _base_state(
        questionnaire=questionnaire,
        patient_id=patient_id,
        session_id=session_id,
        responses=responses,
        declined_items=declined,
        eligibility_determination="eligible",
    )

    with patch("agents.screening_agent.healthlake_store_resource") as mock_store:
        mock_store.invoke.return_value = {"status": "created", "resource_id": "qr-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            finalize_screening_node(state)

    stored_resource = mock_store.invoke.call_args[0][0]["resource"]
    qr_items = stored_resource["item"]
    qr_link_ids = {it["linkId"] for it in qr_items}

    for i in range(n_items):
        expected_id = f"IE-{i:03d}"
        assert expected_id in qr_link_ids, (
            f"Required item {expected_id} missing from finalized QuestionnaireResponse"
        )


@given(n_items=st.integers(min_value=1, max_value=5))
@settings(max_examples=20)
def test_p8_declined_items_marked_in_qr(n_items: int):
    """Declined items must appear in the QuestionnaireResponse with
    'declined' value and a declined-item extension."""
    items = [_make_questionnaire_item(f"IE-{i:03d}", "boolean") for i in range(n_items)]
    questionnaire = _make_questionnaire(items)

    declined = [
        {"linkId": f"IE-{i:03d}", "text": f"Q {i}", "declined_at": "2026-01-01T00:00:00Z", "flagged_for_pi_review": True}
        for i in range(n_items)
    ]

    state = _base_state(
        questionnaire=questionnaire,
        responses=[],
        declined_items=declined,
        eligibility_determination="borderline",
    )

    with patch("agents.screening_agent.healthlake_store_resource") as mock_store:
        mock_store.invoke.return_value = {"status": "created", "resource_id": "qr-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            finalize_screening_node(state)

    stored_resource = mock_store.invoke.call_args[0][0]["resource"]
    for qr_item in stored_resource["item"]:
        answer_val = qr_item["answer"][0].get("valueString", "")
        assert answer_val == "declined", (
            f"Declined item {qr_item['linkId']} should have 'declined' answer"
        )
        assert any(
            ext.get("url", "").endswith("declined-item")
            for ext in qr_item.get("extension", [])
        ), f"Declined item {qr_item['linkId']} missing declined-item extension"


@given(
    n_required=st.integers(min_value=1, max_value=5),
    n_optional=st.integers(min_value=0, max_value=3),
)
@settings(max_examples=20)
def test_p8_check_completion_detects_missing(n_required: int, n_optional: int):
    """check_completion_node must detect all required items that lack
    responses and are not declined."""
    required = [_make_questionnaire_item(f"IE-{i:03d}", "boolean", required=True) for i in range(n_required)]
    optional = [_make_questionnaire_item(f"OPT-{i:03d}", "string", required=False) for i in range(n_optional)]
    questionnaire = _make_questionnaire(required + optional)

    # No responses, no declined → all required items should be missing
    state = _base_state(questionnaire=questionnaire, responses=[], declined_items=[])
    result = check_completion_node(state)

    missing_ids = {m["linkId"] for m in result["missing_items"]}
    for i in range(n_required):
        assert f"IE-{i:03d}" in missing_ids, f"Required item IE-{i:03d} not detected as missing"

    # Optional items should NOT appear in missing
    for i in range(n_optional):
        assert f"OPT-{i:03d}" not in missing_ids, f"Optional item OPT-{i:03d} should not be missing"


def test_p8_check_completion_all_answered():
    """When all required items have responses, missing_items must be empty."""
    items = [_make_questionnaire_item(f"IE-{i:03d}", "boolean") for i in range(3)]
    questionnaire = _make_questionnaire(items)
    responses = [{"linkId": f"IE-{i:03d}", "answer": True} for i in range(3)]

    state = _base_state(questionnaire=questionnaire, responses=responses)
    result = check_completion_node(state)
    assert result["missing_items"] == []


# ===================================================================
# Property 10 — PHI Non-Disclosure in Agent Outputs
# Verify no PHI in agent messages.
# Validates: Requirements 3.3, 4.1, 4.2, 4.5
# ===================================================================

# PHI data that must NEVER appear in agent outputs
_SAMPLE_PHI = {
    "patient_name": "John Smith",
    "dob": "03/15/1980",
    "mrn": "MRN: 12345678",
    "ssn": "123-45-6789",
    "diagnosis": "Type 2 diabetes mellitus",
    "medication": "Metformin 500mg",
    "lab_value": "HbA1c 7.2%",
    "procedure": "Coronary angioplasty",
}


def _assert_no_phi_in_messages(messages: list[dict], phi_data: dict) -> None:
    """Assert that none of the PHI values appear in any assistant message."""
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "").lower()
        for phi_key, phi_val in phi_data.items():
            assert phi_val.lower() not in content, (
                f"PHI '{phi_key}' value found in agent message: {content[:100]}"
            )
        # Check regex patterns for structured PHI
        for pattern in _PHI_PATTERNS:
            assert not re.search(pattern, msg.get("content", "")), (
                f"PHI pattern {pattern} found in agent message"
            )


@given(link_id=_link_id_st)
@settings(max_examples=20)
def test_p10_ask_question_no_phi_disclosure(link_id: str):
    """ask_question_node must not include any PHI from FHIR records in the
    generated question."""
    item = _make_questionnaire_item(link_id, "boolean", text="Do you have diabetes?")
    questionnaire = _make_questionnaire([item])

    # Load FHIR data with PHI
    fhir_data = {
        "Patient": [{"resourceType": "Patient", "id": "p1", "name": [{"text": _SAMPLE_PHI["patient_name"]}]}],
        "Condition": [{"resourceType": "Condition", "id": "c1",
                       "code": {"text": _SAMPLE_PHI["diagnosis"]}}],
        "MedicationRequest": [{"resourceType": "MedicationRequest", "id": "m1",
                               "medicationCodeableConcept": {"text": _SAMPLE_PHI["medication"]}}],
    }

    state = _base_state(
        questionnaire=questionnaire,
        patient_fhir_data=fhir_data,
        current_item_index=0,
    )

    mock_response = MagicMock()
    mock_response.content = "Could you tell me if you have been diagnosed with a chronic condition?"

    with patch("agents.screening_agent._get_llm") as mock_llm_factory:
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = mock_response
        mock_llm_factory.return_value = mock_llm
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            result = ask_question_node(state)

    _assert_no_phi_in_messages(result.get("messages", []), _SAMPLE_PHI)


@given(link_id=_link_id_st)
@settings(max_examples=20)
def test_p10_clarification_no_phi_disclosure(link_id: str):
    """ask_clarification_node must use generic phrasing without revealing
    FHIR record content."""
    item = _make_questionnaire_item(link_id, "boolean")
    questionnaire = _make_questionnaire([item])

    state = _base_state(
        questionnaire=questionnaire,
        current_item_index=0,
        patient_fhir_data={
            "Condition": [{"code": {"text": _SAMPLE_PHI["diagnosis"]}}],
        },
        discrepancies=[{
            "criterion_id": link_id,
            "severity": "critical",
            "patient_reported": "no",
            "fhir_record": _SAMPLE_PHI["diagnosis"],
        }],
        messages=[{"role": "user", "content": "no", "_valid": True}],
    )

    with patch("agents.screening_agent.audit_log_event") as mock_audit:
        mock_audit.invoke.return_value = {"status": "recorded"}
        result = ask_clarification_node(state)

    _assert_no_phi_in_messages(result["messages"], _SAMPLE_PHI)

    # Verify generic phrasing is used
    assistant_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
    assert len(assistant_msgs) > 0
    last_assistant = assistant_msgs[-1]["content"].lower()
    assert "difference" in last_assistant or "clarify" in last_assistant, (
        "Clarification should use generic phrasing about differences"
    )


def test_p10_finalize_no_phi_in_completion_message():
    """finalize_screening_node completion message must not contain PHI."""
    state = _base_state(
        questionnaire={"id": "q1", "item": []},
        responses=[],
        declined_items=[],
        eligibility_determination="eligible",
    )

    with patch("agents.screening_agent.healthlake_store_resource") as mock_store:
        mock_store.invoke.return_value = {"status": "created", "resource_id": "qr-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            result = finalize_screening_node(state)

    _assert_no_phi_in_messages(result.get("messages", []), _SAMPLE_PHI)


# ===================================================================
# Property 12 — Blinding Enforcement
# Verify no treatment arm info in outputs for blinded trials.
# Validates: Requirements 4.3
# ===================================================================


@given(link_id=_link_id_st)
@settings(max_examples=20)
def test_p12_ask_question_no_treatment_arm_info(link_id: str):
    """For blinded trials, ask_question_node must not reveal treatment arm
    assignment in any generated question."""
    item = _make_questionnaire_item(link_id, "boolean", text="Are you eligible?")
    questionnaire = _make_questionnaire([item])

    state = _base_state(questionnaire=questionnaire, current_item_index=0)

    mock_response = MagicMock()
    mock_response.content = "Can you confirm your eligibility for this study?"

    with patch("agents.screening_agent._get_llm") as mock_llm_factory:
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = mock_response
        mock_llm_factory.return_value = mock_llm
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            result = ask_question_node(state)

    for msg in result.get("messages", []):
        if msg.get("role") != "assistant":
            continue
        content_lower = msg["content"].lower()
        for term in _TREATMENT_ARM_TERMS:
            assert term not in content_lower, (
                f"Treatment arm term '{term}' found in agent output for blinded trial"
            )


def test_p12_clarification_no_treatment_arm():
    """Clarification messages must not contain treatment arm information."""
    item = _make_questionnaire_item("IE-001", "boolean")
    questionnaire = _make_questionnaire([item])

    state = _base_state(
        questionnaire=questionnaire,
        current_item_index=0,
        messages=[{"role": "user", "content": "maybe", "_valid": False}],
    )

    with patch("agents.screening_agent.audit_log_event") as mock_audit:
        mock_audit.invoke.return_value = {"status": "recorded"}
        result = ask_clarification_node(state)

    for msg in result["messages"]:
        if msg.get("role") != "assistant":
            continue
        content_lower = msg["content"].lower()
        for term in _TREATMENT_ARM_TERMS:
            assert term not in content_lower, (
                f"Treatment arm term '{term}' in clarification message"
            )


def test_p12_escalation_no_treatment_arm():
    """Escalation messages must not contain treatment arm information."""
    state = _base_state(
        eligibility_determination="borderline",
        eligibility_criteria_results=[
            {"criterion_id": "IE-001", "result": "indeterminate", "fhir_refs": []},
        ],
        discrepancies=[],
    )

    with patch("agents.screening_agent.sqs_send_message") as mock_sqs:
        mock_sqs.invoke.return_value = {"message_id": "msg-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            escalate_node(state)

    sent_msg = mock_sqs.invoke.call_args[0][0]["message"]
    msg_str = json.dumps(sent_msg).lower()
    for term in _TREATMENT_ARM_TERMS:
        assert term not in msg_str, (
            f"Treatment arm term '{term}' found in escalation message"
        )


# ===================================================================
# Property 14 — QuestionnaireResponse Persistence Integrity
# Verify correct Patient, Questionnaire, and session linkage.
# Validates: Requirements 5.4, 7.5
# ===================================================================


@given(patient_id=_uuid_st, trial_id=_nct_st, session_id=_session_st)
@settings(max_examples=30)
def test_p14_qr_linked_to_correct_patient(patient_id: str, trial_id: str, session_id: str):
    """The persisted QuestionnaireResponse must reference the correct Patient."""
    questionnaire = _make_questionnaire([_make_questionnaire_item("IE-001", "boolean")])
    questionnaire["id"] = f"trial-{trial_id}-screening-v1"

    state = _base_state(
        patient_id=patient_id,
        trial_id=trial_id,
        session_id=session_id,
        questionnaire=questionnaire,
        responses=[{"linkId": "IE-001", "answer": True, "raw": "true"}],
        eligibility_determination="eligible",
    )

    with patch("agents.screening_agent.healthlake_store_resource") as mock_store:
        mock_store.invoke.return_value = {"status": "created", "resource_id": "qr-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            finalize_screening_node(state)

    stored = mock_store.invoke.call_args[0][0]["resource"]
    assert stored["subject"]["reference"] == f"Patient/{patient_id}", (
        f"QR subject should reference Patient/{patient_id}"
    )


@given(patient_id=_uuid_st, trial_id=_nct_st, session_id=_session_st)
@settings(max_examples=30)
def test_p14_qr_linked_to_correct_questionnaire(patient_id: str, trial_id: str, session_id: str):
    """The persisted QuestionnaireResponse must reference the correct Questionnaire."""
    q_id = f"trial-{trial_id}-screening-v1"
    questionnaire = _make_questionnaire([_make_questionnaire_item("IE-001", "boolean")])
    questionnaire["id"] = q_id

    state = _base_state(
        patient_id=patient_id,
        trial_id=trial_id,
        session_id=session_id,
        questionnaire=questionnaire,
        responses=[{"linkId": "IE-001", "answer": True, "raw": "true"}],
        eligibility_determination="eligible",
    )

    with patch("agents.screening_agent.healthlake_store_resource") as mock_store:
        mock_store.invoke.return_value = {"status": "created", "resource_id": "qr-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            finalize_screening_node(state)

    stored = mock_store.invoke.call_args[0][0]["resource"]
    assert stored["questionnaire"] == f"Questionnaire/{q_id}", (
        f"QR questionnaire should reference Questionnaire/{q_id}"
    )


@given(patient_id=_uuid_st, trial_id=_nct_st, session_id=_session_st)
@settings(max_examples=30)
def test_p14_qr_contains_session_and_trial_metadata(
    patient_id: str, trial_id: str, session_id: str
):
    """The persisted QuestionnaireResponse must contain session_id, trial_id,
    and agent_id in the trial-session extension."""
    questionnaire = _make_questionnaire([_make_questionnaire_item("IE-001", "boolean")])
    questionnaire["id"] = f"trial-{trial_id}-screening-v1"

    state = _base_state(
        patient_id=patient_id,
        trial_id=trial_id,
        session_id=session_id,
        questionnaire=questionnaire,
        responses=[{"linkId": "IE-001", "answer": True, "raw": "true"}],
        eligibility_determination="eligible",
    )

    with patch("agents.screening_agent.healthlake_store_resource") as mock_store:
        mock_store.invoke.return_value = {"status": "created", "resource_id": "qr-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            finalize_screening_node(state)

    stored = mock_store.invoke.call_args[0][0]["resource"]

    # Find the trial-session extension
    session_ext = None
    for ext in stored.get("extension", []):
        if ext.get("url", "").endswith("trial-session"):
            session_ext = json.loads(ext["valueString"])
            break

    assert session_ext is not None, "trial-session extension missing"
    assert session_ext["session_id"] == session_id
    assert session_ext["trial_id"] == trial_id
    assert "agent_id" in session_ext


@given(patient_id=_uuid_st, trial_id=_nct_st, session_id=_session_st)
@settings(max_examples=30)
def test_p14_qr_contains_eligibility_determination(
    patient_id: str, trial_id: str, session_id: str
):
    """The persisted QuestionnaireResponse must contain the eligibility
    determination in the eligibility-determination extension."""
    questionnaire = _make_questionnaire([_make_questionnaire_item("IE-001", "boolean")])
    questionnaire["id"] = f"trial-{trial_id}-screening-v1"

    state = _base_state(
        patient_id=patient_id,
        trial_id=trial_id,
        session_id=session_id,
        questionnaire=questionnaire,
        responses=[{"linkId": "IE-001", "answer": True, "raw": "true"}],
        eligibility_determination="eligible",
    )

    with patch("agents.screening_agent.healthlake_store_resource") as mock_store:
        mock_store.invoke.return_value = {"status": "created", "resource_id": "qr-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            finalize_screening_node(state)

    stored = mock_store.invoke.call_args[0][0]["resource"]

    elig_ext = None
    for ext in stored.get("extension", []):
        if ext.get("url", "").endswith("eligibility-determination"):
            elig_ext = json.loads(ext["valueString"])
            break

    assert elig_ext is not None, "eligibility-determination extension missing"
    assert elig_ext["determination"] == "eligible"


def test_p14_qr_resource_type_is_questionnaire_response():
    """The persisted resource must have resourceType 'QuestionnaireResponse'."""
    questionnaire = _make_questionnaire([])
    questionnaire["id"] = "trial-NCT00000001-screening-v1"

    state = _base_state(
        questionnaire=questionnaire,
        responses=[],
        eligibility_determination="eligible",
    )

    with patch("agents.screening_agent.healthlake_store_resource") as mock_store:
        mock_store.invoke.return_value = {"status": "created", "resource_id": "qr-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            finalize_screening_node(state)

    stored = mock_store.invoke.call_args[0][0]["resource"]
    assert stored["resourceType"] == "QuestionnaireResponse"
    assert stored["status"] == "completed"


# ===================================================================
# Property 16 — Escalation Message Completeness
# Verify borderline escalations include full context.
# Validates: Requirements 6.1, 6.4
# ===================================================================


@given(
    patient_id=_uuid_st,
    trial_id=_nct_st,
    session_id=_session_st,
    n_criteria=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=30)
def test_p16_escalation_includes_all_required_fields(
    patient_id: str, trial_id: str, session_id: str, n_criteria: int
):
    """Escalation messages for borderline determinations must include:
    eligibility_determination, criteria_breakdown, discrepancy_flags,
    questionnaire_response_ref, and agent_reasoning."""
    criteria_results = [
        {"criterion_id": f"IE-{i:03d}", "result": "indeterminate", "fhir_refs": []}
        for i in range(n_criteria)
    ]

    state = _base_state(
        patient_id=patient_id,
        trial_id=trial_id,
        session_id=session_id,
        eligibility_determination="borderline",
        eligibility_criteria_results=criteria_results,
        discrepancies=[],
    )

    with patch("agents.screening_agent.sqs_send_message") as mock_sqs:
        mock_sqs.invoke.return_value = {"message_id": "msg-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            escalate_node(state)

    sent_msg = mock_sqs.invoke.call_args[0][0]["message"]

    required_fields = [
        "patient_id", "trial_id", "session_id", "escalation_reason",
        "urgency", "eligibility_determination", "criteria_breakdown",
        "discrepancy_flags", "questionnaire_response_ref", "agent_reasoning",
    ]
    for field in required_fields:
        assert field in sent_msg, f"Escalation message missing required field: {field}"

    assert sent_msg["patient_id"] == patient_id
    assert sent_msg["trial_id"] == trial_id
    assert sent_msg["session_id"] == session_id
    assert sent_msg["eligibility_determination"] == "borderline"
    assert len(sent_msg["criteria_breakdown"]) == n_criteria


@given(patient_id=_uuid_st, trial_id=_nct_st, session_id=_session_st)
@settings(max_examples=20)
def test_p16_escalation_includes_discrepancy_flags(
    patient_id: str, trial_id: str, session_id: str
):
    """Escalation messages must include all discrepancy flags from the
    screening session."""
    discrepancies = [
        {"criterion_id": "IE-001", "severity": "warning", "patient_reported": "yes", "fhir_record": "no"},
        {"criterion_id": "IE-002", "severity": "critical", "patient_reported": "50", "fhir_record": "120"},
    ]

    state = _base_state(
        patient_id=patient_id,
        trial_id=trial_id,
        session_id=session_id,
        eligibility_determination="borderline",
        eligibility_criteria_results=[],
        discrepancies=discrepancies,
    )

    with patch("agents.screening_agent.sqs_send_message") as mock_sqs:
        mock_sqs.invoke.return_value = {"message_id": "msg-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            escalate_node(state)

    sent_msg = mock_sqs.invoke.call_args[0][0]["message"]
    assert len(sent_msg["discrepancy_flags"]) == 2


@given(patient_id=_uuid_st, session_id=_session_st)
@settings(max_examples=20)
def test_p16_escalation_includes_qr_reference(patient_id: str, session_id: str):
    """Escalation message must include a QuestionnaireResponse reference
    so the PI can review without re-interviewing."""
    state = _base_state(
        patient_id=patient_id,
        session_id=session_id,
        eligibility_determination="borderline",
        eligibility_criteria_results=[],
        discrepancies=[],
    )

    with patch("agents.screening_agent.sqs_send_message") as mock_sqs:
        mock_sqs.invoke.return_value = {"message_id": "msg-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            escalate_node(state)

    sent_msg = mock_sqs.invoke.call_args[0][0]["message"]
    assert "QuestionnaireResponse" in sent_msg["questionnaire_response_ref"]
    assert session_id in sent_msg["questionnaire_response_ref"]


@given(patient_id=_uuid_st, session_id=_session_st)
@settings(max_examples=20)
def test_p16_escalation_agent_reasoning_non_empty(patient_id: str, session_id: str):
    """Escalation message must include non-empty agent reasoning summary."""
    state = _base_state(
        patient_id=patient_id,
        session_id=session_id,
        eligibility_determination="borderline",
        eligibility_criteria_results=[],
        discrepancies=[],
    )

    with patch("agents.screening_agent.sqs_send_message") as mock_sqs:
        mock_sqs.invoke.return_value = {"message_id": "msg-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            escalate_node(state)

    sent_msg = mock_sqs.invoke.call_args[0][0]["message"]
    assert len(sent_msg["agent_reasoning"]) > 0, "Agent reasoning must not be empty"


# ===================================================================
# Property 17 — Safety Signal Escalation
# Verify safety signals escalated with correct urgency.
# Validates: Requirements 6.2, 7.6
# ===================================================================


def test_p17_critical_discrepancy_sets_high_urgency():
    """When discrepancies include a critical severity (safety signal),
    the escalation urgency must be 'high'."""
    state = _base_state(
        eligibility_determination="borderline",
        eligibility_criteria_results=[],
        discrepancies=[
            {"criterion_id": "IE-001", "severity": "critical",
             "patient_reported": "yes", "fhir_record": "no"},
        ],
    )

    with patch("agents.screening_agent.sqs_send_message") as mock_sqs:
        mock_sqs.invoke.return_value = {"message_id": "msg-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            escalate_node(state)

    sent_msg = mock_sqs.invoke.call_args[0][0]["message"]
    assert sent_msg["urgency"] == "high", (
        f"Critical discrepancy should set urgency to 'high', got '{sent_msg['urgency']}'"
    )

    # Also verify SQS attributes
    sent_attrs = mock_sqs.invoke.call_args[0][0]["attributes"]
    assert sent_attrs["Urgency"] == "high"


def test_p17_no_critical_discrepancy_sets_standard_urgency():
    """When no critical discrepancies exist, the escalation urgency must
    be 'standard'."""
    state = _base_state(
        eligibility_determination="borderline",
        eligibility_criteria_results=[],
        discrepancies=[
            {"criterion_id": "IE-001", "severity": "warning",
             "patient_reported": "50", "fhir_record": "55"},
        ],
    )

    with patch("agents.screening_agent.sqs_send_message") as mock_sqs:
        mock_sqs.invoke.return_value = {"message_id": "msg-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            escalate_node(state)

    sent_msg = mock_sqs.invoke.call_args[0][0]["message"]
    assert sent_msg["urgency"] == "standard"


@given(
    n_warning=st.integers(min_value=0, max_value=3),
    n_critical=st.integers(min_value=0, max_value=3),
)
@settings(max_examples=30)
def test_p17_urgency_reflects_worst_severity(n_warning: int, n_critical: int):
    """Urgency must be 'high' if ANY critical discrepancy exists, regardless
    of how many warning-level discrepancies are present."""
    assume(n_warning + n_critical > 0)

    discrepancies = [
        {"criterion_id": f"W-{i}", "severity": "warning", "patient_reported": "a", "fhir_record": "b"}
        for i in range(n_warning)
    ] + [
        {"criterion_id": f"C-{i}", "severity": "critical", "patient_reported": "x", "fhir_record": "y"}
        for i in range(n_critical)
    ]

    state = _base_state(
        eligibility_determination="borderline",
        eligibility_criteria_results=[],
        discrepancies=discrepancies,
    )

    with patch("agents.screening_agent.sqs_send_message") as mock_sqs:
        mock_sqs.invoke.return_value = {"message_id": "msg-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            escalate_node(state)

    sent_msg = mock_sqs.invoke.call_args[0][0]["message"]
    expected = "high" if n_critical > 0 else "standard"
    assert sent_msg["urgency"] == expected, (
        f"Expected urgency '{expected}' with {n_critical} critical, got '{sent_msg['urgency']}'"
    )


def test_p17_escalation_sent_to_correct_queue():
    """Escalation messages must be sent to the TrialEscalationQueue."""
    state = _base_state(
        eligibility_determination="borderline",
        eligibility_criteria_results=[],
        discrepancies=[],
    )

    with patch("agents.screening_agent.sqs_send_message") as mock_sqs:
        mock_sqs.invoke.return_value = {"message_id": "msg-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            escalate_node(state)

    queue_name = mock_sqs.invoke.call_args[0][0]["queue_name"]
    assert "Escalation" in queue_name, (
        f"Escalation should go to escalation queue, got '{queue_name}'"
    )


# ===================================================================
# Property 18 — Escalation Audit Trail
# Verify escalation events logged with required metadata.
# Validates: Requirements 6.3, 6.5
# ===================================================================


@given(
    patient_id=_uuid_st,
    trial_id=_nct_st,
    session_id=_session_st,
)
@settings(max_examples=30)
def test_p18_escalation_logged_in_audit(patient_id: str, trial_id: str, session_id: str):
    """Every escalation event must be logged in the Audit_Logger with
    session_id, patient_id, escalation reason, urgency, and timestamp context."""
    state = _base_state(
        patient_id=patient_id,
        trial_id=trial_id,
        session_id=session_id,
        eligibility_determination="borderline",
        eligibility_criteria_results=[],
        discrepancies=[],
    )

    with patch("agents.screening_agent.sqs_send_message") as mock_sqs:
        mock_sqs.invoke.return_value = {"message_id": "msg-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            escalate_node(state)

    # Verify audit_log_event was called
    assert mock_audit.invoke.called, "Audit log must be called for escalation"

    audit_call = mock_audit.invoke.call_args[0][0]
    assert audit_call["session_id"] == session_id
    assert audit_call["patient_id"] == patient_id
    assert audit_call["trial_id"] == trial_id
    assert audit_call["event_type"] == "escalation"
    assert "urgency" in audit_call.get("event_data", {})
    assert "determination" in audit_call.get("event_data", {})


@given(patient_id=_uuid_st, session_id=_session_st)
@settings(max_examples=20)
def test_p18_escalation_audit_includes_agent_id(patient_id: str, session_id: str):
    """Escalation audit records must include the agent identifier."""
    state = _base_state(
        patient_id=patient_id,
        session_id=session_id,
        eligibility_determination="borderline",
        eligibility_criteria_results=[],
        discrepancies=[],
    )

    with patch("agents.screening_agent.sqs_send_message") as mock_sqs:
        mock_sqs.invoke.return_value = {"message_id": "msg-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            escalate_node(state)

    audit_call = mock_audit.invoke.call_args[0][0]
    assert audit_call.get("agent_id") == "screening-agent"


@given(patient_id=_uuid_st, session_id=_session_st)
@settings(max_examples=20)
def test_p18_escalation_audit_includes_discrepancy_count(patient_id: str, session_id: str):
    """Escalation audit event_data must include the discrepancy count for
    traceability."""
    discrepancies = [
        {"criterion_id": "IE-001", "severity": "warning", "patient_reported": "a", "fhir_record": "b"},
        {"criterion_id": "IE-002", "severity": "critical", "patient_reported": "x", "fhir_record": "y"},
    ]

    state = _base_state(
        patient_id=patient_id,
        session_id=session_id,
        eligibility_determination="borderline",
        eligibility_criteria_results=[],
        discrepancies=discrepancies,
    )

    with patch("agents.screening_agent.sqs_send_message") as mock_sqs:
        mock_sqs.invoke.return_value = {"message_id": "msg-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            escalate_node(state)

    audit_data = mock_audit.invoke.call_args[0][0].get("event_data", {})
    assert audit_data.get("discrepancy_count") == 2


def test_p18_finalize_screening_logged():
    """finalize_screening_node must log a decision_made audit event."""
    state = _base_state(
        questionnaire={"id": "q1", "item": []},
        responses=[],
        declined_items=[],
        eligibility_determination="eligible",
    )

    with patch("agents.screening_agent.healthlake_store_resource") as mock_store:
        mock_store.invoke.return_value = {"status": "created", "resource_id": "qr-1"}
        with patch("agents.screening_agent.audit_log_event") as mock_audit:
            mock_audit.invoke.return_value = {"status": "recorded"}
            finalize_screening_node(state)

    assert mock_audit.invoke.called, "Finalize must log an audit event"
    audit_call = mock_audit.invoke.call_args[0][0]
    assert audit_call["event_type"] == "decision_made"
    assert "screening_finalized" in audit_call.get("event_data", {}).get("action", "")
