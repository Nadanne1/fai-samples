"""
Run Amazon Connect Health patient insights for all clinicaltrials360 HealthLake patients.

For each patient:
  1. Start a PRE_VISIT insights job (connecthealth boto3 service)
  2. Poll until SUCCEEDED (concurrent — up to BATCH_SIZE at once)
  3. Download output JSON from S3
  4. Mark any existing ai-clinical-trial-insights DocRef as entered-in-error
  5. Write a new DocumentReference to HealthLake with the AI output
  6. Print progress

Usage:
    cd clinicaltrials
    python3 infra/run_insights_all.py

Takes ~30-40 minutes for all 48 patients (8 parallel batches).
"""

import base64
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

# ── Config ─────────────────────────────────────────────────────────────────
REGION = "us-east-1"
DATASTORE_ID = os.getenv("HEALTHLAKE_DATASTORE_ID", "385610836c26d712d212b430cfdccd18")
FHIR_BASE = f"https://healthlake.{REGION}.amazonaws.com/datastore/{DATASTORE_ID}/r4"
FHIR_ENDPOINT = FHIR_BASE + "/"

S3_OUTPUT_BUCKET = os.getenv("S3_INSIGHTS_BUCKET", "clinicaltrials360-insights-output")
S3_OUTPUT_PATH = f"s3://{S3_OUTPUT_BUCKET}/"

DOMAIN_ID = os.getenv("DOMAIN_ID", "dom-fcigd64gzeneavgs0rzht")
CLINICIAN_ID = "clinical-trial-coordinator-001"
ENCOUNTER_REASON = "Clinical trial eligibility and enrollment assessment"

DOC_TYPE_CODE = "ai-clinical-trial-insights"
DOC_TYPE_SYSTEM = "http://clinicaltrials360.demo/doc-types"

BATCH_SIZE = 8
POLL_INTERVAL = 15
MAX_WAIT = 900

boto_session = boto3.Session()


# ── HealthLake signed requests ──────────────────────────────────────────────

def _signed_request(method: str, url: str, body: dict | None = None) -> requests.Response:
    creds = boto_session.get_credentials().get_frozen_credentials()
    body_bytes = json.dumps(body).encode() if body else b""
    headers = {"Content-Type": "application/fhir+json"} if body else {}
    req = AWSRequest(method=method, url=url, data=body_bytes, headers=headers)
    SigV4Auth(creds, "healthlake", REGION).add_auth(req)
    prepped = req.prepare()
    return requests.request(method, prepped.url, headers=dict(prepped.headers), data=body_bytes, timeout=30)


def fhir_get(path: str) -> dict:
    r = _signed_request("GET", f"{FHIR_BASE}/{path}")
    r.raise_for_status()
    return r.json()


def fhir_put(resource_type: str, resource_id: str, body: dict) -> dict:
    r = _signed_request("PUT", f"{FHIR_BASE}/{resource_type}/{resource_id}", body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"PUT {resource_type}/{resource_id} → {r.status_code}: {r.text[:300]}")
    return r.json()


# ── Fetch all patients ──────────────────────────────────────────────────────

def get_all_patients() -> list[dict]:
    """Fetch all patients in a single page (HealthLake pagination tokens are session-bound
    and cannot be re-signed, so we request enough to fit all patients in one response)."""
    patients = []
    seen_names: set[str] = set()
    r = _signed_request("GET", f"{FHIR_BASE}/Patient?_count=100")
    r.raise_for_status()
    bundle = r.json()
    for entry in bundle.get("entry", []):
        res = entry.get("resource", {})
        if res.get("resourceType") != "Patient":
            continue
        name = patient_display_name(res)
        dob = res.get("birthDate", "")
        dedup_key = f"{name}|{dob}"
        if dedup_key in seen_names:
            continue
        seen_names.add(dedup_key)
        patients.append(res)
    return patients


def patient_display_name(p: dict) -> str:
    for name in p.get("name", []):
        given = " ".join(name.get("given", []))
        family = name.get("family", "")
        if given or family:
            return f"{given} {family}".strip()
    return p.get("id", "Unknown")


# ── Insights job ────────────────────────────────────────────────────────────

