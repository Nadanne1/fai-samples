"""TrialMatch360 — FastAPI application.

Endpoints:
  GET  /                           health check
  GET  /api/patients               patient list from HealthLake
  GET  /api/queue                  screening queue depth
  GET  /api/trials                 configured trials
  GET  /api/trial/{id}             trial protocol detail
  GET  /api/trial/{id}/healthlake-mapping  HealthLake FHIR resource mapping
  GET  /api/patient/{id}           patient FHIR detail
  GET  /api/patient/{id}/insights  AI patient insights
  GET  /api/patient/{id}/trial-history  screening history
  POST /api/chat/start             run AI eligibility screening
  POST /api/chat/respond           stub (non-interactive mode)
  GET  /api/analytics              aggregate screening analytics
  POST /screen                     AgentCore: screen patient
  GET  /escalations                AgentCore: list escalations
  POST /trials                     AgentCore: configure trial
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
import requests as http_requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from urllib.parse import quote

from boto3.dynamodb.conditions import Attr
from config.settings import (
    COGNITO_USER_POOL_ID,
    AUDIT_LOG_TABLE,
    AWS_REGION,
    DEVOPS_TESTS_TABLE,
    ESCALATION_QUEUE,
    HEALTHLAKE_DATASTORE_ID,
    PHI_GUARDRAIL_VERSION,
    PHI_PROTECTION_GUARDRAIL_ID,
    PROTOCOL_CONFIG_TABLE,
    SCREENING_INTAKE_QUEUE,
    SCREENING_RULES_TABLE,
)
from tools.queues import _get_queue_urls

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BEDROCK_MODEL = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
FHIR_BASE = f"https://healthlake.{AWS_REGION}.amazonaws.com/datastore/{HEALTHLAKE_DATASTORE_ID}/r4"
API_KEY_SECRET = os.environ.get("CLINICAL_TRIALS_API_KEY", "")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="TrialMatch360",
    version="2.0.0",
)

_cors_origins = [
    "http://localhost:5173",
    "http://localhost:3000",
]
_extra = os.environ.get("CORS_ALLOWED_ORIGINS", "https://d3vru5lvbq0ov4.cloudfront.net")
_cors_origins.extend([o.strip() for o in _extra.split(",") if o.strip()])

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def health_check() -> dict:
    return {"status": "healthy", "service": "clinical-trial-screening", "version": "2.0.0"}


# ---------------------------------------------------------------------------
# Auth middleware — API key for AgentCore tool calls
# ---------------------------------------------------------------------------

async def _check_api_key(request: Request) -> None:
    """Validate X-Api-Key header. Skips check when API_KEY_SECRET is unset (local/dev)."""
    if not API_KEY_SECRET:
        return  # No key configured — allow all (dev/local mode)
    key = request.headers.get("X-Api-Key") or request.headers.get("x-api-key", "")
    if not hmac.compare_digest(key.encode(), API_KEY_SECRET.encode()):
        raise HTTPException(status_code=401, detail="Invalid API key")


api_router = APIRouter(prefix="/api", dependencies=[Depends(_check_api_key)])


# ---------------------------------------------------------------------------
# Cognito JWT role enforcement — defined here so route decorators can reference
# these dependencies before the Cognito user-management routes appear later.
# ---------------------------------------------------------------------------

def _cognito():
    return boto3.client("cognito-idp", region_name=AWS_REGION)


def _parse_jwt_payload(token: str) -> dict:
    """Decode JWT payload without verifying signature (verification done via get_user)."""
    import base64
    try:
        part = token.split(".")[1]
        padding = 4 - len(part) % 4
        padded = part + "=" * (padding % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}


async def _resolve_role(request: Request) -> list[str]:
    """Validate Bearer token against the configured Cognito pool and return the caller's groups.

    Verification steps:
      1. Decode the token payload and confirm iss matches the configured pool URL — rejects tokens
         issued by other pools even if the username happens to exist in this pool.
      2. Call Cognito get_user with the access token — Cognito validates signature, expiry, and
         audience, so this is the authoritative check without needing a local JWKS library.
      3. Fetch group membership fresh from Cognito so stale JWT claims cannot be replayed.
    """
    auth = request.headers.get("Authorization") or request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    access_token = auth[len("Bearer "):]

    expected_iss = f"https://cognito-idp.{AWS_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"
    payload = _parse_jwt_payload(access_token)
    if payload.get("iss") != expected_iss:
        raise HTTPException(status_code=401, detail="Token issued by unknown pool")

    idp = _cognito()
    try:
        user_resp = idp.get_user(AccessToken=access_token)
        username = user_resp["Username"]
    except idp.exceptions.NotAuthorizedException:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    except Exception:
        logger.exception("Cognito get_user failed")
        raise HTTPException(status_code=401, detail="Token verification failed")
    try:
        groups_resp = idp.admin_list_groups_for_user(UserPoolId=COGNITO_USER_POOL_ID, Username=username)
        return [g["GroupName"] for g in groups_resp.get("Groups", [])]
    except Exception:
        logger.exception("Cognito admin_list_groups_for_user failed")
        raise HTTPException(status_code=403, detail="Could not verify role")


async def _require_admin(request: Request) -> None:
    """Allow only admin group members."""
    groups = await _resolve_role(request)
    if "admin" not in groups:
        raise HTTPException(status_code=403, detail="Admin role required")


async def _require_write(request: Request) -> None:
    """Allow admin and researcher group members (can create/delete trials, run builder)."""
    groups = await _resolve_role(request)
    if not {"admin", "researcher"}.intersection(groups):
        raise HTTPException(status_code=403, detail="Researcher or admin role required")


# ---------------------------------------------------------------------------
# HealthLake helpers
# ---------------------------------------------------------------------------

def _hl_get(path: str) -> dict:
    """SigV4-signed GET against HealthLake FHIR R4."""
    url = f"{FHIR_BASE}/{path}"
    session = boto3.Session(region_name=AWS_REGION)
    creds = session.get_credentials().get_frozen_credentials()
    req = AWSRequest(method="GET", url=url, headers={"Host": f"healthlake.{AWS_REGION}.amazonaws.com"})
    SigV4Auth(creds, "healthlake", AWS_REGION).add_auth(req)
    resp = http_requests.get(url, headers=dict(req.headers))
    if not resp.ok:
        logger.warning("HealthLake GET %s → %s", path, resp.status_code)
        return {}
    return resp.json()


def _hl_post(path: str, resource: dict) -> dict:
    """SigV4-signed POST against HealthLake FHIR R4."""
    url = f"{FHIR_BASE}/{path}"
    session = boto3.Session(region_name=AWS_REGION)
    creds = session.get_credentials().get_frozen_credentials()
    body = json.dumps(resource).encode("utf-8")
    req = AWSRequest(
        method="POST", url=url,
        data=body,
        headers={
            "Host": f"healthlake.{AWS_REGION}.amazonaws.com",
            "Content-Type": "application/fhir+json",
        },
    )
    SigV4Auth(creds, "healthlake", AWS_REGION).add_auth(req)
    resp = http_requests.post(url, data=body, headers=dict(req.headers))
    if not resp.ok:
        logger.warning("HealthLake POST %s → %s: %s", path, resp.status_code, resp.text[:200])
        return {}
    return resp.json()


def _hl_delete(path: str) -> bool:
    """SigV4-signed DELETE against HealthLake FHIR R4."""
    url = f"{FHIR_BASE}/{path}"
    session = boto3.Session(region_name=AWS_REGION)
    creds = session.get_credentials().get_frozen_credentials()
    req = AWSRequest(method="DELETE", url=url, headers={"Host": f"healthlake.{AWS_REGION}.amazonaws.com"})
    SigV4Auth(creds, "healthlake", AWS_REGION).add_auth(req)
    resp = http_requests.delete(url, headers=dict(req.headers))
    if resp.status_code in (200, 204):
        return True
    logger.warning("HealthLake DELETE %s → %s: %s", path, resp.status_code, resp.text[:200])
    return False


def _fetch_patient_fhir(patient_id: str) -> dict:
    """Fetch Patient + 5 clinical resource types from HealthLake."""
    resource_types = ["Condition", "MedicationRequest", "AllergyIntolerance", "Procedure", "Observation"]
    data: dict[str, Any] = {}
    patient = _hl_get(f"Patient/{patient_id}")
    if patient:
        data["Patient"] = [patient]
    for rtype in resource_types:
        bundle = _hl_get(f"{rtype}?patient={quote(patient_id, safe='')}&_count=50")
        data[rtype] = [e["resource"] for e in bundle.get("entry", []) if "resource" in e]
    return data


def _fetch_questionnaire(trial_id: str) -> dict:
    """Load FHIR Questionnaire for a trial from DynamoDB → HealthLake."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = ddb.Table(PROTOCOL_CONFIG_TABLE)
    try:
        result = table.get_item(Key={"trial_id": trial_id, "version": "1.0"})
        item = result.get("Item", {})
        q_ref = item.get("questionnaire_id", "")
        if q_ref:
            q_id = q_ref.split("/")[-1]
            q = _hl_get(f"Questionnaire/{q_id}")
            if q.get("item"):
                return q
    except Exception:
        logger.exception("Failed to load questionnaire for trial %s", trial_id)

    # Fallback: search by identifier
    bundle = _hl_get(f"Questionnaire?identifier=https://clinicaltrials.gov|{trial_id}&_count=1")
    entries = bundle.get("entry", [])
    if entries:
        return entries[0].get("resource", {})
    return {}


# ---------------------------------------------------------------------------
# Bedrock auto-screener — replaces the broken LangGraph interactive loop
# ---------------------------------------------------------------------------

