"""Seed trial protocol config and FHIR Questionnaire resources.

- Writes Questionnaire FHIR resources to clinicaltrials360 HealthLake
- Updates TrialProtocolConfig DynamoDB with questionnaire_id refs
- Fixes missing nct_metadata so /api/trials returns proper titles

Run from clinicaltrials/ directory:
  python3 infra/seed_trials.py
"""

import json
import os
import sys
import time
import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = os.environ.get("AWS_REGION", "us-east-1")
DATASTORE_ID = os.environ.get("HEALTHLAKE_DATASTORE_ID", "")
if not DATASTORE_ID:
    print("ERROR: HEALTHLAKE_DATASTORE_ID env var is required")
    sys.exit(1)
FHIR_BASE = f"https://healthlake.{REGION}.amazonaws.com/datastore/{DATASTORE_ID}/r4"
TABLE = "TrialProtocolConfig"

TRIALS = [
    {
        "trial_id": "NCT00000001",
        "title": "A Phase III Study of Empagliflozin in Patients with Type 2 Diabetes",
        "phase": "Phase 3",
        "status": "active",
        "conditions": ["Type 2 Diabetes Mellitus"],
        "interventions": ["Empagliflozin 10mg", "Empagliflozin 25mg", "Placebo"],
        "questionnaire": {
            "resourceType": "Questionnaire",
            "status": "active",
            "title": "Empagliflozin T2D Eligibility Screening",
            "identifier": [{"system": "https://clinicaltrials.gov", "value": "NCT00000001"}],
            "item": [
                {
                    "linkId": "1",
                    "text": "Has the patient been diagnosed with Type 2 Diabetes Mellitus?",
                    "type": "boolean",
                    "required": True,
                },
                {
                    "linkId": "2",
                    "text": "What is the patient's most recent HbA1c value (%)?",
                    "type": "decimal",
                    "required": True,
                    "extension": [{
                        "url": "http://hl7.org/fhir/StructureDefinition/minValue",
                        "valueDecimal": 7.0,
                    }, {
                        "url": "http://hl7.org/fhir/StructureDefinition/maxValue",
                        "valueDecimal": 12.0,
                    }],
                },
                {
                    "linkId": "3",
                    "text": "Is the patient currently taking metformin or other antidiabetic medications?",
                    "type": "boolean",
                    "required": True,
                },
                {
                    "linkId": "4",
                    "text": "Does the patient have an eGFR >= 45 mL/min/1.73m²?",
                    "type": "boolean",
                    "required": True,
                },
                {
                    "linkId": "5",
                    "text": "Does the patient have a history of heart failure (NYHA Class III-IV)?",
                    "type": "boolean",
                    "required": False,
                },
                {
                    "linkId": "6",
                    "text": "Is the patient between 18 and 75 years of age?",
                    "type": "boolean",
                    "required": True,
                },
            ],
        },
    },
    {
        "trial_id": "NCT00000002",
        "title": "A Phase II Study of Novel ARB in Essential Hypertension",
        "phase": "Phase 2",
        "status": "active",
        "conditions": ["Essential Hypertension"],
        "interventions": ["Novel ARB 40mg", "Novel ARB 80mg", "Losartan 50mg"],
        "questionnaire": {
            "resourceType": "Questionnaire",
            "status": "active",
            "title": "Novel ARB Hypertension Eligibility Screening",
            "identifier": [{"system": "https://clinicaltrials.gov", "value": "NCT00000002"}],
            "item": [
                {
                    "linkId": "1",
                    "text": "Does the patient have a documented diagnosis of Essential Hypertension?",
                    "type": "boolean",
                    "required": True,
                },
                {
                    "linkId": "2",
                    "text": "What is the patient's average systolic blood pressure (mmHg)?",
                    "type": "decimal",
                    "required": True,
                    "extension": [{
                        "url": "http://hl7.org/fhir/StructureDefinition/minValue",
                        "valueDecimal": 140.0,
                    }, {
                        "url": "http://hl7.org/fhir/StructureDefinition/maxValue",
                        "valueDecimal": 180.0,
                    }],
                },
                {
                    "linkId": "3",
                    "text": "Is the patient currently on an ACE inhibitor or ARB?",
                    "type": "boolean",
                    "required": True,
                },
                {
                    "linkId": "4",
                    "text": "Does the patient have a serum creatinine < 2.0 mg/dL?",
                    "type": "boolean",
                    "required": True,
                },
                {
                    "linkId": "5",
                    "text": "Does the patient have a history of angioedema related to ACE inhibitors?",
                    "type": "boolean",
                    "required": False,
                },
                {
                    "linkId": "6",
                    "text": "Is the patient between 18 and 70 years of age?",
                    "type": "boolean",
                    "required": True,
                },
            ],
        },
    },
]


def hl_put(resource: dict) -> dict:
    session = boto3.Session()
    creds = session.get_credentials().get_frozen_credentials()
    rtype = resource["resourceType"]
    url = f"{FHIR_BASE}/{rtype}"
    body = json.dumps(resource).encode("utf-8")
    req = AWSRequest(method="POST", url=url, data=body,
                     headers={"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"})
    SigV4Auth(creds, "healthlake", REGION).add_auth(req)
    r = requests.post(url, data=body, headers=dict(req.headers))
    r.raise_for_status()
    return r.json()


def seed():
    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.Table(TABLE)

    for trial in TRIALS:
        tid = trial["trial_id"]
        print(f"\n[{tid}] {trial['title']}")

        # Write Questionnaire to HealthLake
        print(f"  Writing FHIR Questionnaire to HealthLake...")
        q_resource = trial["questionnaire"]
        created = hl_put(q_resource)
        q_id = created.get("id", "")
        print(f"  ✓ Questionnaire/{q_id} created ({len(q_resource['item'])} items)")

        # Update TrialProtocolConfig
        print(f"  Updating TrialProtocolConfig...")
        table.put_item(Item={
            "trial_id": tid,
            "version": "1.0",
            "status": "active",
            "questionnaire_id": f"Questionnaire/{q_id}",
            "nct_metadata": {
                "title": trial["title"],
                "phase": trial["phase"],
                "conditions": trial["conditions"],
                "interventions": trial["interventions"],
                "nct_number": tid,
            },
        })
        print(f"  ✓ TrialProtocolConfig updated with questionnaire_id=Questionnaire/{q_id}")
        time.sleep(0.5)

    print("\n✓ All trials seeded.")


if __name__ == "__main__":
    env = os.environ.get("DEPLOYMENT_ENV", "").lower()
    if env not in ("dev", "local", "sandbox", ""):
        confirm = input(f"WARNING: DEPLOYMENT_ENV='{env}' — this writes to a non-dev environment.\nType 'yes' to continue: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            sys.exit(1)
    seed()
