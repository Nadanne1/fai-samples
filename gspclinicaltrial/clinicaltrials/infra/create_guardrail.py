#!/usr/bin/env python3
"""Create the Bedrock PHI protection guardrail for clinical trial agents.

Creates a guardrail named 'clinical-trial-screening' with sensitive information
filters for PHI (patient ID, date of birth, medical records, SSN, etc.).
Idempotent — skips creation if already exists and returns the existing ID.

Usage:
    python infra/create_guardrail.py [--region REGION]

Prints the guardrail ID to stdout on success so deploy.sh can capture it.
"""

import argparse
import sys

import boto3
from botocore.exceptions import ClientError

GUARDRAIL_NAME = "clinical-trial-screening"
GUARDRAIL_DESCRIPTION = "PHI protection for clinical trial screening and monitoring agents"


def get_existing_guardrail(client, name: str) -> tuple[str, str] | tuple[None, None]:
    """Return (guardrail_id, version) if a guardrail with this name exists."""
    paginator = client.get_paginator("list_guardrails")
    for page in paginator.paginate():
        for g in page.get("guardrails", []):
            if g["name"] == name:
                return g["id"], g.get("version", "DRAFT")
    return None, None


def create_guardrail(client) -> tuple[str, str]:
    """Create the PHI guardrail and return (guardrail_id, version)."""
    response = client.create_guardrail(
        name=GUARDRAIL_NAME,
        description=GUARDRAIL_DESCRIPTION,
        sensitiveInformationPolicyConfig={
            "piiEntitiesConfig": [
                {"type": "NAME", "action": "ANONYMIZE"},
                {"type": "EMAIL", "action": "ANONYMIZE"},
                {"type": "PHONE", "action": "ANONYMIZE"},
                {"type": "US_SOCIAL_SECURITY_NUMBER", "action": "BLOCK"},
                {"type": "AGE", "action": "ANONYMIZE"},
                {"type": "ADDRESS", "action": "ANONYMIZE"},
                {"type": "DRIVER_ID", "action": "ANONYMIZE"},
                {"type": "PASSWORD", "action": "BLOCK"},
                {"type": "USERNAME", "action": "ANONYMIZE"},
                {"type": "DATE_TIME", "action": "ANONYMIZE"},
                {"type": "INTERNET_URL", "action": "ANONYMIZE"},
            ],
        },
        blockedInputMessaging="This request contains sensitive patient information that cannot be processed.",
        blockedOutputsMessaging="The response contained sensitive patient information and has been blocked.",
    )
    guardrail_id = response["guardrailId"]

    # Create a numbered version so ECS containers can pin to it
    version_response = client.create_guardrail_version(
        guardrailIdentifier=guardrail_id,
        description="Initial version — PHI protection for clinical trial agents",
    )
    version = version_response["version"]
    return guardrail_id, version


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Bedrock PHI guardrail")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    client = boto3.client("bedrock", region_name=args.region)

    guardrail_id, version = get_existing_guardrail(client, GUARDRAIL_NAME)
    if guardrail_id:
        print(f"Already exists: {GUARDRAIL_NAME} — id={guardrail_id} version={version}", file=sys.stderr)
    else:
        print(f"Creating guardrail: {GUARDRAIL_NAME}...", file=sys.stderr)
        guardrail_id, version = create_guardrail(client)
        print(f"Created: {GUARDRAIL_NAME} — id={guardrail_id} version={version}", file=sys.stderr)

    # Print ID and version on stdout for shell capture
    print(f"{guardrail_id} {version}")


if __name__ == "__main__":
    main()
