"""HealthLake FHIR R4 tools using SigV4-signed HTTP requests."""

import json
import logging
from urllib.parse import urlencode

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.httpsession import URLLib3Session
from langchain_core.tools import tool

from config.settings import AWS_REGION, FHIR_BASE

logger = logging.getLogger(__name__)

_http = URLLib3Session()
_session = boto3.Session(region_name=AWS_REGION)

SCREENING_RESOURCE_TYPES = [
    "Patient", "Condition", "MedicationRequest",
    "AllergyIntolerance", "Procedure", "Observation",
]


def _fhir_request(method: str, path: str, params: dict | None = None, body: str | None = None) -> dict:
    """Make a SigV4-signed FHIR request to HealthLake."""
    creds = _session.get_credentials().get_frozen_credentials()

    url = f"{FHIR_BASE}/{path}"
    if params:
        url = f"{url}?{urlencode(params)}"

    headers = {"Accept": "application/fhir+json"}
    if body:
        headers["Content-Type"] = "application/fhir+json"

    req = AWSRequest(method=method, url=url, data=body or "", headers=headers)
    SigV4Auth(creds, "healthlake", AWS_REGION).add_auth(req)

    resp = _http.send(req.prepare())
    content = resp.content.decode("utf-8")

    if resp.status_code not in (200, 201):
        logger.error("FHIR %s %s -> %s: %s", method, path, resp.status_code, content[:200])
        return {}

    return json.loads(content) if content else {}


@tool
def healthlake_query_patient(patient_id: str, resource_types: list[str] | None = None) -> dict:
    """Query HealthLake FHIR R4 datastore for patient clinical resources.

    Args:
        patient_id: The FHIR Patient resource ID.
        resource_types: Optional list of FHIR resource types to query.

    Returns:
        Dict with resource_type keys mapping to lists of FHIR resources.
    """
    types_to_query = resource_types or SCREENING_RESOURCE_TYPES
    results: dict[str, list] = {}

    for resource_type in types_to_query:
        try:
            if resource_type == "Patient":
                resource = _fhir_request("GET", f"Patient/{patient_id}")
                results[resource_type] = [resource] if resource.get("resourceType") else []
            else:
                bundle = _fhir_request("GET", resource_type, {"patient": patient_id, "_count": "20"})
                entries = bundle.get("entry", [])
                results[resource_type] = [e.get("resource", {}) for e in entries]
        except Exception:
            logger.exception("Error querying %s for patient %s", resource_type, patient_id)
            results[resource_type] = []

    return results


@tool
def healthlake_store_resource(resource: dict) -> dict:
    """Store a FHIR resource in HealthLake. Creates new or updates existing.

    Args:
        resource: A valid FHIR R4 resource dict with 'resourceType'.

    Returns:
        Dict with 'resource_id', 'resource_type', and 'status'.
    """
    resource_type = resource.get("resourceType")
    if not resource_type:
        return {"error": "resource must include 'resourceType'"}

    resource_id = resource.get("id")
    body = json.dumps(resource)

    try:
        if resource_id:
            result = _fhir_request("PUT", f"{resource_type}/{resource_id}", body=body)
            status = "updated"
        else:
            result = _fhir_request("POST", resource_type, body=body)
            status = "created"

        return {
            "resource_id": result.get("id", resource_id or ""),
            "resource_type": resource_type,
            "status": status,
        }
    except Exception:
        logger.exception("Error storing %s resource in HealthLake", resource_type)
        return {"error": f"Failed to store {resource_type} resource"}
