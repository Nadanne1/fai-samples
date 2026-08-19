"""Property-based tests for the audit logging tool.

Properties tested:
  P20 — Comprehensive Audit Logging: all event types produce records with required metadata.
  P21 — Audit Write Retry and Dead-Letter: exponential backoff, session halt, DLQ on final failure.
  P22 — Audit Record Retention: TTL >= 15 years from creation.
  P13 — PHI Encryption at Rest: PHI in phi_encrypted via KMS, never in event_data.

Validates Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 4.4
"""

import json
import time
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
from hypothesis import given, settings

from models.audit import AuditEventType
from tools.audit import audit_log_event

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

EVENT_TYPES: list[AuditEventType] = [
    "question_asked",
    "response_received",
    "validation_performed",
    "decision_made",
    "data_access",
    "escalation",
    "phi_violation",
    "electronic_signature",
    "audit_write_failure",
]

_uuid_st = st.uuids().map(str)

_event_type_st = st.sampled_from(EVENT_TYPES)

_base_event_st = st.fixed_dictionaries(
    {
        "session_id": _uuid_st,
        "event_type": _event_type_st,
        "patient_id": _uuid_st,
        "trial_id": st.from_regex(r"NCT[0-9]{8}", fullmatch=True),
        "agent_id": st.sampled_from(
            ["screening-agent", "monitoring-agent", "protocol-engine"]
        ),
        "user_identity": st.text(min_size=1, max_size=50),
    },
)


_phi_data_st = st.fixed_dictionaries(
    {
        "patient_name": st.text(
            alphabet=st.characters(categories=("L", "N")),
            min_size=3,
            max_size=30,
        ),
        "date_of_birth": st.dates().map(str),
        "diagnosis": st.text(
            alphabet=st.characters(categories=("L", "N")),
            min_size=3,
            max_size=60,
        ),
    },
)

_retention_years_st = st.integers(min_value=1, max_value=100)

SECONDS_PER_YEAR = 365.25 * 24 * 3600


# ---------------------------------------------------------------------------
# Helpers — mock the AWS clients at module level inside tools.audit
# ---------------------------------------------------------------------------

def _make_mock_table():
    """Return a MagicMock DynamoDB Table that captures put_item calls."""
    table = MagicMock()
    table.put_item = MagicMock(return_value=None)
    return table


def _make_mock_kms():
    """Return a MagicMock KMS client that returns deterministic ciphertext."""
    kms = MagicMock()
    kms.encrypt = MagicMock(
        return_value={"CiphertextBlob": b"encrypted-blob"}
    )
    return kms


def _make_mock_sqs():
    """Return a MagicMock SQS client."""
    sqs = MagicMock()
    sqs.send_message = MagicMock(return_value={"MessageId": "dlq-msg-1"})
    return sqs


def _invoke_audit(event, mock_table=None, mock_kms=None, mock_sqs=None):
    """Invoke audit_log_event with mocked AWS clients.

    Returns (result, mock_table, mock_kms, mock_sqs) so callers can inspect.
    """
    mock_table = mock_table or _make_mock_table()
    mock_kms = mock_kms or _make_mock_kms()
    mock_sqs = mock_sqs or _make_mock_sqs()

    with (
        patch("tools.audit._audit_table", mock_table),
        patch("tools.audit._kms_client", mock_kms),
        patch("tools.audit._sqs_client", mock_sqs),
    ):
        # LangChain @tool expects {"event": <dict>} matching the param name
        result = audit_log_event.invoke({"event": event})

    return result, mock_table, mock_kms, mock_sqs


# ===================================================================
# Property 20 — Comprehensive Audit Logging
# All event types produce records with required metadata fields.
# Validates: Requirements 8.1, 8.2, 8.3, 8.4
# ===================================================================

REQUIRED_ITEM_KEYS = {
    "audit_id",
    "session_id",
    "timestamp",
    "patient_id",
    "trial_id",
    "event_type",
    "agent_id",
    "user_identity",
    "ttl",
}


