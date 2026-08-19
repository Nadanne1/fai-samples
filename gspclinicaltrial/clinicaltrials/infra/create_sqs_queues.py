"""Create SQS queues with dead-letter queues for the Clinical Trial Screening & Monitoring System.

Creates:
  - TrialScreeningIntakeQueue with DLQ TrialScreeningIntakeDLQ (maxReceiveCount: 3)
  - TrialEscalationQueue with DLQ TrialEscalationDLQ (maxReceiveCount: 3)
  - AuditDeadLetterQueue (standalone — receives failed audit writes)

Queue attributes (reused from 1.10-redesigned-agent/queues/create-queues.sh):
  - MessageRetentionPeriod: 1209600 (14 days)
  - VisibilityTimeout: 300 (5 minutes)
  - ReceiveMessageWaitTimeSeconds: 20 (long polling)

Requirements: 6.1, 6.2, 8.5
"""

import json
import sys

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, ".")
from config.settings import (
    AUDIT_DLQ,
    AWS_REGION,
    ESCALATION_DLQ,
    ESCALATION_QUEUE,
    REQUIRED_TAGS,
    SCREENING_INTAKE_DLQ,
    SCREENING_INTAKE_QUEUE,
)

# Standard queue attributes (from 1.10-redesigned-agent/queues/create-queues.sh)
_BASE_ATTRIBUTES = {
    "MessageRetentionPeriod": "1209600",
    "VisibilityTimeout": "300",
    "ReceiveMessageWaitTimeSeconds": "20",
}


def _format_tags(tags: dict) -> dict:
    """Convert a tag dict to the SQS tag format (flat key-value dict)."""
    return {k: v for k, v in tags.items()}


def _get_queue_arn(client, queue_url: str) -> str:
    """Retrieve the ARN for an SQS queue given its URL."""
    resp = client.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["QueueArn"]
    )
    return resp["Attributes"]["QueueArn"]


def _create_queue(client, name: str, attributes: dict | None = None) -> str:
    """Create an SQS queue and apply required tags. Returns the queue URL."""
    attrs = {**_BASE_ATTRIBUTES, **(attributes or {})}
    print(f"Creating queue: {name}")
    try:
        resp = client.create_queue(QueueName=name, Attributes=attrs)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "QueueAlreadyExists":
            print(f"  Queue {name} already exists — fetching URL.")
            resp = client.get_queue_url(QueueName=name)
            queue_url = resp["QueueUrl"]
            client.tag_queue(QueueUrl=queue_url, Tags=_format_tags(REQUIRED_TAGS))
            return queue_url
        raise

    queue_url = resp["QueueUrl"]
    client.tag_queue(QueueUrl=queue_url, Tags=_format_tags(REQUIRED_TAGS))
    print(f"  ✅ {name} created: {queue_url}")
    return queue_url


def _create_queue_with_dlq(
    client, queue_name: str, dlq_name: str, max_receive_count: int = 3
) -> tuple[str, str]:
    """Create a DLQ first, then the main queue with a RedrivePolicy pointing to it."""
    dlq_url = _create_queue(client, dlq_name)
    dlq_arn = _get_queue_arn(client, dlq_url)

    redrive_policy = json.dumps(
        {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": max_receive_count}
    )
    main_url = _create_queue(
        client, queue_name, attributes={"RedrivePolicy": redrive_policy}
    )
    print(f"  ↳ {queue_name} → DLQ {dlq_name} (maxReceiveCount={max_receive_count})")
    return main_url, dlq_url


def main() -> None:
    """Create all SQS queues for the clinical trials system."""
    print(f"Connecting to SQS in {AWS_REGION}...")
    client = boto3.client("sqs", region_name=AWS_REGION)

    # 1. TrialScreeningIntakeQueue + DLQ (Requirement 6.1)
    intake_url, intake_dlq_url = _create_queue_with_dlq(
        client, SCREENING_INTAKE_QUEUE, SCREENING_INTAKE_DLQ, max_receive_count=3
    )

    # 2. TrialEscalationQueue + DLQ (Requirement 6.2)
    escalation_url, escalation_dlq_url = _create_queue_with_dlq(
        client, ESCALATION_QUEUE, ESCALATION_DLQ, max_receive_count=3
    )

    # 3. AuditDeadLetterQueue — standalone (Requirement 8.5)
    audit_dlq_url = _create_queue(client, AUDIT_DLQ)

    print("\n=========================================")
    print("✅ All SQS queues created")
    print("=========================================")
    print(f"  Intake Queue:      {intake_url}")
    print(f"  Intake DLQ:        {intake_dlq_url}")
    print(f"  Escalation Queue:  {escalation_url}")
    print(f"  Escalation DLQ:    {escalation_dlq_url}")
    print(f"  Audit DLQ:         {audit_dlq_url}")


if __name__ == "__main__":
    main()