def _summarise_fhir(fhir_data: dict) -> str:
    """Produce a compact clinical summary from FHIR resources for the LLM."""
    lines: list[str] = []

    patient_list = fhir_data.get("Patient", [])
    if patient_list:
        p = patient_list[0]
        name_obj = (p.get("name") or [{}])[0]
        given = " ".join(name_obj.get("given", []))
        family = name_obj.get("family", "")
        lines.append(f"Patient: {given} {family}, DOB {p.get('birthDate','?')}, {p.get('gender','?')}")

    conditions = fhir_data.get("Condition", [])
    if conditions:
        codes = []
        for c in conditions[:15]:
            text = c.get("code", {}).get("text") or (c.get("code", {}).get("coding") or [{}])[0].get("display", "")
            if text:
                codes.append(text)
        lines.append("Conditions: " + "; ".join(codes))

    meds = fhir_data.get("MedicationRequest", [])
    if meds:
        med_names = []
        for m in meds[:15]:
            text = m.get("medicationCodeableConcept", {}).get("text") or \
                   (m.get("medicationCodeableConcept", {}).get("coding") or [{}])[0].get("display", "")
            if text:
                med_names.append(text)
        lines.append("Medications: " + "; ".join(med_names))

    obs = fhir_data.get("Observation", [])
    if obs:
        obs_lines = []
        for o in obs[:10]:
            code = o.get("code", {}).get("text") or (o.get("code", {}).get("coding") or [{}])[0].get("display", "")
            val = o.get("valueQuantity", {})
            if code and val.get("value") is not None:
                obs_lines.append(f"{code}: {val['value']} {val.get('unit','')}")
        if obs_lines:
            lines.append("Labs/Vitals: " + "; ".join(obs_lines))

    allergy = fhir_data.get("AllergyIntolerance", [])
    if allergy:
        a_names = []
        for a in allergy[:5]:
            text = a.get("code", {}).get("text") or \
                   (a.get("code", {}).get("coding") or [{}])[0].get("display", "")
            if text:
                a_names.append(text)
        lines.append("Allergies: " + "; ".join(a_names))

    return "\n".join(lines) if lines else "No clinical data available."


def _run_bedrock_screening(patient_id: str, trial_id: str, fhir_data: dict, questionnaire: dict) -> dict:
    """Call Bedrock to evaluate eligibility criteria autonomously from FHIR data.

    Returns dict with keys: determination, criteria_results, messages, summary.
    """
    items = questionnaire.get("item", [])
    trial_title = questionnaire.get("title", trial_id)
    clinical_summary = _summarise_fhir(fhir_data)

    criteria_text = "\n".join(
        f"{i+1}. [{item.get('linkId','?')}] {item.get('text','?')} (type: {item.get('type','boolean')}, required: {item.get('required', False)})"
        for i, item in enumerate(items)
    )

    system_prompt = (
        "You are a clinical trial eligibility screener. "
        "Evaluate a patient's eligibility for a clinical trial based solely on their FHIR clinical record. "
        "For each criterion, determine pass/fail/indeterminate from the available data. "
        "Be conservative — if data is missing or ambiguous, mark indeterminate. "
        "After evaluating all criteria, give a final determination: eligible, ineligible, or borderline. "
        "eligible = all inclusion criteria pass, no exclusion criteria fail. "
        "ineligible = any inclusion criterion fails OR any exclusion criterion passes. "
        "borderline = mixed or ambiguous results. "
        "Respond ONLY with valid JSON matching this exact schema:\n"
        '{"determination": "eligible|ineligible|borderline", '
        '"summary": "one sentence explanation", '
        '"criteria_results": [{"linkId": "...", "text": "...", "result": "pass|fail|indeterminate", "reason": "..."}]}'
    )

    user_msg = (
        f"Trial: {trial_title}\n\n"
        f"Patient clinical record:\n{clinical_summary}\n\n"
        f"Eligibility criteria to evaluate:\n{criteria_text}\n\n"
        "Evaluate each criterion against the patient record and return JSON."
    )

    bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    converse_kwargs: dict = {
        "modelId": BEDROCK_MODEL,
        "system": [{"text": system_prompt}],
        "messages": [{"role": "user", "content": [{"text": user_msg}]}],
        "inferenceConfig": {"maxTokens": 2048, "temperature": 0.1},
    }
    if PHI_PROTECTION_GUARDRAIL_ID:
        converse_kwargs["guardrailConfig"] = {
            "guardrailIdentifier": PHI_PROTECTION_GUARDRAIL_ID,
            "guardrailVersion": PHI_GUARDRAIL_VERSION,
            "trace": "disabled",
        }
    response = bedrock.converse(**converse_kwargs)

    raw = response["output"]["message"]["content"][0]["text"].strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
        if m:
            raw = m.group(1)
    raw = raw.strip()

    result = json.loads(raw)
    determination = result.get("determination", "borderline")
    criteria_results = result.get("criteria_results", [])
    summary = result.get("summary", "")

    passed = sum(1 for c in criteria_results if c.get("result") == "pass")
    failed = sum(1 for c in criteria_results if c.get("result") == "fail")

    messages = [
        {"role": "system", "content": f"Screening {trial_title} for patient using AI eligibility analysis."},
        {"role": "assistant", "content": f"I've reviewed the patient's clinical record against {len(items)} eligibility criteria.\n\n{summary}\n\nCriteria evaluated: {len(criteria_results)} | Passed: {passed} | Failed: {failed}"},
    ]

    return {
        "determination": determination,
        "criteria_results": criteria_results,
        "messages": messages,
        "summary": summary,
    }


def _write_audit_record(session_id: str, patient_id: str, trial_id: str, determination: str, criteria_results: list) -> None:
    """Write a decision_made record to TrialAuditLog with top-level eligibility_determination."""
    try:
        ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
        ddb.Table(AUDIT_LOG_TABLE).put_item(Item={
            "audit_id": str(uuid.uuid4()),
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "patient_id": patient_id,
            "trial_id": trial_id,
            "event_type": "decision_made",
            "agent_id": "screening-agent",
            "user_identity": "screening-agent",
            "eligibility_determination": determination,
            "criteria_results": criteria_results,
            "event_data": {"determination": determination, "criteria_count": len(criteria_results)},
            "fhir_resource_refs": [],
        })
    except Exception:
        logger.exception("Failed to write audit record for session %s", session_id)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ChatStartRequest(BaseModel):
    patient_id: str
    trial_id: str


class ChatRespondRequest(BaseModel):
    session_id: str
    message: str


class ScreenRequest(BaseModel):
    patient_id: str
    trial_id: str


class TrialConfigRequest(BaseModel):
    nct_number: str | None = None
    protocol_document: str | None = None
    trial_id: str | None = None


class ScreeningRuleBody(BaseModel):
    name: str = Field(..., max_length=200)
    description: str = Field("", max_length=500)
    category: str = Field("general", max_length=50)
    trigger: str = Field(..., max_length=100)
    triggerConfig: dict = Field(default_factory=dict)
    action: str = Field(..., max_length=100)
    actionConfig: dict = Field(default_factory=dict)
    enabled: bool = True
    priority: int = Field(99, ge=1, le=999)


# ---------------------------------------------------------------------------
# Dashboard API — /api/* (called by React frontend)
# ---------------------------------------------------------------------------

@api_router.get("/patients")
async def api_patients(count: int = 50) -> dict:
    """List patients from HealthLake, deduplicated by name+DOB."""
    bundle = _hl_get("Patient?_count=100")
    seen: set = set()
    patients = []
    for entry in bundle.get("entry", []):
        r = entry.get("resource", {})
        name_obj = (r.get("name") or [{}])[0]
        name = f"{' '.join(name_obj.get('given', []))} {name_obj.get('family', '')}".strip()
        dob = r.get("birthDate", "")
        key = f"{name}|{dob}"
        if key in seen:
            continue
        seen.add(key)
        patients.append({
            "id": r.get("id", ""),
            "name": name,
            "birthDate": dob,
            "gender": r.get("gender", ""),
        })
        if len(patients) >= count:
            break
    return {"patients": patients, "total": len(patients)}


@api_router.get("/queue")
async def api_queue() -> dict:
    """Return current screening queue depth and pending messages."""
    sqs = boto3.client("sqs", region_name=AWS_REGION)
    intake_url = _get_queue_urls().get(SCREENING_INTAKE_QUEUE)
    if not intake_url:
        return {"depth": 0, "inFlight": 0, "messages": []}
    try:
        attrs = sqs.get_queue_attributes(
            QueueUrl=intake_url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        depth = int(attrs.get("ApproximateNumberOfMessages", 0))
        in_flight = int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0))
        return {"depth": depth, "inFlight": in_flight, "messages": []}
    except Exception:
        logger.exception("Failed to fetch queue")
        return {"depth": 0, "inFlight": 0, "messages": []}