@given(event=_base_event_st)
@settings(max_examples=50)
def test_p20_all_event_types_produce_required_metadata(event: dict):
    """Every event type must produce a DynamoDB item containing all required
    metadata keys: audit_id, session_id, timestamp, patient_id, trial_id,
    event_type, agent_id, user_identity, ttl."""
    result, mock_table, _, _ = _invoke_audit(event)

    assert result["status"] == "recorded", f"Unexpected result: {result}"
    assert "audit_id" in result

    # Inspect the item written to DynamoDB
    mock_table.put_item.assert_called_once()
    item = mock_table.put_item.call_args[1]["Item"]

    missing = REQUIRED_ITEM_KEYS - set(item.keys())
    assert not missing, f"Missing required keys {missing} for event_type={event['event_type']}"

    # Values must match the input event
    assert item["session_id"] == event["session_id"]
    assert item["patient_id"] == event["patient_id"]
    assert item["trial_id"] == event["trial_id"]
    assert item["event_type"] == event["event_type"]
    assert item["agent_id"] == event["agent_id"]
    assert item["user_identity"] == event["user_identity"]


# ===================================================================
# Property 21 — Audit Write Retry and Dead-Letter
# Retry with exponential backoff up to 3 attempts; session halt + DLQ
# on final failure.
# Validates: Requirements 8.5
# ===================================================================

@given(event=_base_event_st)
@settings(max_examples=20)
def test_p21_successful_write_no_retry(event: dict):
    """When DynamoDB write succeeds on first attempt, no retry or DLQ."""
    result, mock_table, _, mock_sqs = _invoke_audit(event)

    assert result["status"] == "recorded"
    assert mock_table.put_item.call_count == 1
    mock_sqs.send_message.assert_not_called()


@given(event=_base_event_st)
@settings(max_examples=20)
def test_p21_transient_failure_retries_then_succeeds(event: dict):
    """When first attempt fails but second succeeds, exactly 2 put_item calls
    and no DLQ message."""
    mock_table = _make_mock_table()
    mock_table.put_item.side_effect = [
        Exception("transient error"),
        None,  # success on second attempt
    ]

    with patch("tools.audit.time.sleep") as mock_sleep:
        result, _, _, mock_sqs = _invoke_audit(
            event, mock_table=mock_table
        )

    assert result["status"] == "recorded"
    assert mock_table.put_item.call_count == 2
    # First backoff should be 1 second
    mock_sleep.assert_called_once_with(1)
    mock_sqs.send_message.assert_not_called()


@given(event=_base_event_st)
@settings(max_examples=20)
def test_p21_all_retries_exhausted_halts_session_and_sends_dlq(event: dict):
    """When all 3 attempts fail, session is halted and event goes to DLQ."""
    mock_table = _make_mock_table()
    mock_table.put_item.side_effect = Exception("persistent error")

    mock_sqs = _make_mock_sqs()

    with patch("tools.audit.time.sleep") as mock_sleep:
        result, _, _, _ = _invoke_audit(
            event, mock_table=mock_table, mock_sqs=mock_sqs
        )

    # Session must be halted
    assert result.get("session_halted") is True
    assert "error" in result

    # Exactly 3 attempts
    assert mock_table.put_item.call_count == 3

    # Exponential backoff: sleep(1), sleep(2) — no sleep after last attempt
    assert mock_sleep.call_count == 2
    mock_sleep.assert_any_call(1)
    mock_sleep.assert_any_call(2)

    # DLQ message sent
    mock_sqs.send_message.assert_called_once()
    dlq_body = json.loads(
        mock_sqs.send_message.call_args[1]["MessageBody"]
    )
    assert dlq_body["retry_count"] == 3
    assert "original_audit_event" in dlq_body


# ===================================================================
# Property 22 — Audit Record Retention
# TTL must be at least 15 years from creation.
# Validates: Requirements 8.6
# ===================================================================

@given(event=_base_event_st)
@settings(max_examples=30)
def test_p22_default_ttl_at_least_15_years(event: dict):
    """Without explicit retention_years, TTL must be >= 15 years from now."""
    now = time.time()
    result, mock_table, _, _ = _invoke_audit(event)

    item = mock_table.put_item.call_args[1]["Item"]
    ttl = item["ttl"]

    min_ttl = now + 15 * SECONDS_PER_YEAR
    assert ttl >= min_ttl - 60, (
        f"TTL {ttl} is less than 15 years from now ({min_ttl})"
    )


