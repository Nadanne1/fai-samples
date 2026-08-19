"""Import FHIR resources from sampledata/ into a HealthLake datastore.

Reads NDJSON files produced by export_sampledata.py and POSTs each resource
to the target datastore.  Ingestion order matters — Patients are imported
first so that all subsequent resources can reference them by ID.

AI insights (DocumentReference.insights.ndjson) are skipped by default; run
refresh_insights.py to regenerate them against the live data.

Usage:
    cd clinicaltrials
    set -a; source .env; set +a
    python3 infra/import_sampledata.py                  # import everything
    python3 infra/import_sampledata.py --with-insights  # also import cached insights

Options:
    --with-insights   Also import AI-generated insights from
                      sampledata/DocumentReference.insights.ndjson
    --dry-run         Print counts without writing to HealthLake
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterator

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

sys.path.insert(0, ".")
from config.settings import HEALTHLAKE_DATASTORE_ID

REGION = "us-east-1"
BASE = f"https://healthlake.{REGION}.amazonaws.com/datastore/{HEALTHLAKE_DATASTORE_ID}/r4"
SAMPLEDATA = Path("sampledata")

# Import order: Patient first, then clinical supporting resources
IMPORT_ORDER = [
    "Patient.ndjson",
    "Condition.ndjson",
    "Observation.ndjson",
    "MedicationRequest.ndjson",
    "DocumentReference.ndjson",
    "Questionnaire.ndjson",
]
INSIGHTS_FILE = "DocumentReference.insights.ndjson"

_session = boto3.Session(region_name=REGION)


def _sign(method: str, url: str, body: str = "") -> dict:
    creds = _session.get_credentials().get_frozen_credentials()  # refreshed per-call
    headers: dict = {"Host": f"healthlake.{REGION}.amazonaws.com"}
    if body:
        headers["Content-Type"] = "application/fhir+json"
    req = AWSRequest(method=method, url=url, data=body or None, headers=headers)
    SigV4Auth(creds, "healthlake", REGION).add_auth(req)
    return dict(req.headers)


def _iter_ndjson(path: Path) -> Iterator[dict]:
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _post_resource(resource: dict) -> tuple[bool, str]:
    """POST one resource.  Strips id/meta so HealthLake assigns its own IDs."""
    rtype = resource.get("resourceType", "Unknown")
    clean = {k: v for k, v in resource.items() if k not in ("id",)}
    if "meta" in clean:
        meta = {k: v for k, v in clean["meta"].items() if k not in ("versionId", "lastUpdated")}
        if meta:
            clean["meta"] = meta
        else:
            del clean["meta"]

    url = f"{BASE}/{rtype}"
    body = json.dumps(clean, separators=(",", ":"))
    resp = requests.post(url, data=body, headers=_sign("POST", url, body), timeout=30)
    if resp.status_code in (200, 201):
        return True, resp.json().get("id", "")
    return False, f"{resp.status_code}: {resp.text[:200]}"


def _import_file(path: Path, dry_run: bool) -> dict:
    resources = list(_iter_ndjson(path))
    ok = 0
    errors: list[str] = []

    if dry_run:
        print(f"    [dry-run] {path.name}: {len(resources)} resources (not written)")
        return {"total": len(resources), "ok": 0, "errors": []}

    for i, res in enumerate(resources):
        success, detail = _post_resource(res)
        if success:
            ok += 1
        else:
            errors.append(f"  [{i}] {res.get('resourceType','?')} — {detail}")

    rate = f"{ok}/{len(resources)}"
    status = "✓" if not errors else "⚠"
    print(f"    {status} {path.name}: {rate} imported")
    if errors:
        for e in errors[:5]:
            print(f"      {e}")
        if len(errors) > 5:
            print(f"      … and {len(errors) - 5} more")

    return {"total": len(resources), "ok": ok, "errors": errors}


def main() -> None:
    args = sys.argv[1:]
    with_insights = "--with-insights" in args
    dry_run = "--dry-run" in args

    if not HEALTHLAKE_DATASTORE_ID:
        print("ERROR: HEALTHLAKE_DATASTORE_ID is not set. Source your .env first.")
        sys.exit(1)

    manifest_path = SAMPLEDATA / "manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: {manifest_path} not found. Run export_sampledata.py first.")
        sys.exit(1)

    with manifest_path.open() as f:
        manifest = json.load(f)

    mode = "[dry-run] " if dry_run else ""
    print(f"{mode}Importing sampledata/ → datastore {HEALTHLAKE_DATASTORE_ID[:16]}…")
    print(f"  Exported at: {manifest.get('exported_at', 'unknown')}")
    print()

    files = list(IMPORT_ORDER)
    if with_insights:
        files.append(INSIGHTS_FILE)
    else:
        print(f"  (skipping insights — pass --with-insights to include)")

    total_ok = 0
    total_all = 0

    for fname in files:
        path = SAMPLEDATA / fname
        if not path.exists():
            print(f"    – {fname}: not found, skipping")
            continue
        result = _import_file(path, dry_run)
        total_ok += result["ok"]
        total_all += result["total"]

    print()
    if dry_run:
        print(f"[dry-run] Would import {total_all} resources")
    else:
        print(f"✓ Import complete: {total_ok}/{total_all} resources written")
        if with_insights:
            print(
                "\n  Note: insights were imported from cached snapshots.\n"
                "  Run  python3 infra/refresh_insights.py  to regenerate\n"
                "  live insights against the current FHIR data."
            )
        else:
            print(
                "\n  Run  python3 infra/refresh_insights.py  to generate\n"
                "  AI patient insights and store them in HealthLake."
            )


if __name__ == "__main__":
    main()