@api_router.get("/trials")
async def api_trials() -> dict:
    """List configured trials from DynamoDB."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = ddb.Table(PROTOCOL_CONFIG_TABLE)
    try:
        items = _scan_all(
            table,
            ProjectionExpression="trial_id, nct_metadata, #s, questionnaire_id",
            ExpressionAttributeNames={"#s": "status"},
        )
        trials = []
        for item in items:
            meta = item.get("nct_metadata", {})
            trials.append({
                "trialId": item.get("trial_id", ""),
                "id": item.get("trial_id", ""),
                "title": meta.get("title", item.get("trial_id", "")),
                "phase": meta.get("phase", ""),
                "status": item.get("status", "active"),
                "conditions": meta.get("conditions", []),
                "interventions": meta.get("interventions", []),
            })
        return {"trials": trials}
    except Exception:
        logger.exception("Failed to fetch trials")
        return {"trials": []}


@api_router.get("/trial/{trial_id}")
async def api_trial_detail(trial_id: str) -> dict:
    """Return enriched trial detail matching the TrialDetail frontend type."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    try:
        result = ddb.Table(PROTOCOL_CONFIG_TABLE).get_item(Key={"trial_id": trial_id, "version": "1.0"})
        item = result.get("Item")
        if not item:
            raise HTTPException(status_code=404, detail="Trial not found")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to fetch trial %s", trial_id)
        raise HTTPException(status_code=500, detail="Failed to fetch trial")

    meta = item.get("nct_metadata", {})
    q_id = item.get("questionnaire_id", "")

    # Count questionnaire items from HealthLake
    q_items_count = 0
    eligibility_rules: list = []
    if q_id:
        q_fhir_id = q_id.replace("Questionnaire/", "")
        raw = _hl_get(f"Questionnaire/{q_fhir_id}")
        if raw and raw.get("resourceType") == "Questionnaire":
            items = raw.get("item", [])
            q_items_count = len(items)
            for it in items:
                eligibility_rules.append({
                    "id": it.get("linkId", ""),
                    "type": "inclusion" if it.get("linkId", "").startswith("inc") else "exclusion",
                    "description": it.get("text", ""),
                    "data_type": it.get("type", "string"),
                })

    # Pull screening history for this trial
    try:
        audit_table = ddb.Table(AUDIT_LOG_TABLE)
        scan = audit_table.query(
            IndexName="trial_id-index",
            KeyConditionExpression=boto3.dynamodb.conditions.Key("trial_id").eq(trial_id),
            ScanIndexForward=False,
            Limit=500,
        )
        audit_items = scan.get("Items", [])
    except Exception:
        audit_items = []

    by_det: dict[str, int] = {}
    top_fails: dict[str, int] = {}
    screenings_out = []
    for a in sorted(audit_items, key=lambda x: x.get("timestamp", ""), reverse=True):
        det = a.get("eligibility_determination", a.get("determination", "unknown"))
        by_det[det] = by_det.get(det, 0) + 1
        for cr in a.get("criteria_results", []):
            if cr.get("result") == "fail":
                top_fails[cr.get("linkId", "")] = top_fails.get(cr.get("linkId", ""), 0) + 1
        screenings_out.append({
            "id": a.get("session_id", a.get("id", "")),
            "patient_id": a.get("patient_id", ""),
            "authored": a.get("timestamp", ""),
            "determination": a.get("eligibility_determination", a.get("determination", "unknown")),
            "session_id": a.get("session_id", ""),
            "item_count": len(a.get("criteria_results", [])),
            "criteria_results": a.get("criteria_results", []),
        })

    return {
        "trial_id": trial_id,
        "title": meta.get("title", item.get("title", trial_id)),
        "phase": meta.get("phase", ""),
        "status": item.get("status", "active"),
        "conditions": meta.get("conditions", []),
        "interventions": meta.get("interventions", []),
        "blinding": item.get("blinding", {}),
        "questionnaire_id": q_id,
        "questionnaire_items": q_items_count,
        "eligibility_rules": eligibility_rules,
        "total_screenings": len(audit_items),
        "by_determination": by_det,
        "top_failure_criteria": sorted(top_fails.items(), key=lambda x: x[1], reverse=True)[:10],
        "screenings": screenings_out[:50],
        "terminology_versions": item.get("terminology_versions", {}),
        "retention_years": int(item.get("retention_years", 15)),
        "created_at": item.get("created_at", ""),
    }


@api_router.get("/trial/{trial_id}/healthlake-mapping")
async def api_trial_healthlake_mapping(trial_id: str) -> dict:
    """Return HealthLake FHIR resource mapping for a trial."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    try:
        result = ddb.Table(PROTOCOL_CONFIG_TABLE).get_item(Key={"trial_id": trial_id, "version": "1.0"})
        item = result.get("Item")
        if not item:
            raise HTTPException(status_code=404, detail="Trial not found")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to fetch trial %s", trial_id)
        raise HTTPException(status_code=500, detail="Failed to fetch trial")

    meta = item.get("nct_metadata", {})
    q_id = item.get("questionnaire_id", "")

    # Fetch Questionnaire from HealthLake
    q_resource: dict = {}
    q_status = "not_configured"
    q_fhir_id = ""
    q_items: list = []
    if q_id:
        q_fhir_id = q_id.replace("Questionnaire/", "")
        raw = _hl_get(f"Questionnaire/{q_fhir_id}")
        if raw and raw.get("resourceType") == "Questionnaire":
            q_resource = raw
            q_status = "stored"
            q_items = raw.get("item", [])

    # Fetch ResearchStudy from HealthLake (search by identifier)
    rs_status = "not_found"
    rs_fhir_id = ""
    rs_fields: list = []
    rs_bundle = _hl_get(f"ResearchStudy?identifier={trial_id}&_count=1")
    if rs_bundle:
        entries = rs_bundle.get("entry", [])
        if entries:
            rs = entries[0].get("resource", {})
            rs_fhir_id = rs.get("id", "")
            rs_status = "stored"
            rs_fields = [
                {"field": "title", "value": rs.get("title", ""), "path": "ResearchStudy.title"},
                {"field": "status", "value": rs.get("status", ""), "path": "ResearchStudy.status"},
                {"field": "phase", "value": str(rs.get("phase", {}).get("text", "") or rs.get("phase", {}).get("coding", [{}])[0].get("display", "")), "path": "ResearchStudy.phase"},
                {"field": "condition", "value": ", ".join(c.get("text", "") or c.get("coding", [{}])[0].get("display", "") for c in rs.get("condition", [])), "path": "ResearchStudy.condition"},
            ]

    # Count patients enrolled (QuestionnairResponse with questionnaire ref)
    qr_count = 0
    if q_fhir_id:
        qr_bundle = _hl_get(f"QuestionnaireResponse?questionnaire=Questionnaire/{q_fhir_id}&_count=1&_summary=count")
        qr_count = qr_bundle.get("total", 0) if qr_bundle else 0

    # Build FHIR path mappings based on trial conditions
    conditions = meta.get("conditions", [])
    fhir_paths: dict = {}
    if conditions:
        rules = []
        for c in conditions:
            rules.append({
                "ruleId": f"condition-{len(rules)+1}",
                "description": c,
                "fhirPath": "Condition.code.coding.display",
                "type": "inclusion",
            })
        fhir_paths["Condition"] = rules
        fhir_paths["Observation"] = [
            {"ruleId": "obs-labs", "description": "Lab result values (HbA1c, eGFR, creatinine, BP)", "fhirPath": "Observation.value[x]", "type": "inclusion"},
            {"ruleId": "obs-vitals", "description": "Vital sign measurements", "fhirPath": "Observation.component.value[x]", "type": "inclusion"},
        ]
        fhir_paths["MedicationRequest"] = [
            {"ruleId": "med-active", "description": "Active medication exclusion check", "fhirPath": "MedicationRequest.medicationCodeableConcept.coding.display", "type": "exclusion"},
        ]

    questionnaire_fields = [
        {"field": "title", "value": q_resource.get("title", ""), "path": "Questionnaire.title"},
        {"field": "status", "value": q_resource.get("status", ""), "path": "Questionnaire.status"},
        {"field": "identifier", "value": (q_resource.get("identifier", [{}])[0].get("value", "") if q_resource.get("identifier") else ""), "path": "Questionnaire.identifier.value"},
        {"field": "item count", "value": str(len(q_items)), "path": "Questionnaire.item (count)"},
    ] if q_resource else []

    resources = [
        {
            "resourceType": "TrialProtocolConfig",
            "description": "DynamoDB trial configuration record",
            "status": "configured",
            "fhirId": trial_id,
            "fields": [
                {"field": "trial_id", "value": trial_id, "path": "TrialProtocolConfig.trial_id"},
                {"field": "title", "value": meta.get("title", ""), "path": "TrialProtocolConfig.nct_metadata.title"},
                {"field": "phase", "value": meta.get("phase", ""), "path": "TrialProtocolConfig.nct_metadata.phase"},
                {"field": "status", "value": item.get("status", ""), "path": "TrialProtocolConfig.status"},
                {"field": "questionnaire_id", "value": q_id, "path": "TrialProtocolConfig.questionnaire_id"},
            ],
        },
        {
            "resourceType": "Questionnaire",
            "description": "FHIR eligibility criteria questionnaire",
            "status": q_status,
            "fhirId": q_fhir_id,
            "fields": questionnaire_fields,
            "items": [
                {
                    "linkId": it.get("linkId", ""),
                    "text": it.get("text", ""),
                    "type": it.get("type", ""),
                    "required": it.get("required", False),
                    "hasEnableWhen": bool(it.get("enableWhen")),
                    "hasCodes": bool(it.get("answerOption") or it.get("answerValueSet")),
                }
                for it in q_items
            ],
        },
        {
            "resourceType": "ResearchStudy",
            "description": "FHIR ResearchStudy resource (auto-created on trial save)",
            "status": rs_status,
            "fhirId": rs_fhir_id,
            "fields": rs_fields,
        },
        {
            "resourceType": "QuestionnaireResponse",
            "description": f"Patient screening responses ({qr_count} stored)",
            "status": "stored" if qr_count > 0 else "empty",
            "fields": [
                {"field": "count", "value": str(qr_count), "path": "QuestionnaireResponse (search count)"},
                {"field": "questionnaire ref", "value": f"Questionnaire/{q_fhir_id}" if q_fhir_id else "", "path": "QuestionnaireResponse.questionnaire"},
            ],
        },
        {
            "resourceType": "EligibilityCriteriaMapping",
            "description": "FHIR path rules for automated eligibility evaluation",
            "status": "derived",
            "fhirId": "",
            "fhirPaths": fhir_paths,
            "totalRules": sum(len(v) for v in fhir_paths.values()),
        },
    ]

    return {
        "trialId": trial_id,
        "title": meta.get("title", trial_id),
        "resources": resources,
    }


@api_router.get("/patient/{patient_id}")
async def api_patient_detail(patient_id: str) -> dict:
    """Return FHIR detail for a single patient."""
    patient = _hl_get(f"Patient/{patient_id}")
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    fhir = _fetch_patient_fhir(patient_id)
    return {
        "patient": patient,
        "conditions": fhir.get("Condition", []),
        "medications": fhir.get("MedicationRequest", []),
        "observations": fhir.get("Observation", []),
        "allergies": fhir.get("AllergyIntolerance", []),
        "procedures": fhir.get("Procedure", []),
    }


@api_router.get("/patient/{patient_id}/insights")
async def api_patient_insights(patient_id: str) -> dict:
    """Return AI clinical trial insights from HealthLake DocumentReference."""
    import base64 as _b64
    bundle = _hl_get(f"DocumentReference?patient={patient_id}&_count=20")
    for entry in bundle.get("entry", []):
        doc = entry.get("resource", {})
        if doc.get("status") == "entered-in-error":
            continue
        if not any(c.get("code") == "ai-clinical-trial-insights"
                   for c in doc.get("type", {}).get("coding", [])):
            continue
        try:
            b64data = doc["content"][0]["attachment"]["data"]
            insights = json.loads(_b64.b64decode(b64data).decode("utf-8"))
            return {"insights": insights, "generated_at": doc.get("date", "")}
        except Exception:
            logger.exception("Failed to decode insights for %s", patient_id)
    return {"insights": None, "generated_at": None}


@api_router.get("/patient/{patient_id}/trial-history")
async def api_patient_trial_history(patient_id: str) -> dict:
    """Return screening history for a patient from DynamoDB."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = ddb.Table(AUDIT_LOG_TABLE)
    try:
        result = table.query(
            IndexName="patient_id-index",
            KeyConditionExpression=boto3.dynamodb.conditions.Key("patient_id").eq(patient_id),
            ScanIndexForward=False,
            Limit=50,
        )
        items = result.get("Items", [])
    except Exception:
        try:
            result = table.scan(
                FilterExpression=boto3.dynamodb.conditions.Attr("patient_id").eq(patient_id),
                Limit=200,
            )
            items = result.get("Items", [])
        except Exception:
            logger.exception("Trial history scan failed for %s", patient_id)
            items = []

    history = []
    for item in items:
        criteria = item.get("criteria_results") or []
        if isinstance(criteria, str):
            try:
                criteria = json.loads(criteria)
            except Exception:
                criteria = []
        history.append({
            "id": item.get("session_id", str(uuid.uuid4())),
            "trial_id": item.get("trial_id", ""),
            "session_id": item.get("session_id", ""),
            "authored": item.get("timestamp", ""),
            "determination": item.get("eligibility_determination", ""),
            "criteria_results": criteria,
            "item_count": len(criteria),
        })
    return {"history": history, "total": len(history)}


