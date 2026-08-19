"""Load trial protocol data, questionnaires, and DynamoDB configs for 20+ patients.

Creates:
  - 2 trial protocol configs in DynamoDB (NCT00000001: Type 2 Diabetes, NCT00000002: Hypertension)
  - 2 screening FHIR Questionnaires in HealthLake
  - 2 monitoring FHIR Questionnaires in HealthLake
  - Enrolls 20 patients from existing HealthLake data into screening queues
"""

import json
import sys
import uuid
from datetime import datetime, timezone

import boto3

sys.path.insert(0, ".")
from config.settings import (
    AWS_REGION,
    HEALTHLAKE_DATASTORE_ID,
    PROTOCOL_CONFIG_TABLE,
    SCREENING_INTAKE_QUEUE,
    REQUIRED_TAGS,
)

hl = boto3.client("healthlake", region_name=AWS_REGION)
ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
sqs = boto3.client("sqs", region_name=AWS_REGION)

DATASTORE_ID = HEALTHLAKE_DATASTORE_ID
DATASTORE_ENDPOINT = f"https://healthlake.{AWS_REGION}.amazonaws.com/datastore/{DATASTORE_ID}/r4"


def store_fhir_resource(resource: dict) -> dict:
    """Store a FHIR resource in HealthLake via the FHIR REST API."""
    import requests
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    resource_type = resource["resourceType"]
    url = f"{DATASTORE_ENDPOINT}/{resource_type}"
    body = json.dumps(resource)

    session = boto3.Session(region_name=AWS_REGION)
    creds = session.get_credentials().get_frozen_credentials()

    req = AWSRequest(
        method="POST",
        url=url,
        data=body,
        headers={
            "Content-Type": "application/fhir+json",
            "Host": f"healthlake.{AWS_REGION}.amazonaws.com",
        },
    )
    SigV4Auth(creds, "healthlake", AWS_REGION).add_auth(req)

    resp = requests.post(url, data=body, headers=dict(req.headers))
    if resp.status_code not in (200, 201):
        print(f"  ERROR creating {resource_type}: {resp.status_code} {resp.text[:200]}")
        return {"id": "error", "resourceType": resource_type}

    result = resp.json()
    resource_id = result.get("id", "unknown")
    print(f"  Created {resource_type}/{resource_id}")
    return {"id": resource_id, "resourceType": resource_type}


# ---------------------------------------------------------------------------
# Trial 1: Type 2 Diabetes Study (NCT00000001)
# ---------------------------------------------------------------------------

DIABETES_SCREENING_QUESTIONNAIRE = {
    "resourceType": "Questionnaire",
    "status": "active",
    "title": "NCT00000001 Type 2 Diabetes Eligibility Screening",
    "identifier": [
        {"system": "https://clinicaltrials.gov", "value": "NCT00000001"}
    ],
    "version": "1.0",
    "item": [
        {
            "linkId": "IE-001",
            "text": "Are you between 18 and 75 years of age?",
            "type": "boolean",
            "required": True,
            "code": [{"system": "http://snomed.info/sct", "code": "424144002", "display": "Current chronological age"}],
        },
        {
            "linkId": "IE-002",
            "text": "Have you been diagnosed with Type 2 Diabetes?",
            "type": "boolean",
            "required": True,
            "code": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "E11", "display": "Type 2 diabetes mellitus"}],
            "enableWhen": [{"question": "IE-001", "operator": "=", "answerBoolean": True}],
        },
        {
            "linkId": "IE-003",
            "text": "What is your most recent HbA1c level?",
            "type": "decimal",
            "required": True,
            "code": [{"system": "http://loinc.org", "code": "4548-4", "display": "Hemoglobin A1c"}],
            "enableWhen": [{"question": "IE-002", "operator": "=", "answerBoolean": True}],
        },
        {
            "linkId": "IE-004",
            "text": "Are you currently taking metformin?",
            "type": "boolean",
            "required": True,
            "code": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "6809", "display": "Metformin"}],
            "enableWhen": [{"question": "IE-002", "operator": "=", "answerBoolean": True}],
        },
        {
            "linkId": "IE-005",
            "text": "Do you have a history of cardiovascular disease?",
            "type": "boolean",
            "required": True,
            "code": [{"system": "http://snomed.info/sct", "code": "49601007", "display": "Cardiovascular disease"}],
        },
        {
            "linkId": "IE-005-CV",
            "text": "Cardiovascular Deep-Dive: Have you had a myocardial infarction in the past 6 months?",
            "type": "boolean",
            "required": True,
            "enableWhen": [{"question": "IE-005", "operator": "=", "answerBoolean": True}],
        },
        {
            "linkId": "IE-006",
            "text": "Do you have severe renal impairment (eGFR < 30)?",
            "type": "boolean",
            "required": True,
            "code": [{"system": "http://loinc.org", "code": "33914-3", "display": "eGFR"}],
        },
        {
            "linkId": "IE-007",
            "text": "Are you currently pregnant or planning to become pregnant?",
            "type": "boolean",
            "required": True,
        },
        {
            "linkId": "IE-008",
            "text": "Do you have any known allergies to study medications (empagliflozin, metformin)?",
            "type": "boolean",
            "required": True,
        },
    ],
}

