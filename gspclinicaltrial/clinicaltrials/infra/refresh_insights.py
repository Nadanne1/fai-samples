"""Refresh AI patient insights in HealthLake.

For each target patient this script:
  1. Runs an Amazon Connect Health PRE_VISIT insights job
  2. Waits for the job to complete (polls every POLL_INTERVAL seconds)
  3. Downloads the output JSON from S3
  4. Marks any existing ai-clinical-trial-insights DocumentReference as
     entered-in-error (so old data doesn't surface in the UI)
  5. Writes a new DocumentReference with the fresh insights JSON (base64)

The resulting DocumentReference is what GET /api/patient/{id}/insights reads.

Usage:
    cd clinicaltrials
    set -a; source .env; set +a

    # Refresh all patients (default)
    python3 infra/refresh_insights.py

    # Refresh one specific patient
    python3 infra/refresh_insights.py --patient-id 030f01b8-1e8b-499a-b

    # Refresh all patients concurrently (default batch size is 8)
    python3 infra/refresh_insights.py --batch-size 12

    # Dry-run: list patients without running jobs
    python3 infra/refresh_insights.py --dry-run

Output:
    infra/refresh_insights_results.json  — per-patient status log

Architecture note:
    Insights are stored as FHIR DocumentReferences in HealthLake.
    The API route GET /api/patient/{id}/insights reads the current-status
    DocumentReference with type code "ai-clinical-trial-insights".
    Re-running this script replaces that record so the next API call
    automatically returns the freshened data — no cache flush needed.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

sys.path.insert(0, ".")
from config.settings import HEALTHLAKE_DATASTORE_ID

# ── Configuration ───────────────────────────────────────────────────────────
REGION = "us-east-1"
FHIR_BASE = f"https://healthlake.{REGION}.amazonaws.com/datastore/{HEALTHLAKE_DATASTORE_ID}/r4"
FHIR_ENDPOINT = FHIR_BASE + "/"

S3_OUTPUT_BUCKET = os.getenv("S3_INSIGHTS_BUCKET", "clinicaltrials360-insights-output")
S3_OUTPUT_PATH = f"s3://{S3_OUTPUT_BUCKET}/"

DOMAIN_ID = os.getenv("DOMAIN_ID", "")
if not DOMAIN_ID:
    print("ERROR: DOMAIN_ID env var is required. Set it in your .env file.")
    sys.exit(1)
CLINICIAN_ID = "clinical-trial-coordinator-001"
ENCOUNTER_REASON = "Clinical trial eligibility and enrollment assessment"

DOC_TYPE_CODE = "ai-clinical-trial-insights"
DOC_TYPE_SYSTEM = "http://clinicaltrials360.demo/doc-types"

POLL_INTERVAL = int(os.getenv("INSIGHTS_POLL_INTERVAL", "15"))
MAX_WAIT = int(os.getenv("INSIGHTS_MAX_WAIT", "900"))
DEFAULT_BATCH_SIZE = 8

_boto = boto3.Session(region_name=REGION)
_creds = _boto.get_credentials().get_frozen_credentials()


# ── HealthLake helpers ───────────────────────────────────────────────────────

def _sign(method: str, url: str, body: bytes = b"") -> dict:
    headers: dict = {"Host": f"healthlake.{REGION}.amazonaws.com"}
    if body:
        headers["Content-Type"] = "application/fhir+json"
    req = AWSRequest(method=method, url=url, data=body or None, headers=headers)
    SigV4Auth(_creds, "healthlake", REGION).add_auth(req)
    return dict(req.headers)


def _fhir_get(path: str) -> dict:
    url = f"{FHIR_BASE}/{path}"
    r = requests.get(url, headers=_sign("GET", url), timeout=30)
    r.raise_for_status()
    return r.json()


def _fhir_put(resource_type: str, resource_id: str, body: dict) -> dict:
    url = f"{FHIR_BASE}/{resource_type}/{resource_id}"
    data = json.dumps(body).encode()
    r = requests.put(url, data=data, headers=_sign("PUT", url, data), timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"PUT {resource_type}/{resource_id} → {r.status_code}: {r.text[:300]}")
    return r.json()


def _get_all_patients() -> list[dict]:
    """Fetch all patients (single page, deduped by name+DOB)."""
    patients: list[dict] = []
    seen: set[str] = set()
    r = requests.get(f"{FHIR_BASE}/Patient?_count=100", headers=_sign("GET", f"{FHIR_BASE}/Patient?_count=100"), timeout=30)
    r.raise_for_status()
    for entry in r.json().get("entry", []):
        p = entry.get("resource", {})
        if p.get("resourceType") != "Patient":
            continue
        key = f"{_patient_name(p)}|{p.get('birthDate', '')}"
        if key not in seen:
            seen.add(key)
            patients.append(p)
    return patients


def _patient_name(p: dict) -> str:
    for n in p.get("name", []):
        given = " ".join(n.get("given", []))
        family = n.get("family", "")
        if given or family:
            return f"{given} {family}".strip()
    return p.get("id", "Unknown")


# ── Insights job for one patient ────────────────────────────────────────────

def _refresh_one(patient: dict) -> dict:
    pid = patient["id"]
    name = _patient_name(patient)
    client = _boto.client("connecthealth", region_name=REGION)
    s3 = _boto.client("s3", region_name=REGION)
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
        result.update(status="FAILED", error=f"start_job: {e}")
        return result

    # 2. Poll until complete
    started = time.time()
    status = "SUBMITTED"
    job_resp: dict = {}
    while status in ("SUBMITTED", "IN_PROGRESS"):
        if time.time() - started > MAX_WAIT:
            result.update(status="TIMEOUT", error=f"job {job_id} exceeded {MAX_WAIT}s")
            return result
        time.sleep(POLL_INTERVAL)
        try:
            job_resp = client.get_patient_insights_job(jobId=job_id, domainId=DOMAIN_ID)
            status = job_resp["jobStatus"]
        except Exception as e:
            result.update(status="FAILED", error=f"poll: {e}")
            return result

    if status != "SUCCEEDED":
        result.update(status=status, error=job_resp.get("statusDetails", "no details"))
        return result

    # 3. Download output from S3
    output_uri = job_resp["insightsOutput"]["uri"]
    bucket = output_uri[5:].split("/", 1)[0]
    key = output_uri[5:].split("/", 1)[1]
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        insights = json.loads(obj["Body"].read().decode())
    except Exception as e:
        result.update(status="FAILED", error=f"s3_download: {e}")
        return result

    # 4. Mark existing ai-clinical-trial-insights DocRefs as entered-in-error
    try:
        search = _fhir_get(f"DocumentReference?patient={pid}&_count=50")
        for entry in search.get("entry", []):
            doc = entry.get("resource", {})
            if doc.get("status") == "entered-in-error":
                continue
            codings = doc.get("type", {}).get("coding", [])
            if any(c.get("code") == DOC_TYPE_CODE for c in codings):
                doc["status"] = "entered-in-error"
                _fhir_put("DocumentReference", doc["id"], doc)
    except Exception:
        pass

    # 5. Write fresh DocumentReference
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc_id = f"ct-insights-{pid[:30]}-{uuid.uuid4().hex[:8]}"
    docref = {
        "resourceType": "DocumentReference",
        "id": doc_id,
        "status": "current",
        "type": {"coding": [{"system": DOC_TYPE_SYSTEM, "code": DOC_TYPE_CODE}]},
        "subject": {"reference": f"Patient/{pid}"},
        "date": now,
        "content": [{
            "attachment": {
                "contentType": "application/json",
                "data": base64.b64encode(json.dumps(insights).encode()).decode(),
                "title": "AI Clinical Trial Insights",
                "creation": now,
            }
        }],
        "description": f"AI-generated clinical trial assessment — {name}",
    }
    try:
        _fhir_put("DocumentReference", doc_id, docref)
    except Exception as e:
        result.update(status="FAILED", error=f"docref_write: {e}")
        return result

    result["status"] = "DONE"
    return result


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    batch_size = DEFAULT_BATCH_SIZE

    patient_id_filter: str | None = None
    for i, arg in enumerate(args):
        if arg == "--patient-id" and i + 1 < len(args):
            patient_id_filter = args[i + 1]
        if arg == "--batch-size" and i + 1 < len(args):
            batch_size = int(args[i + 1])

    if not HEALTHLAKE_DATASTORE_ID:
        print("ERROR: HEALTHLAKE_DATASTORE_ID is not set. Source your .env first.")
        sys.exit(1)

    print(f"Fetching patients from datastore {HEALTHLAKE_DATASTORE_ID[:16]}…")
    patients = _get_all_patients()

    if patient_id_filter:
        patients = [p for p in patients if p["id"].startswith(patient_id_filter)]
        if not patients:
            print(f"ERROR: no patient found with id prefix '{patient_id_filter}'")
            sys.exit(1)

    print(f"  Target: {len(patients)} patient(s)")
    print(f"  Batch size: {batch_size}")
    print(f"  Domain: {DOMAIN_ID}")
    print(f"  S3 bucket: {S3_OUTPUT_BUCKET}")
    print()

    if dry_run:
        print("[dry-run] Patients that would be refreshed:")
        for p in patients:
            print(f"  {p['id'][:32]}  {_patient_name(p)}")
        return

    results: list[dict] = []
    total = len(patients)
    done = 0

    with ThreadPoolExecutor(max_workers=batch_size) as executor:
        futures = {executor.submit(_refresh_one, p): p for p in patients}
        for future in as_completed(futures):
            r = future.result()
            done += 1
            ts = datetime.now().strftime("%H:%M:%S")
            symbol = "✓" if r["status"] == "DONE" else "✗"
            suffix = f" — {r['status']}: {r['error']}" if r["status"] != "DONE" else ""
            print(f"[{ts}] {symbol} [{done}/{total}] {r['name']}{suffix}")
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

    log_path = Path("infra/refresh_insights_results.json")
    with log_path.open("w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults logged → {log_path}")


if __name__ == "__main__":
    main()
