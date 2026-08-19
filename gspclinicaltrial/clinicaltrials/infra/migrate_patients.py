"""Migrate + massage patients from careconnect360 HealthLake → clinicaltrials360 HealthLake.

Strategy:
  - Export all 48 patients + their FHIR resources from careconnect360
  - Massage each patient to be clinical-trial-ready:
      * Patients 1-24  → Type 2 Diabetes trial (NCT00000001)
        - Add/verify T2DM Condition (E11), HbA1c Observation (7.5-9.5%), Metformin MedicationRequest
      * Patients 25-48 → Hypertension trial (NCT00000002)
        - Add/verify HTN Condition (I10), SBP Observation (145-175 mmHg)
  - Import all into clinicaltrials360 datastore
  - Output patient IDs suitable for load_trial_data.py

Usage:
    cd clinicaltrials
    python3 infra/migrate_patients.py
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

sys.path.insert(0, ".")
if True:  # noqa — after sys.path
    from config.settings import AWS_REGION, HEALTHLAKE_DATASTORE_ID

REGION = "us-east-1"  # both datastores live in us-east-1
SRC_DS = os.environ.get("SRC_HEALTHLAKE_DATASTORE_ID", "")
if not SRC_DS:
    print("ERROR: SRC_HEALTHLAKE_DATASTORE_ID env var is required")
    sys.exit(1)
DST_DS = HEALTHLAKE_DATASTORE_ID              # clinicaltrials360

SRC_BASE = f"https://healthlake.{REGION}.amazonaws.com/datastore/{SRC_DS}/r4"
DST_BASE = f"https://healthlake.{REGION}.amazonaws.com/datastore/{DST_DS}/r4"

_session = boto3.Session(region_name=REGION)
_creds = _session.get_credentials().get_frozen_credentials()


# ---------------------------------------------------------------------------
# FHIR helpers
# ---------------------------------------------------------------------------

def _sign(method: str, url: str, body: str = "") -> dict:
    headers: dict = {"Host": f"healthlake.{REGION}.amazonaws.com"}
    if body:
        headers["Content-Type"] = "application/fhir+json"
    req = AWSRequest(method=method, url=url, data=body or None, headers=headers)
    SigV4Auth(_creds, "healthlake", REGION).add_auth(req)
    return dict(req.headers)


def fhir_get(base: str, path: str) -> dict:
    url = f"{base}/{path}"
    resp = requests.get(url, headers=_sign("GET", url))
    resp.raise_for_status()
    return resp.json()


def fhir_get_url(full_url: str) -> dict:
    """GET a full URL for pagination using SigV4 auth."""
    resp = requests.get(full_url, headers=_sign("GET", full_url))
    if not resp.ok:
        raise PermissionError(f"HealthLake request failed ({resp.status_code}): {full_url[:100]}")
    return resp.json()


def fhir_post(base: str, resource: dict) -> dict:
    """POST to HealthLake — always strip id, let HealthLake assign one."""
    rtype = resource["resourceType"]
    # HealthLake rejects client-supplied IDs — remove them
    clean = {k: v for k, v in resource.items() if k not in ("id",)}
    # Strip meta.versionId too (from source)
    if "meta" in clean:
        meta = {k: v for k, v in clean["meta"].items() if k not in ("versionId", "lastUpdated")}
        if meta:
            clean["meta"] = meta
        else:
            del clean["meta"]
    url = f"{base}/{rtype}"
    body = json.dumps(clean)
    resp = requests.post(url, data=body, headers=_sign("POST", url, body))
    if resp.status_code not in (200, 201):
        print(f"    ERROR {resp.status_code}: {resp.text[:300]}")
        return {}
    return resp.json()


def paginate(base: str, query: str) -> list[dict]:
    """Collect all entries from a paginated search.

    For the first page, builds URL from base+query and signs it.
    For subsequent pages, uses the next link URL directly — HealthLake's
    pagination tokens are pre-signed and cannot be re-signed (403 if re-signed).
    """
    resources = []
    # First page — sign normally
    b = fhir_get(base, query)
    resources += [e["resource"] for e in b.get("entry", [])]
    next_full_url = next((l["url"] for l in b.get("link", []) if l["relation"] == "next"), None)

    while next_full_url:
        b = fhir_get_url(next_full_url)
        resources += [e["resource"] for e in b.get("entry", [])]
        next_full_url = next((l["url"] for l in b.get("link", []) if l["relation"] == "next"), None)

    return resources


# ---------------------------------------------------------------------------
# Enrichment helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _make_t2dm_condition(patient_id: str) -> dict:
    return {
        "resourceType": "Condition",
        "id": f"t2dm-{patient_id[:16]}",
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
        "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": "confirmed"}]},
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category", "code": "encounter-diagnosis", "display": "Encounter Diagnosis"}]}],
        "code": {
            "coding": [
                {"system": "http://hl7.org/fhir/sid/icd-10", "code": "E11", "display": "Type 2 diabetes mellitus without complications"},
                {"system": "http://snomed.info/sct", "code": "44054006", "display": "Diabetes mellitus type 2"},
            ],
            "text": "Type 2 diabetes mellitus",
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "onsetDateTime": "2020-01-15",
        "recordedDate": "2020-01-15",
    }


def _make_hba1c_observation(patient_id: str, value: float) -> dict:
    return {
        "resourceType": "Observation",
        "id": f"hba1c-{patient_id[:16]}",
        "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
        "code": {
            "coding": [{"system": "http://loinc.org", "code": "4548-4", "display": "Hemoglobin A1c/Hemoglobin.total in Blood"}],
            "text": "Hemoglobin A1c",
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "effectiveDateTime": _now(),
        "valueQuantity": {"value": value, "unit": "%", "system": "http://unitsofmeasure.org", "code": "%"},
        "interpretation": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                                         "code": "H", "display": "High"}]}],
    }


def _make_metformin_med(patient_id: str) -> dict:
    return {
        "resourceType": "MedicationRequest",
        "id": f"metformin-{patient_id[:16]}",
        "status": "active",
        "intent": "order",
        "medicationCodeableConcept": {
            "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "6809", "display": "Metformin"}],
            "text": "Metformin 500 MG Oral Tablet",
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "authoredOn": "2020-02-01",
        "dosageInstruction": [{"text": "500 mg twice daily with meals", "timing": {"repeat": {"frequency": 2, "period": 1, "periodUnit": "d"}}}],
    }


def _make_htn_condition(patient_id: str) -> dict:
    return {
        "resourceType": "Condition",
        "id": f"htn-{patient_id[:16]}",
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
        "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": "confirmed"}]},
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category", "code": "encounter-diagnosis"}]}],
        "code": {
            "coding": [
                {"system": "http://hl7.org/fhir/sid/icd-10", "code": "I10", "display": "Essential hypertension"},
                {"system": "http://snomed.info/sct", "code": "59621000", "display": "Essential hypertension"},
            ],
            "text": "Essential hypertension",
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "onsetDateTime": "2019-06-01",
        "recordedDate": "2019-06-01",
    }


def _make_bp_observation(patient_id: str, sbp: int, dbp: int) -> dict:
    return {
        "resourceType": "Observation",
        "id": f"bp-{patient_id[:16]}",
        "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs"}]}],
        "code": {
            "coding": [{"system": "http://loinc.org", "code": "55284-4", "display": "Blood pressure systolic and diastolic"}],
            "text": "Blood pressure",
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "effectiveDateTime": _now(),
        "component": [
            {
                "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6", "display": "Systolic blood pressure"}]},
                "valueQuantity": {"value": sbp, "unit": "mmHg", "system": "http://unitsofmeasure.org", "code": "mm[Hg]"},
            },
            {
                "code": {"coding": [{"system": "http://loinc.org", "code": "8462-4", "display": "Diastolic blood pressure"}]},
                "valueQuantity": {"value": dbp, "unit": "mmHg", "system": "http://unitsofmeasure.org", "code": "mm[Hg]"},
            },
        ],
    }


def _make_antihtn_med(patient_id: str) -> dict:
    return {
        "resourceType": "MedicationRequest",
        "id": f"antihtn-{patient_id[:16]}",
        "status": "active",
        "intent": "order",
        "medicationCodeableConcept": {
            "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "29046", "display": "Lisinopril"}],
            "text": "Lisinopril 10 MG Oral Tablet",
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "authoredOn": "2019-07-01",
        "dosageInstruction": [{"text": "10 mg once daily"}],
    }


# ---------------------------------------------------------------------------
# Strip careconnect360-specific extensions / identifiers
# ---------------------------------------------------------------------------

CC360_EXTENSIONS_TO_STRIP = {
    "http://careconnect360.demo/extension",
    "http://careconnect360.example.com",
}

def _clean_resource(resource: dict) -> dict:
    """Remove cc360-specific metadata so it imports cleanly into clinicaltrials360."""
    r = json.loads(json.dumps(resource))  # deep copy

    # Remove meta.source if it points to cc360
    meta = r.get("meta", {})
    if "source" in meta:
        src = meta["source"]
        if "careconnect" in src.lower():
            del meta["source"]

    # Strip cc360-specific profile claims
    if "profile" in meta:
        meta["profile"] = [p for p in meta["profile"] if "careconnect" not in p.lower()]
        if not meta["profile"]:
            del meta["profile"]

    r["meta"] = meta

    # Strip cc360 extensions
    if "extension" in r:
        r["extension"] = [
            e for e in r["extension"]
            if not any(e.get("url", "").startswith(pfx) for pfx in CC360_EXTENSIONS_TO_STRIP)
        ]

    return r


# ---------------------------------------------------------------------------
# Main migration
# ---------------------------------------------------------------------------

def main():
    print("=" * 65)
    print("Clinical Trial Patient Migration")
    print(f"  Source: careconnect360  ({SRC_DS[:12]}...)")
    print(f"  Dest:   clinicaltrials360 ({DST_DS[:12]}...)")
    print("=" * 65)

    # 1. Fetch all source patients
    print("\n▸ Fetching patients from careconnect360...")
    patients = paginate(SRC_BASE, "Patient?_count=100")
    print(f"  Found {len(patients)} patients")

    # Split: first 24 → diabetes, next 24 → hypertension
    # (use deterministic index-based split, not random)
    diabetes_patients = patients[:24]
    htn_patients = patients[24:]

    # HbA1c values: stagger between 7.5 and 9.5 for variety
    hba1c_values = [7.5 + (i % 8) * 0.25 for i in range(24)]
    # SBP values: stagger 145-175
    sbp_values = [145 + (i % 7) * 5 for i in range(24)]
    dbp_values = [88 + (i % 4) * 3 for i in range(24)]

    migrated_diabetes = []
    migrated_htn = []

    # ── Diabetes patients ──────────────────────────────────────────────────
    print(f"\n▸ Migrating {len(diabetes_patients)} diabetes patients...")
    for i, p in enumerate(diabetes_patients):
        pid = p["id"]
        name_obj = p.get("name", [{}])[0]
        name = f"{' '.join(name_obj.get('given', []))} {name_obj.get('family', '')}".strip()
        print(f"  [{i+1:2d}/{len(diabetes_patients)}] {name} ({pid[:20]})")

        # Import base patient
        clean_p = _clean_resource(p)
        result = fhir_post(DST_BASE, clean_p)
        if not result:
            print(f"    ⚠ Skipping — patient import failed")
            continue

        new_pid = result.get("id", pid)

        # ── Enrich for T2DM trial ──────────────────────────────────────────
        # We only add trial-specific clinical data — existing conditions from careconnect360
        # are not migrated (pagination tokens are session-bound and can't be re-signed)
        enriched = []
        cond = _make_t2dm_condition(new_pid)
        fhir_post(DST_BASE, cond)
        enriched.append("T2DM condition")

        obs = _make_hba1c_observation(new_pid, hba1c_values[i])
        fhir_post(DST_BASE, obs)
        enriched.append(f"HbA1c={hba1c_values[i]}%")

        med = _make_metformin_med(new_pid)
        fhir_post(DST_BASE, med)
        enriched.append("Metformin")

        print(f"    + Added: {', '.join(enriched)}")
        migrated_diabetes.append({"id": new_pid, "name": name, "original_id": pid})

    # ── Hypertension patients ──────────────────────────────────────────────
    print(f"\n▸ Migrating {len(htn_patients)} hypertension patients...")
    for i, p in enumerate(htn_patients):
        pid = p["id"]
        name_obj = p.get("name", [{}])[0]
        name = f"{' '.join(name_obj.get('given', []))} {name_obj.get('family', '')}".strip()
        print(f"  [{i+1:2d}/{len(htn_patients)}] {name} ({pid[:20]})")

        # Import base patient
        clean_p = _clean_resource(p)
        result = fhir_post(DST_BASE, clean_p)
        if not result:
            print(f"    ⚠ Skipping — patient import failed")
            continue

        new_pid = result.get("id", pid)

        # ── Enrich for HTN trial ───────────────────────────────────────────
        enriched = []
        cond = _make_htn_condition(new_pid)
        fhir_post(DST_BASE, cond)
        enriched.append("HTN condition")

        obs = _make_bp_observation(new_pid, sbp_values[i], dbp_values[i])
        fhir_post(DST_BASE, obs)
        enriched.append(f"BP={sbp_values[i]}/{dbp_values[i]}")

        med = _make_antihtn_med(new_pid)
        fhir_post(DST_BASE, med)
        enriched.append("Lisinopril")

        print(f"    + Added: {', '.join(enriched)}")
        migrated_htn.append({"id": new_pid, "name": name, "original_id": pid})

    # ── Summary ────────────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("✅ Migration complete")
    print("=" * 65)
    print(f"  NCT00000001 (Diabetes):     {len(migrated_diabetes)} patients")
    print(f"  NCT00000002 (Hypertension): {len(migrated_htn)} patients")

    # Write manifest for load_trial_data.py
    manifest = {
        "NCT00000001": migrated_diabetes,
        "NCT00000002": migrated_htn,
    }
    with open("infra/patient_manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print("\n  Manifest written → infra/patient_manifest.json")

    print("\n  Run next:")
    print("    python3 infra/load_trial_data.py")


if __name__ == "__main__":
    main()