DIABETES_MONITORING_QUESTIONNAIRE = {
    "resourceType": "Questionnaire",
    "status": "active",
    "title": "NCT00000001 Type 2 Diabetes Monitoring Visit",
    "identifier": [
        {"system": "https://clinicaltrials.gov", "value": "NCT00000001-monitoring"}
    ],
    "version": "1.0",
    "item": [
        {"linkId": "MON-001", "text": "Have you experienced any new symptoms since your last visit?", "type": "text", "required": True},
        {"linkId": "MON-002", "text": "Have you started any new medications?", "type": "text", "required": True},
        {"linkId": "MON-003", "text": "Have you experienced any side effects or adverse events?", "type": "boolean", "required": True},
        {"linkId": "MON-004", "text": "If yes, describe the adverse event", "type": "text", "required": True, "enableWhen": [{"question": "MON-003", "operator": "=", "answerBoolean": True}]},
        {"linkId": "MON-005", "text": "What is the severity of the adverse event?", "type": "choice", "required": True, "enableWhen": [{"question": "MON-003", "operator": "=", "answerBoolean": True}],
         "answerOption": [{"valueCoding": {"code": "mild", "display": "Mild"}}, {"valueCoding": {"code": "moderate", "display": "Moderate"}}, {"valueCoding": {"code": "severe", "display": "Severe"}}]},
        {"linkId": "MON-006", "text": "Current blood glucose reading (mg/dL)", "type": "decimal", "required": False},
    ],
}

# ---------------------------------------------------------------------------
# Trial 2: Hypertension Study (NCT00000002)
# ---------------------------------------------------------------------------

HYPERTENSION_SCREENING_QUESTIONNAIRE = {
    "resourceType": "Questionnaire",
    "status": "active",
    "title": "NCT00000002 Hypertension Eligibility Screening",
    "identifier": [
        {"system": "https://clinicaltrials.gov", "value": "NCT00000002"}
    ],
    "version": "1.0",
    "item": [
        {"linkId": "HT-001", "text": "Are you between 21 and 80 years of age?", "type": "boolean", "required": True},
        {"linkId": "HT-002", "text": "Have you been diagnosed with essential hypertension?", "type": "boolean", "required": True,
         "code": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "I10", "display": "Essential hypertension"}]},
        {"linkId": "HT-003", "text": "What is your most recent systolic blood pressure (mmHg)?", "type": "integer", "required": True,
         "code": [{"system": "http://loinc.org", "code": "8480-6", "display": "Systolic blood pressure"}]},
        {"linkId": "HT-004", "text": "Are you currently on antihypertensive medication?", "type": "boolean", "required": True},
        {"linkId": "HT-005", "text": "Do you have a history of stroke or TIA?", "type": "boolean", "required": True,
         "code": [{"system": "http://snomed.info/sct", "code": "230690007", "display": "Stroke"}]},
        {"linkId": "HT-006", "text": "Do you have chronic kidney disease stage 4 or 5?", "type": "boolean", "required": True},
        {"linkId": "HT-007", "text": "Are you currently pregnant or breastfeeding?", "type": "boolean", "required": True},
    ],
}