@api_router.post("/chat/start")
async def api_chat_start(req: ChatStartRequest) -> dict:
    """Run AI eligibility screening. Evaluates patient FHIR data against trial
    criteria using Bedrock Claude and returns a complete result immediately."""
    session_id = f"scr_{uuid.uuid4().hex[:12]}"

    # Load patient FHIR data and trial questionnaire in parallel-ish
    try:
        fhir_data = _fetch_patient_fhir(req.patient_id)
    except Exception:
        logger.exception("Failed to load FHIR data for %s", req.patient_id)
        raise HTTPException(status_code=500, detail="Failed to load patient data")

    questionnaire = _fetch_questionnaire(req.trial_id)
    if not questionnaire.get("item"):
        raise HTTPException(status_code=400, detail=f"No questionnaire found for trial {req.trial_id}")

    # Run Bedrock screening
    try:
        result = _run_bedrock_screening(req.patient_id, req.trial_id, fhir_data, questionnaire)
    except json.JSONDecodeError as e:
        logger.exception("Bedrock returned invalid JSON for session %s", session_id)
        raise HTTPException(status_code=500, detail="AI screening returned invalid response")
    except Exception:
        logger.exception("Bedrock screening failed for session %s", session_id)
        raise HTTPException(status_code=500, detail="Screening failed")

    determination = result["determination"]
    criteria_results = result["criteria_results"]

    # Persist audit record
    _write_audit_record(session_id, req.patient_id, req.trial_id, determination, criteria_results)

    return {
        "session_id": session_id,
        "status": "complete",
        "messages": result["messages"],
        "questionnaire_items": len(questionnaire.get("item", [])),
        "progress": {
            "answered": len(criteria_results),
            "total": len(questionnaire.get("item", [])),
        },
        "eligibility": {
            "determination": determination,
            "criteria_results": [
                {
                    "linkId": c.get("linkId", ""),
                    "text": c.get("text", ""),
                    "answer": c.get("reason", ""),
                    "result": c.get("result", ""),
                }
                for c in criteria_results
            ],
        },
        "discrepancies": [],
    }


# ---------------------------------------------------------------------------
# Trial Builder — AI-assisted protocol builder
# ---------------------------------------------------------------------------

_builder_sessions: dict[str, dict] = {}

BUILDER_SYSTEM = """You are a clinical trial protocol designer. Help the user design a clinical trial
by extracting eligibility criteria and building a FHIR Questionnaire.
When the user describes a trial, extract:
- Trial title, phase, condition, intervention
- Inclusion and exclusion criteria as structured questionnaire items
Respond conversationally and ask clarifying questions if needed."""


@api_router.post("/builder/start")
async def api_builder_start() -> dict:
    """Start a new builder session."""
    session_id = f"builder-{uuid.uuid4().hex[:12]}"
    _builder_sessions[session_id] = {"messages": [], "extracted": None, "items": []}
    return {
        "session_id": session_id,
        "status": "started",
        "messages": [{"role": "assistant", "content": "Hello! I'm your clinical trial protocol designer. Describe the trial you'd like to build — include the condition, intervention, and any known eligibility criteria."}],
    }