def run_single_patient(patient: dict) -> dict:
    """Run end-to-end for one patient. Returns result dict."""
    pid = patient["id"]
    name = patient_display_name(patient)
    client = boto_session.client("connecthealth", region_name=REGION)
    s3 = boto_session.client("s3", region_name=REGION)

    result = {"patient_id": pid, "name": name, "status": "unknown", "error": None}

    # 1. Start job
    try:
        resp = client.start_patient_insights_job(
            domainId=DOMAIN_ID,
            patientContext={"patientId": pid},
            insightsContext={"insightsType": "PRE_VISIT"},
            encounterContext={"encounterReason": ENCOUNTER_REASON},
            userContext={"role": "CLINICIAN", "userId": CLINICIAN_ID},
            inputDataConfig={"fhirServer": {"fhirEndpoint": FHIR_ENDPOINT}},
            outputDataConfig={"s3OutputPath": S3_OUTPUT_PATH},
        )
        job_id = resp["jobId"]
    except Exception as e:
        result["status"] = "FAILED"
        result["error"] = f"start_job: {e}"
        return result

    # 2. Poll
    start = time.time()
    status = "SUBMITTED"
    g = {}
    while status in ("SUBMITTED", "IN_PROGRESS"):
        if time.time() - start > MAX_WAIT:
            result["status"] = "TIMEOUT"
            result["error"] = f"job {job_id} exceeded {MAX_WAIT}s"
            return result
        time.sleep(POLL_INTERVAL)
        try:
            g = client.get_patient_insights_job(jobId=job_id, domainId=DOMAIN_ID)
            status = g["jobStatus"]
        except Exception as e:
            result["status"] = "FAILED"
            result["error"] = f"poll: {e}"
            return result

    if status != "SUCCEEDED":
        result["status"] = status
        result["error"] = g.get("statusDetails", "no details")
        return result

    # 3. Download output from S3
    output_uri = g["insightsOutput"]["uri"]
    bucket = output_uri[5:].split("/", 1)[0]
    key = output_uri[5:].split("/", 1)[1]
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        insights = json.loads(obj["Body"].read().decode("utf-8"))
    except Exception as e:
        result["status"] = "FAILED"
        result["error"] = f"s3_download: {e}"
        return result

    # 4. Mark existing DocRefs as entered-in-error
    try:
        search = fhir_get(f"DocumentReference?patient={pid}&_count=50")
        for entry in search.get("entry", []):
            doc = entry.get("resource", {})
            if doc.get("status") == "entered-in-error":
                continue
            codings = doc.get("type", {}).get("coding", [])
            if any(c.get("code") == DOC_TYPE_CODE for c in codings):
                doc["status"] = "entered-in-error"
                fhir_put("DocumentReference", doc["id"], doc)
    except Exception:
        pass  # no existing DocRef is fine

    # 5. Write new DocumentReference to clinicaltrials360 HealthLake
    insights_b64 = base64.b64encode(json.dumps(insights).encode()).decode()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    docref_id = f"ct-insights-{pid[:30]}-{uuid.uuid4().hex[:8]}"
    docref = {
        "resourceType": "DocumentReference",
        "id": docref_id,
        "status": "current",
        "type": {
            "coding": [{"system": DOC_TYPE_SYSTEM, "code": DOC_TYPE_CODE}]
        },
        "subject": {"reference": f"Patient/{pid}"},
        "date": now,
        "content": [{
            "attachment": {
                "contentType": "application/json",
                "data": insights_b64,
                "title": "AI Clinical Trial Insights",
                "creation": now,
            }
        }],
        "description": f"AI-generated clinical trial assessment — {name}",
    }
    try:
        fhir_put("DocumentReference", docref_id, docref)
    except Exception as e:
        result["status"] = "FAILED"
        result["error"] = f"docref_write: {e}"
        return result

    result["status"] = "DONE"
    return result


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching all patients from clinicaltrials360 HealthLake...")
    print(f"  Datastore: {DATASTORE_ID}")
    print(f"  Domain:    {DOMAIN_ID}")
    print(f"  S3 bucket: {S3_OUTPUT_BUCKET}")
    print()

    patients = get_all_patients()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Found {len(patients)} patients (deduped)")

    results = []
    total = len(patients)
    done = 0

    with ThreadPoolExecutor(max_workers=BATCH_SIZE) as executor:
        futures = {executor.submit(run_single_patient, p): p for p in patients}
        for future in as_completed(futures):
            r = future.result()
            done += 1
            ts = datetime.now().strftime("%H:%M:%S")
            if r["status"] == "DONE":
                print(f"[{ts}] ✓ [{done}/{total}] {r['name']}")
            else:
                print(f"[{ts}] ✗ [{done}/{total}] {r['name']} — {r['status']}: {r['error']}")
            results.append(r)

    succeeded = [r for r in results if r["status"] == "DONE"]
    failed = [r for r in results if r["status"] != "DONE"]

    print(f"\n{'='*60}")
    print(f"COMPLETE — {len(succeeded)}/{total} succeeded")
    if failed:
        print(f"\nFailed ({len(failed)}):")
        for r in failed:
            print(f"  {r['name']}: {r['status']} — {r['error']}")
    print(f"{'='*60}")

    log_path = "infra/run_insights_results.json"
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to {log_path}")


if __name__ == "__main__":
    main()
