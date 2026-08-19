"""Seed TrialAuditLog with realistic decision_made records for analytics demo.

Run from clinicaltrials/ directory:
  python3 infra/seed_analytics.py

Clears old records first, then writes 5 per trial with varied determinations.
"""

import json
import time
import uuid
from datetime import datetime, timezone, timedelta

import boto3

REGION = "us-east-1"
TABLE = "TrialAuditLog"

# 10 real patient IDs from clinicaltrials360 HealthLake
PATIENTS = [
    ("030f01b8-1e8b-499a-b04e-6153622fe875", "Alyse Gusikowski"),
    ("0704439a-e0aa-4513-9bcd-62486607f81b", "Timothy Conroy"),
    ("0b9dc6cb-86fc-4dfc-aa2e-9cb5f0eaedb5", "Dale Ankunding"),
    ("14477314-0a37-4ecf-8ca5-a8a6441b79ba", "Andrew Krajcik"),
    ("1489348a-c52b-41f9-be77-7b515b5833db", "Sherilyn Gerhold"),
    ("14c44443-9d90-4a06-bd93-0477983d2b45", "Javier Jacobs"),
    ("1e1e61ea-c70d-4c11-935d-05ed835511fe", "Omar Jenkins"),
    ("2412f0d3-e6c2-48ca-b63f-765da13236ba", "Kelley Ernser"),
    ("28606106-4198-404d-ad5e-6778d13fe2bf", "Maryann Wiza"),
    ("29ed3507-42dd-4233-97f1-e9ff75166fdd", "Dacia Glover"),
]

# 5 per trial: mix of eligible / ineligible / borderline for visual variety
TRIALS = [
    {
        "trial_id": "NCT00000001",
        "determinations": ["eligible", "eligible", "ineligible", "borderline", "eligible"],
    },
    {
        "trial_id": "NCT00000002",
        "determinations": ["ineligible", "eligible", "eligible", "ineligible", "borderline"],
    },
]

TTL_YEARS = 15
SECONDS_PER_YEAR = 365.25 * 24 * 3600


def seed():
    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.Table(TABLE)

    # Delete all existing items first (paginated scan)
    print(f"Clearing existing records from {TABLE}...")
    scan_kwargs: dict = {
        "ProjectionExpression": "session_id, #ts",
        "ExpressionAttributeNames": {"#ts": "timestamp"},
    }
    deleted = 0
    with table.batch_writer() as batch:
        while True:
            scan = table.scan(**scan_kwargs)
            for item in scan.get("Items", []):
                ts = item.get("timestamp")
                if ts is None:
                    continue
                batch.delete_item(Key={"session_id": item["session_id"], "timestamp": ts})
                deleted += 1
            last = scan.get("LastEvaluatedKey")
            if not last:
                break
            scan_kwargs["ExclusiveStartKey"] = last
    print(f"  Deleted {deleted} old records.")

    # Write 5 new decision_made records per trial
    now = datetime.now(timezone.utc)
    written = 0
    for trial in TRIALS:
        tid = trial["trial_id"]
        for i, det in enumerate(trial["determinations"]):
            patient_id, patient_name = PATIENTS[i]
            session_id = f"scr_{uuid.uuid4().hex[:12]}"
            # Spread timestamps across last 14 days
            ts = (now - timedelta(days=13 - i * 2)).isoformat()
            item = {
                "audit_id": str(uuid.uuid4()),
                "session_id": session_id,
                "timestamp": ts,
                "patient_id": patient_id,
                "trial_id": tid,
                "event_type": "decision_made",
                "agent_id": "screening-agent",
                "user_identity": "screening-agent",
                "eligibility_determination": det,
                "event_data": {
                    "determination": det,
                    "patient_name": patient_name,
                    "criteria_count": 6,
                },
                "fhir_resource_refs": [],
                "ttl": int(time.time() + TTL_YEARS * SECONDS_PER_YEAR),
            }
            table.put_item(Item=item)
            print(f"  [{tid}] {patient_name:<25} → {det}")
            written += 1

    print(f"\n✓ Seeded {written} records across {len(TRIALS)} trials.")


if __name__ == "__main__":
    seed()
