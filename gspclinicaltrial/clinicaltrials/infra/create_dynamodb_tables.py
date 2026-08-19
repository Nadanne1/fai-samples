"""Create DynamoDB tables for the Clinical Trial Screening & Monitoring System.

Creates:
  - TrialAuditLog: Regulatory audit trail with GSIs on patient_id and trial_id,
    KMS encryption, and TTL for retention enforcement.
  - TrialProtocolConfig: Trial protocol configuration storage.

Requirements: 8.3 (composite key + GSIs), 8.6 (retention/TTL), 4.4 (KMS encryption)
"""

import sys
import time

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, ".")
from config.settings import (
    AUDIT_LOG_TABLE,
    AWS_REGION,
    DEVOPS_TESTS_TABLE,
    PROTOCOL_CONFIG_TABLE,
    REQUIRED_TAGS,
    SCREENING_RULES_TABLE,
)


def _format_tags(tags: dict) -> list[dict]:
    """Convert a tag dict to the DynamoDB Tags format."""
    return [{"Key": k, "Value": v} for k, v in tags.items()]


def _wait_for_table(client, table_name: str, max_wait: int = 120) -> None:
    """Poll until a table reaches ACTIVE status."""
    print(f"  Waiting for {table_name} to become ACTIVE...")
    start = time.time()
    while time.time() - start < max_wait:
        resp = client.describe_table(TableName=table_name)
        status = resp["Table"]["TableStatus"]
        if status == "ACTIVE":
            print(f"  {table_name} is ACTIVE.")
            return
        time.sleep(2)
    raise TimeoutError(f"{table_name} did not become ACTIVE within {max_wait}s")


def create_trial_audit_log_table(client) -> None:
    """Create the TrialAuditLog table with GSIs, KMS encryption, and TTL.

    Schema:
      PK: session_id (S)  SK: timestamp (S)
      GSI-1: patient_id-index  (patient_id, timestamp)
      GSI-2: trial_id-index    (trial_id, timestamp)
    """
    print(f"Creating table: {AUDIT_LOG_TABLE}")
    try:
        client.create_table(
            TableName=AUDIT_LOG_TABLE,
            KeySchema=[
                {"AttributeName": "session_id", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "session_id", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
                {"AttributeName": "patient_id", "AttributeType": "S"},
                {"AttributeName": "trial_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "patient_id-index",
                    "KeySchema": [
                        {"AttributeName": "patient_id", "KeyType": "HASH"},
                        {"AttributeName": "timestamp", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "trial_id-index",
                    "KeySchema": [
                        {"AttributeName": "trial_id", "KeyType": "HASH"},
                        {"AttributeName": "timestamp", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
            SSESpecification={
                "Enabled": True,
                "SSEType": "KMS",
            },
            Tags=_format_tags(REQUIRED_TAGS),
        )
    except client.exceptions.ResourceInUseException:
        print(f"  Table {AUDIT_LOG_TABLE} already exists — skipping creation.")
        return

    _wait_for_table(client, AUDIT_LOG_TABLE)

    # Enable TTL for retention enforcement (Requirement 8.6)
    print(f"  Enabling TTL on {AUDIT_LOG_TABLE} (attribute: ttl)")
    client.update_time_to_live(
        TableName=AUDIT_LOG_TABLE,
        TimeToLiveSpecification={
            "Enabled": True,
            "AttributeName": "ttl",
        },
    )
    print(f"  {AUDIT_LOG_TABLE} created successfully.")


def create_trial_protocol_config_table(client) -> None:
    """Create the TrialProtocolConfig table.

    Schema:
      PK: trial_id (S)  SK: version (S)
    """
    print(f"Creating table: {PROTOCOL_CONFIG_TABLE}")
    try:
        client.create_table(
            TableName=PROTOCOL_CONFIG_TABLE,
            KeySchema=[
                {"AttributeName": "trial_id", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "trial_id", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            Tags=_format_tags(REQUIRED_TAGS),
        )
    except client.exceptions.ResourceInUseException:
        print(f"  Table {PROTOCOL_CONFIG_TABLE} already exists — skipping creation.")
        return

    _wait_for_table(client, PROTOCOL_CONFIG_TABLE)
    print(f"  {PROTOCOL_CONFIG_TABLE} created successfully.")


def create_trial_screening_rules_table(client) -> None:
    """Create the TrialScreeningRules table.

    Schema:
      PK: id (S)
    """
    print(f"Creating table: {SCREENING_RULES_TABLE}")
    try:
        client.create_table(
            TableName=SCREENING_RULES_TABLE,
            KeySchema=[
                {"AttributeName": "id", "KeyType": "HASH"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            Tags=_format_tags(REQUIRED_TAGS),
        )
    except client.exceptions.ResourceInUseException:
        # Verify the existing table has the right PK
        try:
            desc = client.describe_table(TableName=SCREENING_RULES_TABLE)
            existing_pk = desc["Table"]["KeySchema"][0]["AttributeName"]
            if existing_pk != "id":
                print(f"  ⚠ {SCREENING_RULES_TABLE} has wrong PK '{existing_pk}' — deleting and recreating...")
                client.delete_table(TableName=SCREENING_RULES_TABLE)
                time.sleep(5)
                create_trial_screening_rules_table(client)
            else:
                print(f"  Table {SCREENING_RULES_TABLE} already exists with correct PK — skipping.")
        except Exception as e:
            print(f"  Warning: Could not verify {SCREENING_RULES_TABLE} schema: {e}")
        return

    _wait_for_table(client, SCREENING_RULES_TABLE)
    print(f"  {SCREENING_RULES_TABLE} created successfully.")


def create_devops_tests_table(client) -> None:
    """Create the TrialDevOpsTests table.

    Schema:
      PK: flow_id (S), SK: test_id (S)
    """
    print(f"Creating table: {DEVOPS_TESTS_TABLE}")
    try:
        client.create_table(
            TableName=DEVOPS_TESTS_TABLE,
            KeySchema=[
                {"AttributeName": "flow_id", "KeyType": "HASH"},
                {"AttributeName": "test_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "flow_id", "AttributeType": "S"},
                {"AttributeName": "test_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            Tags=_format_tags(REQUIRED_TAGS),
        )
    except client.exceptions.ResourceInUseException:
        print(f"  Table {DEVOPS_TESTS_TABLE} already exists — skipping.")
        return
    _wait_for_table(client, DEVOPS_TESTS_TABLE)
    print(f"  {DEVOPS_TESTS_TABLE} created successfully.")


def main() -> None:
    """Create all DynamoDB tables for the clinical trials system."""
    print(f"Connecting to DynamoDB in {AWS_REGION}...")
    client = boto3.client("dynamodb", region_name=AWS_REGION)

    create_trial_audit_log_table(client)
    create_trial_protocol_config_table(client)
    create_trial_screening_rules_table(client)
    create_devops_tests_table(client)

    print("\nAll DynamoDB tables created.")
    print(f"  - {AUDIT_LOG_TABLE}")
    print(f"  - {PROTOCOL_CONFIG_TABLE}")
    print(f"  - {SCREENING_RULES_TABLE}")
    print(f"  - {DEVOPS_TESTS_TABLE}")


if __name__ == "__main__":
    main()