@api_router.post("/builder/respond")
async def api_builder_respond(body: dict) -> dict:
    """Send a message to the builder session and get an AI response."""
    session_id = body.get("session_id", "")
    message = (body.get("message") or "").strip()
    if len(message) > 4000:
        raise HTTPException(status_code=400, detail="Message exceeds maximum length of 4000 characters")
    if not session_id or not message:
        raise HTTPException(status_code=400, detail="session_id and message required")

    session = _builder_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Builder session not found or expired. Please start a new session.")

    # Build conversation for Bedrock
    bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    session["messages"].append({"role": "user", "content": message})

    conv_messages = [{"role": m["role"], "content": [{"text": m["content"]}]}
                     for m in session["messages"] if m["role"] in ("user", "assistant")]

    extract_instruction = (
        "\n\nIf the user has described enough trial details, respond with a JSON block wrapped in ```json ``` "
        "containing: {\"title\": str, \"phase\": str, \"condition\": str, \"intervention\": str, "
        "\"questionnaire_items\": [{\"linkId\": str, \"text\": str, \"type\": \"boolean\", "
        "\"category\": \"inclusion\"|\"exclusion\", \"required\": bool}]}"
    )

    try:
        resp = bedrock.converse(
            modelId=BEDROCK_MODEL,
            system=[{"text": BUILDER_SYSTEM + extract_instruction}],
            messages=conv_messages,
            inferenceConfig={"maxTokens": 2000, "temperature": 0.3},
        )
        reply = resp["output"]["message"]["content"][0]["text"]
    except Exception:
        logger.exception("Builder respond failed for session %s", session_id)
        reply = "I encountered an error. Please try again."

    session["messages"].append({"role": "assistant", "content": reply})
    _builder_sessions[session_id] = session

    # Parse JSON if present
    extracted = None
    questionnaire_items = None
    status = "chat"
    json_match = re.search(r"```json\s*(.*?)\s*```", reply, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            extracted = {
                "title": data.get("title", ""),
                "phase": data.get("phase", ""),
                "condition": data.get("condition", ""),
                "intervention": data.get("intervention", ""),
            }
            questionnaire_items = data.get("questionnaire_items", [])
            session["extracted"] = extracted
            session["items"] = questionnaire_items
            status = "review"
        except Exception:
            pass

    return {
        "session_id": session_id,
        "status": status,
        "messages": [{"role": "assistant", "content": reply}],
        "extracted": extracted,
        "questionnaire_items": questionnaire_items,
    }


@api_router.post("/builder/create", dependencies=[Depends(_require_write)])
async def api_builder_create(body: dict) -> dict:
    """Create a trial from an approved set of questionnaire items."""
    session_id = body.get("session_id", "")
    approved_items = body.get("approved_items", [])
    trial_overrides = body.get("trial_overrides", {})

    session = _builder_sessions.get(session_id, {})
    extracted = session.get("extracted") or {}

    trial_id = trial_overrides.get("trial_id") or f"CUSTOM-{uuid.uuid4().hex[:8].upper()}"
    title = trial_overrides.get("title") or extracted.get("title") or trial_id
    now = datetime.now(timezone.utc).isoformat()

    # Create a HealthLake Questionnaire so the screener can load it
    questionnaire_id = ""
    if approved_items:
        q_resource = {
            "resourceType": "Questionnaire",
            "status": "active",
            "title": f"{title} — Eligibility Criteria",
            "identifier": [{"system": "https://clinical-trials-builder", "value": trial_id}],
            "item": approved_items,
        }
        q_created = _hl_post("Questionnaire", q_resource)
        questionnaire_id = q_created.get("id", "")
        if not questionnaire_id:
            logger.error("Questionnaire creation failed for builder trial %s", trial_id)
            raise HTTPException(status_code=500, detail="Failed to persist trial criteria — trial not created")

    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = ddb.Table(PROTOCOL_CONFIG_TABLE)
    table.put_item(Item={
        "trial_id": trial_id,
        "version": "1.0",
        "status": "active",
        "title": title,
        "questionnaire_id": questionnaire_id,
        "questionnaire_items": approved_items,
        "builder_session_id": session_id,
        "created_at": now,
        "updated_at": now,
    })

    return {
        "status": "created",
        "trial_id": trial_id,
        "title": title,
        "questionnaire_id": questionnaire_id,
        "questionnaire_items": len(approved_items),
    }


@api_router.post("/chat/respond")
async def api_chat_respond(req: ChatRespondRequest) -> dict:
    """Stub — screening completes in a single /api/chat/start call."""
    return {
        "session_id": req.session_id,
        "status": "complete",
        "messages": [{"role": "assistant", "content": "Screening is already complete. Review the results above."}],
        "questionnaire_items": 0,
        "progress": {"answered": 0, "total": 0},
        "eligibility": None,
    }


@api_router.get("/analytics")
async def api_analytics() -> dict:
    """Aggregate screening analytics for the dashboard."""
    from collections import Counter
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    sqs_client = boto3.client("sqs", region_name=AWS_REGION)

    # Scan only decision_made records that have eligibility_determination
    all_raw: list[dict] = []
    try:
        table = ddb.Table(AUDIT_LOG_TABLE)
        scan_kwargs: dict = {
            "FilterExpression": "event_type = :et AND attribute_exists(eligibility_determination)",
            "ExpressionAttributeValues": {":et": "decision_made"},
            "ProjectionExpression": "trial_id, eligibility_determination, session_id, patient_id, #ts",
            "ExpressionAttributeNames": {"#ts": "timestamp"},
        }
        while True:
            resp = table.scan(**scan_kwargs)
            all_raw.extend(resp.get("Items", []))
            if len(all_raw) >= 10000:
                break
            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception:
        logger.exception("Analytics scan failed")

    # Deduplicate: keep the most recent decision per session
    by_session: dict = {}
    for item in all_raw:
        sid = item.get("session_id", "")
        if not sid:
            continue
        existing = by_session.get(sid)
        if not existing or item.get("timestamp", "") > existing.get("timestamp", ""):
            by_session[sid] = item
    items = list(by_session.values())

    # Queue depth
    queue_depth = 0
    try:
        intake_url = _get_queue_urls().get(SCREENING_INTAKE_QUEUE)
        if intake_url:
            attrs = sqs_client.get_queue_attributes(
                QueueUrl=intake_url,
                AttributeNames=["ApproximateNumberOfMessages"],
            )["Attributes"]
            queue_depth = int(attrs.get("ApproximateNumberOfMessages", 0))
    except Exception:
        pass

    by_det: Counter = Counter()
    by_trial: dict = {}
    recent = []
    for item in items:
        tid = item.get("trial_id", "unknown")
        det = str(item.get("eligibility_determination", "pending")).lower()
        by_det[det] += 1
        if tid not in by_trial:
            by_trial[tid] = Counter()
        by_trial[tid][det] += 1
        recent.append({
            "id": item.get("session_id", ""),
            "patient_id": item.get("patient_id", ""),
            "trial_id": tid,
            "determination": det,
            "authored": item.get("timestamp", ""),
            "session_id": item.get("session_id", ""),
        })

    recent.sort(key=lambda x: x.get("authored", ""), reverse=True)

    # Fetch trial titles from DynamoDB to enrich by_trial
    trial_titles: dict[str, str] = {}
    try:
        config_table = ddb.Table(PROTOCOL_CONFIG_TABLE)
        for cfg in _scan_all(config_table, ProjectionExpression="trial_id, nct_metadata"):
            tid = cfg.get("trial_id", "")
            title = cfg.get("nct_metadata", {}).get("title", "")
            if tid and title:
                trial_titles[tid] = title
    except Exception:
        pass

    all_by_trial = {
        t: {"total": sum(c.values()), **dict(c), "title": trial_titles.get(t, "")}
        for t, c in by_trial.items()
        if t and t != "unknown"
    }
    unique_patients = len({item.get("patient_id", "") for item in items if item.get("patient_id")})

    return {
        "total_screenings": len(items),
        "unique_patients": unique_patients,
        "by_determination": {
            "eligible": by_det.get("eligible", 0),
            "ineligible": by_det.get("ineligible", 0),
            "borderline": by_det.get("borderline", 0),
        },
        "by_trial": all_by_trial,
        "recent_screenings": [r for r in recent if r.get("trial_id", "") and r.get("trial_id") != "unknown"][:20],
        "audit_records": len(items),
        "queue_depth": queue_depth,
    }


# ---------------------------------------------------------------------------
# ClinicalTrials.gov proxy endpoints
# ---------------------------------------------------------------------------

CTG_API = "https://clinicaltrials.gov/api/v2"

@api_router.get("/ctg/search")
async def api_ctg_search(
    query: str = "",
    condition: str = "",
    status: str = "",
    phase: str = "",
    page_size: int = 10,
    page_token: str = "",
) -> dict:
    """Proxy search to ClinicalTrials.gov API v2."""
    query = (query or "").strip()[:200]
    condition = (condition or "").strip()[:200]
    params: dict[str, Any] = {
        "format": "json",
        "pageSize": min(page_size, 25),
        "fields": "NCTId,BriefTitle,OverallStatus,Phase,Condition,LeadSponsorName,EnrollmentCount,StartDate",
    }
    query_parts = []
    if query:
        query_parts.append(query)
    if condition:
        params["query.cond"] = condition
    if query_parts:
        params["query.term"] = " ".join(query_parts)
    if status:
        params["filter.overallStatus"] = status
    if phase:
        params["filter.phase"] = phase
    if page_token:
        params["pageToken"] = page_token

    try:
        resp = http_requests.get(f"{CTG_API}/studies", params=params, timeout=15)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        logger.exception("CTG search failed")
        raise HTTPException(status_code=502, detail=f"ClinicalTrials.gov unavailable: {e}")

    studies = []
    for s in raw.get("studies", []):
        ps = s.get("protocolSection", {})
        id_mod = ps.get("identificationModule", {})
        status_mod = ps.get("statusModule", {})
        design_mod = ps.get("designModule", {})
        contacts_mod = ps.get("contactsLocationsModule", {})
        enroll = design_mod.get("enrollmentInfo", {})
        sponsor = ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name", "")
        studies.append({
            "nctId": id_mod.get("nctId", ""),
            "briefTitle": id_mod.get("briefTitle", ""),
            "overallStatus": status_mod.get("overallStatus", "UNKNOWN"),
            "phase": (design_mod.get("phases") or [""])[0],
            "conditions": ps.get("conditionsModule", {}).get("conditions", []),
            "sponsor": sponsor,
            "enrollment": enroll.get("count"),
            "startDate": status_mod.get("startDateStruct", {}).get("date", ""),
        })

    return {
        "studies": studies,
        "totalCount": raw.get("totalCount", len(studies)),
        "nextPageToken": raw.get("nextPageToken", ""),
    }


@api_router.get("/ctg/study/{nct_id}")
async def api_ctg_study(nct_id: str) -> dict:
    """Fetch full detail for one study from ClinicalTrials.gov."""
    if not re.fullmatch(r'NCT\d{8}', nct_id or ""):
        raise HTTPException(status_code=400, detail="Invalid NCT ID format — expected NCT followed by 8 digits")
    try:
        resp = http_requests.get(
            f"{CTG_API}/studies/{nct_id}",
            params={"format": "json"},
            timeout=15,
        )
        resp.raise_for_status()
        s = resp.json()
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("CTG study fetch failed for %s", nct_id)
        raise HTTPException(status_code=502, detail=f"ClinicalTrials.gov unavailable: {e}")

    ps = s.get("protocolSection", {})
    desc_mod = ps.get("descriptionModule", {})
    elig_mod = ps.get("eligibilityModule", {})
    id_mod = ps.get("identificationModule", {})
    arms = ps.get("armsInterventionsModule", {})
    interventions = [
        f"{iv.get('interventionType','')}: {iv.get('interventionName','')}"
        for iv in arms.get("interventions", [])
    ]
    return {
        "nctId": id_mod.get("nctId", nct_id),
        "briefTitle": id_mod.get("briefTitle", ""),
        "briefSummary": desc_mod.get("briefSummary", ""),
        "eligibilityCriteria": elig_mod.get("eligibilityCriteria", ""),
        "minimumAge": elig_mod.get("minimumAge", ""),
        "maximumAge": elig_mod.get("maximumAge", ""),
        "sex": elig_mod.get("sex", ""),
        "interventions": interventions,
    }


@api_router.post("/ctg/import", dependencies=[Depends(_require_write)])
async def api_ctg_import(body: dict) -> dict:
    """Import a trial from ClinicalTrials.gov into TrialProtocolConfig."""
    # Frontend sends either nctId or nct_id
    nct_id = body.get("nctId") or body.get("nct_id", "")
    if not nct_id:
        raise HTTPException(status_code=400, detail="nctId required")
    if not re.fullmatch(r'NCT\d{8}', nct_id or ""):
        raise HTTPException(status_code=400, detail="Invalid NCT ID format — expected NCT followed by 8 digits")

    # Fetch study detail
    try:
        resp = http_requests.get(
            f"{CTG_API}/studies/{nct_id}",
            params={"format": "json"},
            timeout=15,
        )
        resp.raise_for_status()
        s = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ClinicalTrials.gov fetch failed: {e}")

    ps = s.get("protocolSection", {})
    id_mod = ps.get("identificationModule", {})
    status_mod = ps.get("statusModule", {})
    design_mod = ps.get("designModule", {})
    cond_mod = ps.get("conditionsModule", {})
    arms = ps.get("armsInterventionsModule", {})
    elig_mod = ps.get("eligibilityModule", {})

    title = id_mod.get("briefTitle", nct_id)
    phase_list = design_mod.get("phases", [])
    phase = phase_list[0].replace("PHASE", "Phase ") if phase_list else ""
    conditions = cond_mod.get("conditions", [])
    interventions = [iv.get("interventionName", "") for iv in arms.get("interventions", [])]
    min_age = elig_mod.get("minimumAge", "")
    max_age = elig_mod.get("maximumAge", "")

    nct_metadata = {
        "nct_number": nct_id,
        "title": title,
        "phase": phase,
        "conditions": conditions,
        "interventions": interventions,
        "min_age": min_age,
        "max_age": max_age,
    }

    # Check if already exists
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = ddb.Table(PROTOCOL_CONFIG_TABLE)
    existing = table.get_item(Key={"trial_id": nct_id, "version": "1.0"}).get("Item")
    if existing and existing.get("questionnaire_id"):
        return {
            "status": "imported",
            "nctId": nct_id,
            "trial_id": nct_id,
            "title": title,
            "phase": nct_metadata.get("phase", ""),
            "conditions": nct_metadata.get("conditions", []),
            "interventions": nct_metadata.get("interventions", []),
            "builderSessionId": f"ctg-{nct_id}",
            "builderStatus": "ready",
            "already_existed": True,
        }
    # Fall through: trial exists but has no questionnaire — rebuild it

    # Build FHIR Questionnaire items from CTG eligibility criteria text
    criteria_text = elig_mod.get("eligibilityCriteria", "")
    q_items: list[dict] = []
    if criteria_text:
        lines = [l.strip() for l in criteria_text.splitlines() if l.strip()]
        q_type = "inclusion"
        for line in lines:
            ll = line.lower()
            if "inclusion" in ll:
                q_type = "inclusion"
                continue
            if "exclusion" in ll:
                q_type = "exclusion"
                continue
            if not line or line.startswith("-") and len(line) < 3:
                continue
            text = line.lstrip("-•* ").strip()
            if len(text) < 8:
                continue
            link_id = f"{q_type[:3]}-{len(q_items)+1}"
            q_items.append({
                "linkId": link_id,
                "text": text,
                "type": "boolean",
                "required": True,
                "extension": [{"url": "http://example.org/fhir/criterion-type", "valueString": q_type}],
            })

    if not q_items:
        raise HTTPException(
            status_code=422,
            detail=f"No eligibility criteria could be parsed from {nct_id} — trial cannot be imported",
        )

    q_resource = {
        "resourceType": "Questionnaire",
        "status": "active",
        "title": f"{title} — Eligibility Criteria",
        "identifier": [{"system": "https://clinicaltrials.gov", "value": nct_id}],
        "item": q_items,
    }
    q_created = _hl_post("Questionnaire", q_resource)
    questionnaire_id = q_created.get("id", "")
    if questionnaire_id:
        logger.info("Created Questionnaire %s for trial %s (%d items)", questionnaire_id, nct_id, len(q_items))
    else:
        logger.error("Questionnaire creation returned no ID for %s — aborting import", nct_id)
        raise HTTPException(status_code=500, detail=f"Failed to create eligibility questionnaire for {nct_id}")

    # Write config
    now = datetime.now(timezone.utc).isoformat()
    table.put_item(Item={
        "trial_id": nct_id,
        "version": "1.0",
        "status": "active",
        "questionnaire_id": questionnaire_id,
        "nct_metadata": nct_metadata,
        "created_at": now,
        "updated_at": now,
    })

    return {
        "status": "imported",
        "nctId": nct_id,
        "trial_id": nct_id,
        "title": title,
        "phase": nct_metadata.get("phase", ""),
        "conditions": nct_metadata.get("conditions", []),
        "interventions": nct_metadata.get("interventions", []),
        "builderSessionId": f"ctg-{nct_id}",
        "builderStatus": "ready",
        "questionnaire_id": questionnaire_id,
        "criteria_count": len(q_items),
        "nct_metadata": nct_metadata,
        "already_existed": False,
    }


@api_router.delete("/trial/{trial_id}", dependencies=[Depends(_require_write)])
async def api_delete_trial(trial_id: str) -> dict:
    """Delete a trial from TrialProtocolConfig DynamoDB."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    try:
        ddb.Table(PROTOCOL_CONFIG_TABLE).delete_item(Key={"trial_id": trial_id, "version": "1.0"})
        return {"status": "deleted", "trial_id": trial_id}
    except Exception:
        logger.exception("Failed to delete trial %s", trial_id)
        raise HTTPException(status_code=500, detail="Failed to delete trial")


# ---------------------------------------------------------------------------
# Screening Rules CRUD
# ---------------------------------------------------------------------------

DEFAULT_RULES = [
    {
        "id": "rule-minor-consent",
        "name": "Minor Patient Guardian Consent",
        "description": "Require guardian consent when patient age is below the threshold.",
        "category": "age",
        "trigger": "minor_patient",
        "triggerConfig": {"age_threshold": 18},
        "action": "require_guardian_consent",
        "actionConfig": {"message": "This patient is a minor. Guardian consent is required before proceeding."},
        "enabled": True,
        "priority": 1,
    },
    {
        "id": "rule-critical-discrepancy",
        "name": "Critical Discrepancy Escalation",
        "description": "Escalate to PI when a critical discrepancy is detected between self-report and FHIR data.",
        "category": "safety",
        "trigger": "critical_discrepancy",
        "triggerConfig": {},
        "action": "escalate_to_pi",
        "actionConfig": {"message": "A critical discrepancy was found. This case requires PI review."},
        "enabled": True,
        "priority": 2,
    },
    {
        "id": "rule-age-below-min",
        "name": "Age Below Trial Minimum",
        "description": "Auto-fail when patient age is below the trial's minimum age requirement.",
        "category": "eligibility",
        "trigger": "age_below_minimum",
        "triggerConfig": {},
        "action": "auto_fail",
        "actionConfig": {"message": "Patient does not meet the minimum age requirement for this trial."},
        "enabled": True,
        "priority": 3,
    },
    {
        "id": "rule-age-above-max",
        "name": "Age Above Trial Maximum",
        "description": "Auto-fail when patient age exceeds the trial's maximum age requirement.",
        "category": "eligibility",
        "trigger": "age_above_maximum",
        "triggerConfig": {},
        "action": "auto_fail",
        "actionConfig": {"message": "Patient exceeds the maximum age requirement for this trial."},
        "enabled": True,
        "priority": 4,
    },
    {
        "id": "rule-missing-fhir",
        "name": "Missing FHIR Records",
        "description": "Flag for review when required FHIR records are absent.",
        "category": "data_quality",
        "trigger": "missing_fhir_records",
        "triggerConfig": {},
        "action": "flag_for_review",
        "actionConfig": {"message": "Required clinical data is missing. Manual review needed."},
        "enabled": True,
        "priority": 5,
    },
    {
        "id": "rule-gender-skip",
        "name": "Gender-Based Auto-Skip",
        "description": "Automatically skip gender-specific criteria that don't apply.",
        "category": "demographics",
        "trigger": "gender_not_applicable",
        "triggerConfig": {},
        "action": "auto_pass",
        "actionConfig": {},
        "enabled": True,
        "priority": 6,
    },
]

BASELINE_DEMO_TRIAL_IDS = {"NCT00000001", "NCT00000002"}


def _scan_all(table, **scan_kwargs) -> list:
    """Paginate through an entire DynamoDB table scan, passing through extra kwargs."""
    items = []
    kwargs = dict(scan_kwargs)
    while True:
        result = table.scan(**kwargs)
        items.extend(result.get("Items", []))
        last = result.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def _delete_items(table, key_fields: list[str], items: list[dict]) -> int:
    """Delete DynamoDB items using an explicit key field list."""
    deleted = 0
    for item in items:
        key = {field: item[field] for field in key_fields if field in item}
        if len(key) != len(key_fields):
            continue
        table.delete_item(Key=key)
        deleted += 1
    return deleted


def _hl_delete_search(resource_type: str, search_path: str) -> int:
    """Delete every resource returned by a HealthLake search bundle."""
    deleted = 0
    next_path = search_path
    pages_seen = 0
    while next_path and pages_seen < 20:
        pages_seen += 1
        bundle = _hl_get(next_path)
        for entry in bundle.get("entry", []):
            resource = entry.get("resource", {})
            resource_id = resource.get("id")
            if resource_id and _hl_delete(f"{resource_type}/{quote(resource_id, safe='')}"):
                deleted += 1
        next_path = ""
        for link in bundle.get("link", []):
            if link.get("relation") != "next":
                continue
            url = link.get("url", "")
            if "/r4/" in url:
                next_path = url.split("/r4/", 1)[1]
            else:
                next_path = url
            break
    return deleted


def _reset_screening_rules_table() -> int:
    """Restore the screening rules table to the built-in defaults."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = ddb.Table(SCREENING_RULES_TABLE)
    existing = _scan_all(table)
    _delete_items(table, ["id"], existing)
    for rule in DEFAULT_RULES:
        table.put_item(Item=rule)
    return len(DEFAULT_RULES)


def _get_screening_rules() -> list:
    """Load rules from DynamoDB, seeding defaults if empty."""
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    try:
        table = ddb.Table(SCREENING_RULES_TABLE)
        items = _scan_all(table)
        if not items:
            for rule in DEFAULT_RULES:
                table.put_item(Item=rule)
            return list(DEFAULT_RULES)
        return sorted(items, key=lambda r: r.get("priority", 99))
    except Exception:
        logger.exception("Failed to load screening rules, returning defaults")
        return list(DEFAULT_RULES)


@api_router.get("/screening-rules")
async def api_get_screening_rules() -> dict:
    return {"rules": _get_screening_rules()}


@api_router.post("/screening-rules", dependencies=[Depends(_require_admin)])
async def api_create_screening_rule(body: ScreeningRuleBody) -> dict:
    rule_id = f"rule-{uuid.uuid4().hex[:8]}"
    rule = {**body.model_dump(), "id": rule_id}
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    ddb.Table(SCREENING_RULES_TABLE).put_item(Item=rule)
    return rule


@api_router.put("/screening-rules/{rule_id}", dependencies=[Depends(_require_admin)])
async def api_update_screening_rule(rule_id: str, body: ScreeningRuleBody) -> dict:
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = ddb.Table(SCREENING_RULES_TABLE)
    existing = table.get_item(Key={"id": rule_id}).get("Item")
    if not existing:
        raise HTTPException(status_code=404, detail="Rule not found")
    updated = {**existing, **body.model_dump(), "id": rule_id}
    table.put_item(Item=updated)
    return updated


@api_router.delete("/screening-rules/{rule_id}", dependencies=[Depends(_require_admin)])
async def api_delete_screening_rule(rule_id: str) -> dict:
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    ddb.Table(SCREENING_RULES_TABLE).delete_item(Key={"id": rule_id})
    return {"status": "deleted"}


@api_router.post("/screening-rules/reset", dependencies=[Depends(_require_admin)])
async def api_reset_screening_rules() -> dict:
    _reset_screening_rules_table()
    return {"rules": list(DEFAULT_RULES)}


# ---------------------------------------------------------------------------
# AgentCore / MCP endpoints (called by Q Connect / external agents)
# ---------------------------------------------------------------------------

@app.post("/screen")
async def screen_patient(req: ScreenRequest, request: Request) -> dict:
    """AgentCore: screen a patient for trial eligibility."""
    await _check_api_key(request)
    try:
        fhir_data = _fetch_patient_fhir(req.patient_id)
        questionnaire = _fetch_questionnaire(req.trial_id)
        if not questionnaire.get("item"):
            return {"error": f"No questionnaire for trial {req.trial_id}"}
        result = _run_bedrock_screening(req.patient_id, req.trial_id, fhir_data, questionnaire)
        session_id = f"scr_{uuid.uuid4().hex[:12]}"
        _write_audit_record(session_id, req.patient_id, req.trial_id,
                            result["determination"], result["criteria_results"])
        return {
            "session_id": session_id,
            "patient_id": req.patient_id,
            "trial_id": req.trial_id,
            "determination": result["determination"],
            "summary": result["summary"],
            "criteria_count": len(result["criteria_results"]),
            "criteria_results": result["criteria_results"],
        }
    except Exception:
        logger.exception("AgentCore screen failed for %s/%s", req.patient_id, req.trial_id)
        raise HTTPException(status_code=500, detail="Screening failed")


@app.get("/escalations")
async def list_escalations(request: Request, max_messages: int = 10) -> dict:
    """AgentCore: list pending escalation messages."""
    await _check_api_key(request)
    sqs = boto3.client("sqs", region_name=AWS_REGION)
    queue_url = _get_queue_urls().get(ESCALATION_QUEUE)
    if not queue_url:
        raise HTTPException(status_code=500, detail="Escalation queue not configured")
    try:
        resp = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=min(max_messages, 10),
            WaitTimeSeconds=1,
            MessageAttributeNames=["All"],
        )
    except Exception:
        logger.exception("Failed to receive escalations")
        raise HTTPException(status_code=500, detail="Failed to read escalation queue")

    escalations = []
    for msg in resp.get("Messages", []):
        try:
            body = json.loads(msg.get("Body", "{}"))
        except json.JSONDecodeError:
            body = {"raw": msg.get("Body", "")}
        attrs = {k: v.get("StringValue", "") for k, v in msg.get("MessageAttributes", {}).items()}
        escalations.append({
            "message_id": msg.get("MessageId", ""),
            "body": body,
            "attributes": attrs,
        })
    return {"count": len(escalations), "escalations": escalations}