HYPERTENSION_MONITORING_QUESTIONNAIRE = {
    "resourceType": "Questionnaire",
    "status": "active",
    "title": "NCT00000002 Hypertension Monitoring Visit",
    "identifier": [
        {"system": "https://clinicaltrials.gov", "value": "NCT00000002-monitoring"}
    ],
    "version": "1.0",
    "item": [
        {"linkId": "HM-001", "text": "Have you experienced any new symptoms since your last visit?", "type": "text", "required": True},
        {"linkId": "HM-002", "text": "Current blood pressure reading (systolic/diastolic)", "type": "text", "required": True},
        {"linkId": "HM-003", "text": "Have you started any new medications?", "type": "text", "required": True},
        {"linkId": "HM-004", "text": "Have you experienced any side effects or adverse events?", "type": "boolean", "required": True},
        {"linkId": "HM-005", "text": "If yes, describe the adverse event", "type": "text", "required": True, "enableWhen": [{"question": "HM-004", "operator": "=", "answerBoolean": True}]},
    ],
}


def create_questionnaires():
    """Store all trial questionnaires in HealthLake."""
    print("\n=== Creating FHIR Questionnaires ===")
    q1 = store_fhir_resource(DIABETES_SCREENING_QUESTIONNAIRE)
    q2 = store_fhir_resource(DIABETES_MONITORING_QUESTIONNAIRE)
    q3 = store_fhir_resource(HYPERTENSION_SCREENING_QUESTIONNAIRE)
    q4 = store_fhir_resource(HYPERTENSION_MONITORING_QUESTIONNAIRE)
    return q1, q2, q3, q4