@given(
    event=_base_event_st,
    retention=st.integers(min_value=15, max_value=100),
)
@settings(max_examples=30)
def test_p22_explicit_retention_respected(event: dict, retention: int):
    """When retention_years is provided and >= 15, TTL reflects that value."""
    event_with_retention = {**event, "retention_years": retention}
    now = time.time()
    result, mock_table, _, _ = _invoke_audit(event_with_retention)

    item = mock_table.put_item.call_args[1]["Item"]
    ttl = item["ttl"]

    expected_min = now + retention * SECONDS_PER_YEAR
    assert ttl >= expected_min - 60, (
        f"TTL {ttl} < expected {expected_min} for {retention} years"
    )


@given(
    event=_base_event_st,
    retention=st.integers(min_value=1, max_value=14),
)
@settings(max_examples=20)
def test_p22_retention_below_minimum_clamped_to_15(event: dict, retention: int):
    """When retention_years < 15, TTL must still be >= 15 years (clamped)."""
    event_with_retention = {**event, "retention_years": retention}
    now = time.time()
    result, mock_table, _, _ = _invoke_audit(event_with_retention)

    item = mock_table.put_item.call_args[1]["Item"]
    ttl = item["ttl"]

    min_ttl = now + 15 * SECONDS_PER_YEAR
    assert ttl >= min_ttl - 60, (
        f"TTL {ttl} should be clamped to 15-year minimum, got less"
    )


# ===================================================================
# Property 13 — PHI Encryption at Rest
# PHI stored in phi_encrypted via KMS, never in event_data.
# Validates: Requirements 4.4
# ===================================================================

@given(event=_base_event_st, phi=_phi_data_st)
@settings(max_examples=30)
def test_p13_phi_stored_encrypted_not_in_event_data(event: dict, phi: dict):
    """When phi_data is provided, it must be KMS-encrypted in phi_encrypted
    and must NOT appear in event_data."""
    event_with_phi = {**event, "phi_data": phi}
    result, mock_table, mock_kms, _ = _invoke_audit(event_with_phi)

    assert result["status"] == "recorded"

    item = mock_table.put_item.call_args[1]["Item"]

    # phi_encrypted must be present and be the KMS ciphertext
    assert "phi_encrypted" in item, "phi_encrypted field missing from item"
    assert item["phi_encrypted"] == b"encrypted-blob"

    # KMS encrypt must have been called with the trial-specific key
    mock_kms.encrypt.assert_called_once()
    call_kwargs = mock_kms.encrypt.call_args[1]
    assert event["trial_id"] in call_kwargs["KeyId"]

    # event_data must NOT contain any PHI values
    event_data = item.get("event_data", {})
    event_data_str = json.dumps(event_data, default=str).lower()
    for key, value in phi.items():
        v = str(value).lower()
        if len(v) < 4:
            continue  # skip short values that false-match JSON keywords (true/false/null)
        assert v not in event_data_str, (
            f"PHI field '{key}' found in event_data (unencrypted)"
        )


@given(event=_base_event_st)
@settings(max_examples=20)
def test_p13_no_phi_means_no_encrypted_field(event: dict):
    """When no phi_data is provided, phi_encrypted should not be in the item."""
    result, mock_table, mock_kms, _ = _invoke_audit(event)

    item = mock_table.put_item.call_args[1]["Item"]
    assert "phi_encrypted" not in item
    mock_kms.encrypt.assert_not_called()


@given(event=_base_event_st, phi=_phi_data_st)
@settings(max_examples=10)
def test_p13_kms_failure_does_not_leak_phi(event: dict, phi: dict):
    """When KMS encryption fails, PHI must still not appear in event_data."""
    event_with_phi = {**event, "phi_data": phi}

    mock_kms = _make_mock_kms()
    mock_kms.encrypt.side_effect = Exception("KMS unavailable")

    result, mock_table, _, _ = _invoke_audit(
        event_with_phi, mock_kms=mock_kms
    )

    assert result["status"] == "recorded"

    item = mock_table.put_item.call_args[1]["Item"]

    # phi_encrypted should NOT be present (encryption failed)
    assert "phi_encrypted" not in item

    # PHI values must still not be in event_data
    event_data = item.get("event_data", {})
    event_data_str = json.dumps(event_data, default=str).lower()
    for key, value in phi.items():
        v = str(value).lower()
        if len(v) < 4:
            continue  # skip short values that false-match JSON keywords (true/false/null)
        assert v not in event_data_str, (
            f"PHI field '{key}' leaked into event_data after KMS failure"
        )