@app.post("/trials")
async def configure_trial(req: TrialConfigRequest, request: Request) -> dict:
    """AgentCore: configure a new trial from NCT number or protocol text."""
    await _check_api_key(request)
    from components.protocol_engine import (
        clinicaltrials_gov_query,
        generate_fhir_questionnaire,
        parse_protocol_criteria,
    )

    trial_id = req.trial_id or req.nct_number
    if not trial_id:
        raise HTTPException(status_code=400, detail="Provide trial_id or nct_number")

    protocol_text = req.protocol_document or ""
    ctg_metadata: dict = {}

    if req.nct_number:
        try:
            ctg_result = clinicaltrials_gov_query.invoke({"nct_number": req.nct_number})
            ctg_metadata = ctg_result
            ctg_criteria = ctg_result.get("eligibility_criteria", "")
            if ctg_criteria and not protocol_text:
                protocol_text = ctg_criteria
            elif ctg_criteria:
                protocol_text = f"{ctg_criteria}\n\n--- Site Protocol ---\n\n{protocol_text}"
        except Exception:
            logger.warning("ClinicalTrials.gov query failed for %s", req.nct_number)

    if not protocol_text:
        raise HTTPException(status_code=400, detail="No protocol criteria available")

    try:
        parse_result = parse_protocol_criteria.invoke({"protocol_document": protocol_text, "trial_id": trial_id})
        eligibility_rules = parse_result.get("eligibility_rules", [])
        q_result = generate_fhir_questionnaire.invoke({"eligibility_rules": eligibility_rules, "trial_id": trial_id})
    except Exception:
        logger.exception("Protocol parsing failed for %s", trial_id)
        raise HTTPException(status_code=500, detail="Protocol parsing failed")

    return {
        "trial_id": trial_id,
        "questionnaire_id": q_result.get("questionnaire_id", ""),
        "eligibility_rule_count": len(eligibility_rules),
        "ctg_metadata": ctg_metadata,
    }