def create_protocol_configs(q1_id, q2_id, q3_id, q4_id):
    """Store trial protocol configs in DynamoDB."""
    print("\n=== Creating Trial Protocol Configs ===")
    table = ddb.Table(PROTOCOL_CONFIG_TABLE)
    now = datetime.now(timezone.utc).isoformat()

    # Trial 1: Type 2 Diabetes
    table.put_item(Item={
        "trial_id": "NCT00000001",
        "version": "1.0",
        "nct_metadata": {
            "title": "A Phase III Study of Empagliflozin in Patients with Type 2 Diabetes",
            "phase": "Phase 3",
            "status": "Recruiting",
            "conditions": ["Type 2 Diabetes Mellitus"],
            "interventions": ["Empagliflozin 10mg", "Empagliflozin 25mg", "Placebo"],
        },
        "questionnaire_id": f"Questionnaire/{q1_id}",
        "monitoring_questionnaire_id": f"Questionnaire/{q2_id}",
        "eligibility_rules": [
            {"id": "IE-001", "type": "inclusion", "description": "Age 18-75", "data_type": "boolean", "fhir_path": "Patient.birthDate"},
            {"id": "IE-002", "type": "inclusion", "description": "Confirmed T2DM diagnosis", "data_type": "boolean", "fhir_path": "Condition.code"},
            {"id": "IE-003", "type": "inclusion", "description": "HbA1c 7.0-10.5%", "data_type": "decimal", "fhir_path": "Observation.value"},
            {"id": "IE-004", "type": "inclusion", "description": "On stable metformin", "data_type": "boolean", "fhir_path": "MedicationRequest.medication"},
            {"id": "IE-005", "type": "exclusion", "description": "Recent MI (6 months)", "data_type": "boolean", "fhir_path": "Condition.code"},
            {"id": "IE-006", "type": "exclusion", "description": "Severe renal impairment eGFR<30", "data_type": "boolean", "fhir_path": "Observation.value"},
            {"id": "IE-007", "type": "exclusion", "description": "Pregnant or planning pregnancy", "data_type": "boolean"},
            {"id": "IE-008", "type": "exclusion", "description": "Allergy to study meds", "data_type": "boolean", "fhir_path": "AllergyIntolerance.code"},
        ],
        "cdash_mapping": {
            "IE-001": {"domain": "IE", "variable": "IETEST", "label": "Incl/Excl Criterion"},
            "IE-002": {"domain": "IE", "variable": "IEORRES", "label": "I/E Result"},
            "IE-003": {"domain": "LB", "variable": "LBORRES", "label": "Lab Result"},
        },
        "sdtm_mapping": {
            "IE": {"domain": "IE", "variables": ["IETEST", "IEORRES", "IEDTC"]},
            "DM": {"domain": "DM", "variables": ["SUBJID", "RFSTDTC", "AGE", "SEX", "RACE"]},
            "MH": {"domain": "MH", "variables": ["MHTERM", "MHSTDTC", "MHENDTC"]},
        },
        "terminology_versions": {"meddra": "26.0", "snomed": "2024-03", "icd10": "2024", "loinc": "2.77", "rxnorm": "2024-03"},
        "blinding_config": {"is_blinded": True, "unblinding_authority": ["PI", "Medical Monitor"]},
        "safety_signal_patterns": [
            {"pattern": "hypoglycemia", "urgency": "high"},
            {"pattern": "ketoacidosis", "urgency": "urgent"},
            {"pattern": "urinary tract infection", "urgency": "standard"},
        ],
        "retention_years": 15,
        "status": "active",
        "created_at": now,
        "updated_at": now,
    })
    print("  Created NCT00000001 (Type 2 Diabetes)")

    # Trial 2: Hypertension
    table.put_item(Item={
        "trial_id": "NCT00000002",
        "version": "1.0",
        "nct_metadata": {
            "title": "A Phase II Study of Novel ARB in Essential Hypertension",
            "phase": "Phase 2",
            "status": "Recruiting",
            "conditions": ["Essential Hypertension"],
            "interventions": ["Novel ARB 40mg", "Novel ARB 80mg", "Losartan 50mg"],
        },
        "questionnaire_id": f"Questionnaire/{q3_id}",
        "monitoring_questionnaire_id": f"Questionnaire/{q4_id}",
        "eligibility_rules": [
            {"id": "HT-001", "type": "inclusion", "description": "Age 21-80", "data_type": "boolean"},
            {"id": "HT-002", "type": "inclusion", "description": "Essential hypertension diagnosis", "data_type": "boolean"},
            {"id": "HT-003", "type": "inclusion", "description": "SBP 140-180 mmHg", "data_type": "integer"},
            {"id": "HT-004", "type": "inclusion", "description": "On antihypertensive medication", "data_type": "boolean"},
            {"id": "HT-005", "type": "exclusion", "description": "History of stroke/TIA", "data_type": "boolean"},
            {"id": "HT-006", "type": "exclusion", "description": "CKD stage 4-5", "data_type": "boolean"},
            {"id": "HT-007", "type": "exclusion", "description": "Pregnant or breastfeeding", "data_type": "boolean"},
        ],
        "terminology_versions": {"meddra": "26.0", "snomed": "2024-03", "icd10": "2024", "loinc": "2.77", "rxnorm": "2024-03"},
        "blinding_config": {"is_blinded": False, "unblinding_authority": []},
        "safety_signal_patterns": [
            {"pattern": "hypotension", "urgency": "high"},
            {"pattern": "syncope", "urgency": "urgent"},
            {"pattern": "hyperkalemia", "urgency": "high"},
        ],
        "retention_years": 15,
        "status": "active",
        "created_at": now,
        "updated_at": now,
    })
    print("  Created NCT00000002 (Hypertension)")


