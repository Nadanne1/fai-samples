"""Trial configuration store backed by HealthLake ResearchStudy resources.

Replaces the DynamoDB TrialProtocolConfig table with FHIR R4 ResearchStudy
resources stored in HealthLake. Trial metadata, eligibility rules, terminology
versions, blinding config, and CDASH mappings are stored as FHIR extensions.

All functions mirror the previous DynamoDB access patterns so callers can
switch with minimal changes.
"""

import json
import logging
from datetime import datetime, timezone

from tools.healthlake import _fhir_request

logger = logging.getLogger(__name__)

# Extension URL prefix
_EXT_BASE = "http://example.org/fhir/StructureDefinition"


# ── Public API ────────────────────────────────────────────────────────────────


def get_trial_config(trial_id: str) -> dict:
    """Retrieve trial protocol configuration from HealthLake ResearchStudy.

    Searches for a ResearchStudy with an identifier matching the trial_id.
    Returns a dict with the same shape as the old DynamoDB item so callers
    don't need to change their field access patterns.

    Returns an empty dict if not found.
    """
    try:
        bundle = _fhir_request(
            "GET",
            "ResearchStudy",
            params={
                "identifier": f"https://clinicaltrials.gov|{trial_id}",
                "_count": "1",
                "_sort": "-_lastUpdated",
            },
        )
        entries = bundle.get("entry", [])
        if not entries:
            return {}
        resource = entries[0].get("resource", {})
        return _research_study_to_config(resource)
    except Exception:
        logger.exception("Failed to load trial config for %s from HealthLake", trial_id)
        return {}


def list_trial_configs() -> list[dict]:
    """List all trial configurations from HealthLake.

    Returns a list of config dicts (same shape as get_trial_config).
    """
    try:
        bundle = _fhir_request(
            "GET",
            "ResearchStudy",
            params={"_count": "100", "_sort": "-_lastUpdated"},
        )
        entries = bundle.get("entry", [])
        return [_research_study_to_config(e.get("resource", {})) for e in entries]
    except Exception:
        logger.exception("Failed to list trial configs from HealthLake")
        return []


def store_trial_config(
    trial_id: str,
    nct_metadata: dict,
    questionnaire_id: str | None = None,
    eligibility_rules: list[dict] | None = None,
    terminology_versions: dict | None = None,
    blinding_config: dict | None = None,
    cdash_mapping: dict | None = None,
    safety_signal_patterns: list | None = None,
    retention_years: int = 15,
    status: str = "active",
) -> str:
    """Store or update trial configuration as a HealthLake ResearchStudy.

    If a ResearchStudy with the same trial_id identifier already exists, it is
    updated (PUT). Otherwise a new one is created (POST).

    Returns the ResearchStudy resource ID.
    """
    now = datetime.now(timezone.utc).isoformat()

    # Check for existing resource
    existing = _find_research_study(trial_id)
    resource_id = existing.get("id") if existing else None

    resource = _config_to_research_study(
        trial_id=trial_id,
        nct_metadata=nct_metadata,
        questionnaire_id=questionnaire_id,
        eligibility_rules=eligibility_rules,
        terminology_versions=terminology_versions,
        blinding_config=blinding_config,
        cdash_mapping=cdash_mapping,
        safety_signal_patterns=safety_signal_patterns,
        retention_years=retention_years,
        status=status,
        resource_id=resource_id,
        created_at=existing.get("meta", {}).get("lastUpdated", now) if existing else now,
        updated_at=now,
    )

    try:
        body = json.dumps(resource)
        if resource_id:
            result = _fhir_request("PUT", f"ResearchStudy/{resource_id}", body=body)
            logger.info("Updated ResearchStudy for %s (id=%s)", trial_id, resource_id)
        else:
            result = _fhir_request("POST", "ResearchStudy", body=body)
            resource_id = result.get("id", "")
            logger.info("Created ResearchStudy for %s (id=%s)", trial_id, resource_id)
        return resource_id or ""
    except Exception:
        logger.exception("Failed to store trial config for %s", trial_id)
        return ""


# ── Internal helpers ──────────────────────────────────────────────────────────


def _find_research_study(trial_id: str) -> dict | None:
    """Find an existing ResearchStudy by trial identifier."""
    try:
        bundle = _fhir_request(
            "GET",
            "ResearchStudy",
            params={
                "identifier": f"https://clinicaltrials.gov|{trial_id}",
                "_count": "1",
            },
        )
        entries = bundle.get("entry", [])
        return entries[0].get("resource", {}) if entries else None
    except Exception:
        return None