# ---------------------------------------------------------------------------
# Cilantro AI assistant — in-app chat panel
# ---------------------------------------------------------------------------

_cilantro_history: dict[str, list[dict]] = {}

CILANTRO_SYSTEM = """You are Cilantro, an AI assistant embedded in a clinical trial screening dashboard.
You help clinical trial coordinators with: understanding trial eligibility criteria, interpreting
patient data, explaining screening results, and navigating the dashboard. Be concise and precise."""


@api_router.post("/cilantro/chat")
async def api_cilantro_chat(body: dict) -> dict:
    """Run a single-turn Cilantro chat request and persist history."""
    session_id = body.get("session_id", "default")
    message = (body.get("message") or "").strip()
    if len(message) > 4000:
        raise HTTPException(status_code=400, detail="Message exceeds maximum length of 4000 characters")
    model_id = BEDROCK_MODEL
    system_prompt = CILANTRO_SYSTEM
    history = body.get("history", [])  # passed from client (last N turns)

    if not message:
        raise HTTPException(status_code=400, detail="message required")

    # Build messages for Bedrock
    conv_messages = [
        {"role": m["role"], "content": [{"text": m["content"]}]}
        for m in history
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    conv_messages.append({"role": "user", "content": [{"text": message}]})

    bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    try:
        resp = bedrock.converse(
            modelId=model_id,
            system=[{"text": system_prompt}],
            messages=conv_messages,
            inferenceConfig={"maxTokens": 1000, "temperature": 0.3},
        )
        reply = resp["output"]["message"]["content"][0]["text"]
        usage = resp.get("usage", {})
    except Exception:
        logger.exception("Cilantro chat failed")
        return {"response": "I encountered an error. Please try again.", "model_id": BEDROCK_MODEL}

    # Persist to in-memory history
    now = datetime.now(timezone.utc).isoformat()
    sess = _cilantro_history.setdefault(session_id, [])
    sess.append({"session_id": session_id, "role": "user", "content": message, "timestamp": now})
    sess.append({"session_id": session_id, "role": "assistant", "content": reply, "model_id": model_id, "timestamp": now})
    # Keep last 40 messages per session
    _cilantro_history[session_id] = sess[-40:]

    return {"response": reply, "model_id": model_id, "usage": usage}


@api_router.get("/cilantro/history/{session_id}")
async def api_cilantro_get_history(session_id: str) -> dict:
    """Return the conversation history for a Cilantro session."""
    return {"messages": _cilantro_history.get(session_id, [])}


@api_router.delete("/cilantro/history/{session_id}")
async def api_cilantro_clear_history(session_id: str) -> dict:
    """Clear the conversation history for a Cilantro session."""
    _cilantro_history.pop(session_id, None)
    return {"status": "cleared", "session_id": session_id}


# ---------------------------------------------------------------------------
# DevOps — test suite backed by DynamoDB (PK: flow_id, SK: test_id)
# ---------------------------------------------------------------------------

def _devops_table():
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(DEVOPS_TESTS_TABLE)


@api_router.get("/devops/tests/{flow_id}")
async def api_devops_get_tests(flow_id: str) -> dict:
    try:
        result = _devops_table().query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("flow_id").eq(flow_id),
        )
        return {"tests": result.get("Items", [])}
    except Exception:
        logger.exception("Failed to fetch devops tests for flow %s", flow_id)
        return {"tests": []}


@api_router.post("/devops/tests", dependencies=[Depends(_require_admin)])
async def api_devops_create_test(request: Request) -> dict:
    body = await request.json()
    flow_id = body.get("flow_id", "default")
    test = {"flow_id": flow_id, "test_id": str(uuid.uuid4()), **body}
    try:
        _devops_table().put_item(Item=test)
    except Exception:
        logger.exception("Failed to create devops test")
        raise HTTPException(status_code=500, detail="Failed to create test")
    return test


@api_router.put("/devops/tests/{flow_id}/{test_id}", dependencies=[Depends(_require_admin)])
async def api_devops_update_test(flow_id: str, test_id: str, request: Request) -> dict:
    body = await request.json()
    try:
        result = _devops_table().get_item(Key={"flow_id": flow_id, "test_id": test_id})
        item = result.get("Item")
        if not item:
            raise HTTPException(status_code=404, detail="test not found")
        updated = {**item, **body, "flow_id": flow_id, "test_id": test_id}
        _devops_table().put_item(Item=updated)
        return updated
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to update devops test %s/%s", flow_id, test_id)
        raise HTTPException(status_code=500, detail="Failed to update test")


@api_router.delete("/devops/tests/{flow_id}/{test_id}", dependencies=[Depends(_require_admin)])
async def api_devops_delete_test(flow_id: str, test_id: str) -> dict:
    try:
        _devops_table().delete_item(Key={"flow_id": flow_id, "test_id": test_id})
    except Exception:
        logger.exception("Failed to delete devops test %s/%s", flow_id, test_id)
        raise HTTPException(status_code=500, detail="Failed to delete test")
    return {"status": "deleted"}


@api_router.post("/devops/tests/{flow_id}/{test_id}/run", dependencies=[Depends(_require_admin)])
async def api_devops_run_test(flow_id: str, test_id: str) -> dict:
    return {"test_id": test_id, "status": "not_implemented", "duration_ms": 0, "message": "Test execution not yet implemented — results are not real"}


@api_router.get("/traces/{flow_id}")
async def api_traces_list(flow_id: str, limit: int = 50) -> dict:
    return {"traces": [], "flow_id": flow_id, "note": "Trace collection not yet implemented"}


@api_router.get("/traces/detail/{trace_id}")
async def api_traces_detail(trace_id: str) -> dict:
    return {"trace_id": trace_id, "steps": []}


# ---------------------------------------------------------------------------
# Cognito user management — admin routes
# ---------------------------------------------------------------------------

def _fmt_user(u: dict) -> dict:
    attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
    return {
        "username": u["Username"],
        "email": attrs.get("email", ""),
        "status": u.get("UserStatus", "").lower(),
        "enabled": u.get("Enabled", True),
        "createdAt": u.get("UserCreateDate", "").isoformat() if hasattr(u.get("UserCreateDate", ""), "isoformat") else str(u.get("UserCreateDate", "")),
        "groups": [],
    }


