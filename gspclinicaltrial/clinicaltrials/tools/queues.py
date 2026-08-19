"""SQS queue tools for clinical trial screening intake and escalation.

Sends messages to TrialScreeningIntakeQueue and TrialEscalationQueue
with structured message attributes for routing and priority handling.
Reuses patterns from 1.10-redesigned-agent/queues/queue_manager.py.
"""

import json
import logging
from datetime import datetime, timezone

import boto3
from langchain_core.tools import tool

from config.settings import (
    AWS_ACCOUNT_ID,
    AWS_REGION,
    ESCALATION_QUEUE,
    SCREENING_INTAKE_QUEUE,
)

logger = logging.getLogger(__name__)

_sqs_client = boto3.client("sqs", region_name=AWS_REGION)

_resolved_account_id: str | None = None


def _get_account_id() -> str:
    global _resolved_account_id
    if not _resolved_account_id:
        _resolved_account_id = (
            AWS_ACCOUNT_ID
            or boto3.client("sts", region_name=AWS_REGION).get_caller_identity()["Account"]
        )
    return _resolved_account_id


def _queue_url(queue_name: str) -> str:
    return f"https://sqs.{AWS_REGION}.amazonaws.com/{_get_account_id()}/{queue_name}"


def _get_queue_urls() -> dict:
    return {
        SCREENING_INTAKE_QUEUE: _queue_url(SCREENING_INTAKE_QUEUE),
        ESCALATION_QUEUE: _queue_url(ESCALATION_QUEUE),
    }


def _build_message_attributes(attributes: dict) -> dict:
    """Convert a flat dict of string attributes to SQS MessageAttributes format."""
    return {
        key: {"StringValue": str(value), "DataType": "String"}
        for key, value in attributes.items()
        if value is not None
    }


@tool
def sqs_send_message(queue_name: str, message: dict, attributes: dict | None = None) -> dict:
    """Send a message to an SQS queue (TrialScreeningIntakeQueue or TrialEscalationQueue).

    Args:
        queue_name: The queue name — must be one of
            'TrialScreeningIntakeQueue' or 'TrialEscalationQueue'.
        message: The message body as a dict. Will be JSON-serialized.
        attributes: Optional message attributes dict. Supported keys:
            Priority, Urgency, TrialId, PatientId, EscalationReason.

    Returns:
        Dict with 'message_id', 'queue_name', and 'sent_at' on success,
        or 'error' key on failure.
    """
    queue_url = _get_queue_urls().get(queue_name)
    if not queue_url:
        return {"error": f"Unknown queue: {queue_name}. Use '{SCREENING_INTAKE_QUEUE}' or '{ESCALATION_QUEUE}'."}

    # Inject timestamp into message body
    message["sent_at"] = datetime.now(timezone.utc).isoformat()

    try:
        kwargs: dict = {
            "QueueUrl": queue_url,
            "MessageBody": json.dumps(message),
        }
        if attributes:
            kwargs["MessageAttributes"] = _build_message_attributes(attributes)

        response = _sqs_client.send_message(**kwargs)

        return {
            "message_id": response["MessageId"],
            "queue_name": queue_name,
            "sent_at": message["sent_at"],
        }
    except Exception:
        logger.exception("Error sending message to %s", queue_name)
        return {"error": f"Failed to send message to {queue_name}"}