def _config_to_research_study(
    trial_id: str,
    nct_metadata: dict,
    questionnaire_id: str | None,
    eligibility_rules: list[dict] | None,
    terminology_versions: dict | None,
    blinding_config: dict | None,
    cdash_mapping: dict | None,
    safety_signal_patterns: list | None,
    retention_years: int,
    status: str,
    resource_id: str | None,
    created_at: str,
    updated_at: str,
) -> dict:
    """Build a FHIR ResearchStudy resource from trial config fields."""
    # Map status to FHIR ResearchStudy status
    fhir_status_map = {
        "active": "active",
        "draft": "in-review",
        "suspended": "temporarily-closed-to-accrual",
        "completed": "completed",
    }
    fhir_status = fhir_status_map.get(status, "active")

    resource: dict = {
        "resourceType": "ResearchStudy",
        "status": fhir_status,
        "identifier": [
            {"system": "https://clinicaltrials.gov", "value": trial_id},
        ],
        "title": nct_metadata.get("title", f"Trial {trial_id}"),
        "phase": _map_phase(nct_metadata.get("phase", "")),
        "condition": [
            {"text": c} for c in nct_metadata.get("conditions", [])
        ],
    }

    if resource_id:
        resource["id"] = resource_id

    # Extensions for fields that don't map to standard ResearchStudy elements
    extensions = []

    # NCT metadata (full blob for cache/fallback)
    extensions.append({
        "url": f"{_EXT_BASE}/trial-nct-metadata",
        "valueString": json.dumps(nct_metadata),
    })

    # Questionnaire reference
    if questionnaire_id:
        extensions.append({
            "url": f"{_EXT_BASE}/trial-questionnaire-id",
            "valueString": questionnaire_id,
        })

    # Eligibility rules
    if eligibility_rules is not None:
        extensions.append({
            "url": f"{_EXT_BASE}/trial-eligibility-rules",
            "valueString": json.dumps(eligibility_rules),
        })

    # Terminology versions
    if terminology_versions:
        extensions.append({
            "url": f"{_EXT_BASE}/trial-terminology-versions",
            "valueString": json.dumps(terminology_versions),
        })

    # Blinding config
    if blinding_config:
        extensions.append({
            "url": f"{_EXT_BASE}/trial-blinding-config",
            "valueString": json.dumps(blinding_config),
        })

    # CDASH mapping
    if cdash_mapping:
        extensions.append({
            "url": f"{_EXT_BASE}/trial-cdash-mapping",
            "valueString": json.dumps(cdash_mapping),
        })

    # Safety signal patterns
    if safety_signal_patterns:
        extensions.append({
            "url": f"{_EXT_BASE}/trial-safety-signal-patterns",
            "valueString": json.dumps(safety_signal_patterns),
        })

    # Retention years
    extensions.append({
        "url": f"{_EXT_BASE}/trial-retention-years",
        "valueInteger": retention_years,
    })

    # Internal status (our app status, not FHIR status)
    extensions.append({
        "url": f"{_EXT_BASE}/trial-app-status",
        "valueString": status,
    })

    # Timestamps
    extensions.append({
        "url": f"{_EXT_BASE}/trial-created-at",
        "valueString": created_at,
    })
    extensions.append({
        "url": f"{_EXT_BASE}/trial-updated-at",
        "valueString": updated_at,
    })

    resource["extension"] = extensions
    return resource


def _research_study_to_config(resource: dict) -> dict:
    """Convert a FHIR ResearchStudy resource back to the config dict shape.

    Returns a dict with the same keys as the old DynamoDB item:
    trial_id, version, nct_metadata, questionnaire_id, eligibility_rules,
    terminology_versions, blinding_config, cdash_mapping,
    safety_signal_patterns, retention_years, status, created_at, updated_at.
    """
    # Extract trial_id from identifier
    trial_id = ""
    for ident in resource.get("identifier", []):
        if ident.get("system") == "https://clinicaltrials.gov":
            trial_id = ident.get("value", "")
            break

    # Parse extensions
    ext_map: dict[str, str | int] = {}
    for ext in resource.get("extension", []):
        url = ext.get("url", "")
        key = url.rsplit("/", 1)[-1] if "/" in url else url
        if "valueString" in ext:
            ext_map[key] = ext["valueString"]
        elif "valueInteger" in ext:
            ext_map[key] = ext["valueInteger"]

    # Deserialize JSON extensions
    def _json_ext(key: str, default=None):
        val = ext_map.get(key)
        if val is None:
            return default
        if isinstance(val, str):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                return default
        return val

    nct_metadata = _json_ext("trial-nct-metadata", {})
    # Enrich nct_metadata from ResearchStudy standard fields if sparse
    if not nct_metadata.get("title"):
        nct_metadata["title"] = resource.get("title", "")
    if not nct_metadata.get("conditions"):
        nct_metadata["conditions"] = [
            c.get("text", "") for c in resource.get("condition", [])
        ]

    return {
        "trial_id": trial_id,
        "version": "1.0",
        "resource_id": resource.get("id", ""),
        "nct_metadata": nct_metadata,
        "questionnaire_id": ext_map.get("trial-questionnaire-id", ""),
        "eligibility_rules": _json_ext("trial-eligibility-rules", []),
        "terminology_versions": _json_ext("trial-terminology-versions", {}),
        "blinding_config": _json_ext("trial-blinding-config", {}),
        "cdash_mapping": _json_ext("trial-cdash-mapping"),
        "safety_signal_patterns": _json_ext("trial-safety-signal-patterns", []),
        "retention_years": ext_map.get("trial-retention-years", 15),
        "status": ext_map.get("trial-app-status", "active"),
        "created_at": ext_map.get("trial-created-at", ""),
        "updated_at": ext_map.get("trial-updated-at", ""),
    }


def _map_phase(phase_str: str) -> dict | None:
    """Map a phase string to a FHIR CodeableConcept."""
    phase_map = {
        "Phase 1": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/research-study-phase", "code": "phase-1", "display": "Phase 1"}]},
        "Phase 2": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/research-study-phase", "code": "phase-2", "display": "Phase 2"}]},
        "Phase 3": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/research-study-phase", "code": "phase-3", "display": "Phase 3"}]},
        "Phase 4": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/research-study-phase", "code": "phase-4", "display": "Phase 4"}]},
        "Phase 1/Phase 2": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/research-study-phase", "code": "phase-1-phase-2", "display": "Phase 1/Phase 2"}]},
        "Phase 2/Phase 3": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/research-study-phase", "code": "phase-2-phase-3", "display": "Phase 2/Phase 3"}]},
    }
    return phase_map.get(phase_str)
