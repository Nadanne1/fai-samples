"""Audit logging tool for 21 CFR Part 11 compliant trial audit trails.

Writes audit records to the TrialAuditLog DynamoDB table with composite key
(session_id, timestamp). Encrypts PHI fields with KMS. Implements exponential
backoff retry (3 attempts: 1s, 2s, 4s) with dead-letter queue fallback.
Supports electronic signature capture for PI review decisions.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone

import boto3
from langchain_core.tools import tool

from config.settings import (
    AUDIT_DLQ,
    AUDIT_LOG_TABLE,
    AUDIT_RETRY_ATTEMPTS,
    AUDIT_RETRY_BACKOFF_SECONDS,
    AWS_ACCOUNT_ID,
    AWS_REGION,
    MIN_RETENTION_YEARS,
)

logger = logging.getLogger(__name__)

_dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
_kms_client = boto3.client("kms", region_name=AWS_REGION)
_sqs_client = boto3.client("sqs", region_name=AWS_REGION)

_audit_table = _dynamodb.Table(AUDIT_LOG_TABLE)


def _get_audit_dlq_url() -> str:
    """Resolve the audit DLQ URL at runtime to avoid empty AWS_ACCOUNT_ID at import time."""
    if AWS_ACCOUNT_ID:
        return f"https://sqs.{AWS_REGION}.amazonaws.com/{AWS_ACCOUNT_ID}/{AUDIT_DLQ}"
    account_id = boto3.client("sts", region_name=AWS_REGION).get_caller_identity()["Account"]
    return f"https://sqs.{AWS_REGION}.amazonaws.com/{account_id}/{AUDIT_DLQ}"

# Seconds per year (approximate) for TTL calculation
_SECONDS_PER_YEAR = 365.25 * 24 * 3600


def _encrypt_phi(phi_data: dict, trial_id: str) -> bytes:
    """Encrypt PHI data using KMS with the trial-specific key alias.

    Args:
        phi_data: Dict containing PHI fields to encrypt.
        trial_id: Trial identifier used to resolve the KMS key alias.

    Returns:
        KMS-encrypted ciphertext blob as bytes.
    """
    key_alias = "alias/clinical-trial-screening/phi"
    response = _kms_client.encrypt(
        KeyId=key_alias,
        Plaintext=json.dumps(phi_data).encode("utf-8"),
    )
    return response["CiphertextBlob"]


def _compute_ttl(retention_years: int | None = None) -> int:
    """Compute TTL epoch seconds from now + retention period."""
    years = max(retention_years or MIN_RETENTION_YEARS, MIN_RETENTION_YEARS)
    return int(time.time() + years * _SECONDS_PER_YEAR)


def _send_to_dlq(event: dict, failure_reason: str, retry_count: int) -> None:
    """Send a failed audit event to the AuditDeadLetterQueue."""
    try:
        dlq_message = {
            "original_audit_event": event,
            "failure_reason": failure_reason,
            "retry_count": retry_count,
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }
        _sqs_client.send_message(
            QueueUrl=_get_audit_dlq_url(),
            MessageBody=json.dumps(dlq_message, default=str),
        )
        logger.info("Audit event sent to DLQ after %d retries", retry_count)
    except Exception:
        logger.exception("CRITICAL: Failed to send audit event to DLQ")


@tool
def audit_log_event(event: dict) -> dict:
    """Write an audit record to the TrialAuditLog DynamoDB table.

    Implements 21 CFR Part 11 compliant audit logging with KMS-encrypted PHI,
    electronic signature support, and exponential backoff retry.

    Args:
        event: Audit event dict with required keys:
            - session_id (str): Screening/monitoring session ID (partition key).
            - event_type (str): One of question_asked, response_received,
              validation_performed, decision_made, data_access, escalation,
              phi_violation, electronic_signature, audit_write_failure.
            - patient_id (str): Patient UUID.
            - trial_id (str): NCT number or internal trial ID.
            - agent_id (str): Agent identifier.
            - user_identity (str): Agent or human user identity.
            Optional keys:
            - event_data (dict): Event-specific details (non-PHI).
            - fhir_resource_refs (list[str]): FHIR resource references accessed.
            - phi_data (dict): PHI fields to encrypt with KMS (stored separately).
            - signature (dict): Electronic signature with signer_id,
              signature_timestamp, and meaning for 21 CFR Part 11.
            - retention_years (int): Override retention period (min 15 years).

    Returns:
        Dict with 'audit_id', 'status' on success, or 'error' and
        'session_halted' on final failure.
    """
    now = datetime.now(timezone.utc)
    audit_id = str(uuid.uuid4())
    timestamp = now.isoformat()

    # Build the DynamoDB item
    item = {
        "audit_id": audit_id,
        "session_id": event["session_id"],
        "timestamp": timestamp,
        "patient_id": event.get("patient_id", ""),
        "trial_id": event.get("trial_id", ""),
        "event_type": event["event_type"],
        "agent_id": event.get("agent_id", ""),
        "user_identity": event.get("user_identity", ""),
        "event_data": event.get("event_data", {}),
        "fhir_resource_refs": event.get("fhir_resource_refs", []),
        "ttl": _compute_ttl(event.get("retention_years")),
    }

    # Encrypt PHI if present — stored in phi_encrypted, NOT in event_data
    phi_data = event.get("phi_data")
    if phi_data and event.get("trial_id"):
        try:
            item["phi_encrypted"] = _encrypt_phi(phi_data, event["trial_id"])
        except Exception:
            logger.exception("CRITICAL: PHI encryption failed — halting audit write for patient safety")
            # Try to send to DLQ for retry, but do NOT write plaintext PHI to DynamoDB
            try:
                _send_to_dlq(event, "phi_encryption_failed", 0)
            except Exception:
                logger.exception("DLQ send also failed — audit record lost")
            return {
                "error": "PHI encryption failed — audit write halted",
                "audit_id": audit_id,
                "session_halted": True,
            }

    # Electronic signature for 21 CFR Part 11
    signature = event.get("signature")
    if signature:
        item["signature"] = {
            "signer_id": signature["signer_id"],
            "signature_timestamp": signature.get(
                "signature_timestamp", timestamp
            ),
            "meaning": signature["meaning"],
        }

    # Retry with exponential backoff: 1s, 2s, 4s
    last_error = None
    for attempt in range(AUDIT_RETRY_ATTEMPTS):
        try:
            _audit_table.put_item(Item=item)
            return {"audit_id": audit_id, "status": "recorded"}
        except Exception as exc:
            last_error = exc
            if attempt < AUDIT_RETRY_ATTEMPTS - 1:
                backoff = AUDIT_RETRY_BACKOFF_SECONDS[attempt]
                logger.warning(
                    "Audit write attempt %d failed, retrying in %ds: %s",
                    attempt + 1,
                    backoff,
                    exc,
                )
                time.sleep(backoff)

    # All retries exhausted — halt session and send to DLQ
    logger.error(
        "Audit write failed after %d attempts. Halting session %s.",
        AUDIT_RETRY_ATTEMPTS,
        event.get("session_id"),
    )
    _send_to_dlq(event, str(last_error), AUDIT_RETRY_ATTEMPTS)

    return {
        "error": "Audit write failed after all retries",
        "session_halted": True,
        "audit_id": audit_id,
    }