@api_router.get("/admin/users", dependencies=[Depends(_require_admin)])
async def admin_list_users() -> dict:
    try:
        idp = _cognito()
        paginator = idp.get_paginator("list_users")
        users = []
        for page in paginator.paginate(UserPoolId=COGNITO_USER_POOL_ID):
            users.extend([_fmt_user(u) for u in page["Users"]])
        # attach group membership
        for user in users:
            resp = idp.admin_list_groups_for_user(UserPoolId=COGNITO_USER_POOL_ID, Username=user["username"])
            user["groups"] = [g["GroupName"] for g in resp.get("Groups", [])]
        return {"users": users}
    except Exception:
        logger.exception("Failed to list Cognito users")
        raise HTTPException(status_code=500, detail="Failed to list users")


@api_router.get("/admin/groups", dependencies=[Depends(_require_admin)])
async def admin_list_groups() -> dict:
    try:
        idp = _cognito()
        paginator = idp.get_paginator("list_groups")
        groups = []
        for page in paginator.paginate(UserPoolId=COGNITO_USER_POOL_ID):
            for g in page["Groups"]:
                member_paginator = idp.get_paginator("list_users_in_group")
                members = []
                for mp in member_paginator.paginate(UserPoolId=COGNITO_USER_POOL_ID, GroupName=g["GroupName"]):
                    members.extend(u["Username"] for u in mp.get("Users", []))
                groups.append({
                    "name": g["GroupName"],
                    "description": g.get("Description", ""),
                    "precedence": g.get("Precedence", 0),
                    "members": members,
                })
        return {"groups": groups}
    except Exception:
        logger.exception("Failed to list Cognito groups")
        raise HTTPException(status_code=500, detail="Failed to list groups")


@api_router.post("/admin/users", dependencies=[Depends(_require_admin)])
async def admin_create_user(request: Request) -> dict:
    body = await request.json()
    username = (body.get("username") or "").strip()
    email = (body.get("email") or "").strip()
    group = (body.get("group") or "viewer").strip()
    temp_password = (body.get("tempPassword") or "").strip()
    if not username or not email:
        raise HTTPException(status_code=400, detail="username and email required")
    try:
        idp = _cognito()
        kwargs: dict = {
            "UserPoolId": COGNITO_USER_POOL_ID,
            "Username": username,
            "UserAttributes": [{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
            "MessageAction": "SUPPRESS",
        }
        if temp_password:
            kwargs["TemporaryPassword"] = temp_password
        idp.admin_create_user(**kwargs)
        if group:
            idp.admin_add_user_to_group(UserPoolId=COGNITO_USER_POOL_ID, Username=username, GroupName=group)
        return {"username": username, "email": email, "group": group, "status": "FORCE_CHANGE_PASSWORD"}
    except idp.exceptions.UsernameExistsException:
        raise HTTPException(status_code=409, detail="User already exists")
    except Exception:
        logger.exception("Failed to create Cognito user %s", username)
        raise HTTPException(status_code=500, detail="Failed to create user")


@api_router.delete("/admin/users/{username}", dependencies=[Depends(_require_admin)])
async def admin_delete_user(username: str) -> dict:
    try:
        _cognito().admin_delete_user(UserPoolId=COGNITO_USER_POOL_ID, Username=username)
        return {"status": "deleted", "username": username}
    except Exception:
        logger.exception("Failed to delete Cognito user %s", username)
        raise HTTPException(status_code=500, detail="Failed to delete user")


@api_router.post("/admin/users/{username}/groups/{group}", dependencies=[Depends(_require_admin)])
async def admin_add_to_group(username: str, group: str) -> dict:
    try:
        _cognito().admin_add_user_to_group(UserPoolId=COGNITO_USER_POOL_ID, Username=username, GroupName=group)
        return {"status": "added", "username": username, "group": group}
    except Exception:
        logger.exception("Failed to add %s to group %s", username, group)
        raise HTTPException(status_code=500, detail="Failed to add user to group")


@api_router.delete("/admin/users/{username}/groups/{group}", dependencies=[Depends(_require_admin)])
async def admin_remove_from_group(username: str, group: str) -> dict:
    try:
        _cognito().admin_remove_user_from_group(UserPoolId=COGNITO_USER_POOL_ID, Username=username, GroupName=group)
        return {"status": "removed", "username": username, "group": group}
    except Exception:
        logger.exception("Failed to remove %s from group %s", username, group)
        raise HTTPException(status_code=500, detail="Failed to remove user from group")


@api_router.post("/admin/users/{username}/password", dependencies=[Depends(_require_admin)])
async def admin_reset_password(username: str, request: Request) -> dict:
    body = await request.json()
    password = (body.get("password") or "").strip()
    if not password:
        raise HTTPException(status_code=400, detail="password required")
    try:
        _cognito().admin_set_user_password(
            UserPoolId=COGNITO_USER_POOL_ID, Username=username, Password=password, Permanent=True
        )
        return {"status": "updated", "username": username}
    except Exception:
        logger.exception("Failed to reset password for %s", username)
        raise HTTPException(status_code=500, detail="Failed to reset password")


@api_router.post("/admin/demo/reset", dependencies=[Depends(_require_admin)])
async def admin_reset_demo(request: Request) -> dict:
    """Restore the demo to a repeatable baseline without deleting users or patients.

    Clears screening history, Dev Ops tests, pending queues, in-memory assistant history,
    and non-baseline trial configs. Keeps the seeded baseline trials and all Cognito users.
    """
    body = await request.json()
    if body.get("confirm") != "RESET_DEMO":
        raise HTTPException(status_code=400, detail="confirm must be RESET_DEMO")

    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    summary: dict[str, Any] = {
        "screening_history_deleted": 0,
        "devops_tests_deleted": 0,
        "trials_deleted": 0,
        "questionnaires_deleted": 0,
        "questionnaire_responses_deleted": 0,
        "research_studies_deleted": 0,
        "screening_rules_restored": 0,
        "queues_purged": [],
        "assistant_sessions_cleared": 0,
        "preserved_trials": sorted(BASELINE_DEMO_TRIAL_IDS),
        "warnings": [],
    }

    try:
        audit_table = ddb.Table(AUDIT_LOG_TABLE)
        audit_items = _scan_all(audit_table, ProjectionExpression="audit_id")
        summary["screening_history_deleted"] = _delete_items(audit_table, ["audit_id"], audit_items)
    except Exception as e:
        logger.exception("Demo reset failed clearing audit history")
        summary["warnings"].append(f"screening history was not fully cleared: {e}")

    try:
        tests_table = ddb.Table(DEVOPS_TESTS_TABLE)
        test_items = _scan_all(tests_table, ProjectionExpression="flow_id, test_id")
        summary["devops_tests_deleted"] = _delete_items(tests_table, ["flow_id", "test_id"], test_items)
    except Exception as e:
        logger.exception("Demo reset failed clearing DevOps tests")
        summary["warnings"].append(f"dev ops tests were not fully cleared: {e}")

    try:
        protocol_table = ddb.Table(PROTOCOL_CONFIG_TABLE)
        trial_items = _scan_all(
            protocol_table,
            ProjectionExpression="trial_id, version, questionnaire_id",
        )
        trial_delete_items = [
            item for item in trial_items
            if item.get("trial_id") not in BASELINE_DEMO_TRIAL_IDS
        ]
        questionnaire_ids: set[str] = set()
        for item in trial_delete_items:
            trial_id = str(item.get("trial_id", ""))
            q_ref = str(item.get("questionnaire_id", ""))
            if q_ref.startswith("Questionnaire/"):
                q_id = q_ref.split("/", 1)[1]
                if q_id:
                    questionnaire_ids.add(q_id)

            for system in ("https://clinicaltrials.gov", "https://clinical-trials-builder"):
                identifier = quote(f"{system}|{trial_id}", safe="")
                bundle = _hl_get(f"Questionnaire?identifier={identifier}&_count=100")
                for entry in bundle.get("entry", []):
                    q_resource = entry.get("resource", {})
                    q_id = q_resource.get("id")
                    if q_id:
                        questionnaire_ids.add(q_id)

            rs_identifier = quote(trial_id, safe="")
            summary["research_studies_deleted"] += _hl_delete_search(
                "ResearchStudy",
                f"ResearchStudy?identifier={rs_identifier}&_count=100",
            )

        for q_id in sorted(questionnaire_ids):
            q_ref = quote(f"Questionnaire/{q_id}", safe="")
            summary["questionnaire_responses_deleted"] += _hl_delete_search(
                "QuestionnaireResponse",
                f"QuestionnaireResponse?questionnaire={q_ref}&_count=100",
            )
            if _hl_delete(f"Questionnaire/{quote(q_id, safe='')}"):
                summary["questionnaires_deleted"] += 1

        summary["trials_deleted"] = _delete_items(protocol_table, ["trial_id", "version"], trial_delete_items)
    except Exception as e:
        logger.exception("Demo reset failed clearing non-baseline trials")
        summary["warnings"].append(f"custom/imported trials were not fully cleared: {e}")

    try:
        summary["screening_rules_restored"] = _reset_screening_rules_table()
    except Exception as e:
        logger.exception("Demo reset failed restoring screening rules")
        summary["warnings"].append(f"screening rules were not fully restored: {e}")

    try:
        sqs = boto3.client("sqs", region_name=AWS_REGION)
        queue_urls = _get_queue_urls()
        for queue_name in (SCREENING_INTAKE_QUEUE, ESCALATION_QUEUE):
            queue_url = queue_urls.get(queue_name)
            if not queue_url:
                continue
            try:
                sqs.purge_queue(QueueUrl=queue_url)
                summary["queues_purged"].append(queue_name)
            except Exception as e:
                logger.warning("Could not purge queue %s during demo reset: %s", queue_name, e)
                summary["warnings"].append(f"{queue_name} was not purged: {e}")
    except Exception as e:
        logger.exception("Demo reset failed while resolving queues")
        summary["warnings"].append(f"queues were not fully purged: {e}")

    summary["assistant_sessions_cleared"] = len(_cilantro_history)
    _cilantro_history.clear()

    return {"status": "reset", "summary": summary}


# ---------------------------------------------------------------------------
# Register API router (must come after all @api_router decorators)
# ---------------------------------------------------------------------------

app.include_router(api_router)

# ---------------------------------------------------------------------------
# Lambda handler (Mangum wraps FastAPI for API Gateway HTTP API)
# ---------------------------------------------------------------------------

from mangum import Mangum
handler = Mangum(app, lifespan="off")