def get_patient_ids(count=20):
    """Fetch patient IDs — use manifest if available, else query HealthLake."""
    import os
    import requests
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    manifest_path = os.path.join(os.path.dirname(__file__), "patient_manifest.json")
    if os.path.exists(manifest_path):
        print(f"\n=== Loading patient IDs from manifest ===")
        with open(manifest_path) as f:
            manifest = json.load(f)
        # Return flat list of all patients across both trials
        all_p = []
        for trial_patients in manifest.values():
            for p in trial_patients:
                all_p.append({"id": p["id"], "name": p["name"]})
        print(f"  Loaded {len(all_p)} patients from manifest")
        return all_p[:count]

    print(f"\n=== Fetching {count} patient IDs from HealthLake ===")
    session = boto3.Session(region_name=AWS_REGION)
    creds = session.get_credentials().get_frozen_credentials()

    url = f"{DATASTORE_ENDPOINT}/Patient?_count={count}"
    req = AWSRequest(method="GET", url=url, headers={"Host": f"healthlake.{AWS_REGION}.amazonaws.com"})
    SigV4Auth(creds, "healthlake", AWS_REGION).add_auth(req)

    resp = requests.get(url, headers=dict(req.headers))
    bundle = resp.json()
    patients = []
    for entry in bundle.get("entry", []):
        r = entry.get("resource", {})
        pid = r.get("id")
        name = r.get("name", [{}])[0]
        given = " ".join(name.get("given", []))
        family = name.get("family", "")
        patients.append({"id": pid, "name": f"{given} {family}"})
    print(f"  Found {len(patients)} patients")
    return patients


def queue_screening_requests(patients, trial_id="NCT00000001"):
    """Send screening requests to the intake queue for each patient."""
    print(f"\n=== Queuing {len(patients)} screening requests for {trial_id} ===")

    # Get queue URL
    resp = sqs.get_queue_url(QueueName=SCREENING_INTAKE_QUEUE)
    queue_url = resp["QueueUrl"]

    for i, p in enumerate(patients):
        msg = {
            "patient_id": p["id"],
            "trial_id": trial_id,
            "priority": "standard" if i % 3 != 0 else "urgent",
            "site_id": "SITE-001",
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
        sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(msg),
            MessageAttributes={
                "Priority": {"DataType": "String", "StringValue": msg["priority"]},
                "TrialId": {"DataType": "String", "StringValue": trial_id},
                "PatientId": {"DataType": "String", "StringValue": p["id"]},
            },
        )
        print(f"  [{i+1}/{len(patients)}] Queued {p['name']} ({p['id'][:8]}...) priority={msg['priority']}")

    print(f"  All {len(patients)} requests queued to {SCREENING_INTAKE_QUEUE}")


def main():
    import os
    print("=" * 60)
    print("Clinical Trial Data Loading")
    print("=" * 60)

    # 1. Create questionnaires
    q1, q2, q3, q4 = create_questionnaires()

    # 2. Create protocol configs
    create_protocol_configs(q1["id"], q2["id"], q3["id"], q4["id"])

    # 3. Load patients — use manifest (per-trial split) if available
    manifest_path = os.path.join(os.path.dirname(__file__), "patient_manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
        diabetes_patients = manifest.get("NCT00000001", [])
        hypertension_patients = manifest.get("NCT00000002", [])
    else:
        patients = get_patient_ids(48)
        diabetes_patients = patients[:24]
        hypertension_patients = patients[24:48]

    # 4. Queue screening requests per trial
    queue_screening_requests(diabetes_patients, "NCT00000001")
    queue_screening_requests(hypertension_patients, "NCT00000002")

    total = len(diabetes_patients) + len(hypertension_patients)
    print("\n" + "=" * 60)
    print("✅ Data loading complete")
    print("=" * 60)
    print(f"  Questionnaires: 4 (2 screening + 2 monitoring)")
    print(f"  Protocol configs: 2 (NCT00000001, NCT00000002)")
    print(f"  Screening requests queued: {total}")
    print(f"    NCT00000001 (Diabetes):     {len(diabetes_patients)} patients")
    print(f"    NCT00000002 (Hypertension): {len(hypertension_patients)} patients")
    print(f"\n  First 5 NCT00000001 patients:")
    for p in diabetes_patients[:5]:
        print(f"    {p['id']}  ({p['name']})")
    print(f"\n  First 5 NCT00000002 patients:")
    for p in hypertension_patients[:5]:
        print(f"    {p['id']}  ({p['name']})")


if __name__ == "__main__":
    main()
