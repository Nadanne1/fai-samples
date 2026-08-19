"""EHR FHIR query tool for external EHR integration via SMART on FHIR.

Queries external EHR systems (Epic, Cerner) using FHIR R4 RESTful API
with SMART on FHIR authorization. Tokens are stored in AWS Secrets Manager
and refreshed before expiration. Implements graceful degradation on failure.
"""

import json
import logging
import time
from datetime import datetime, timezone

import boto3
import requests
from langchain_core.tools import tool

from config.settings import AWS_REGION

logger = logging.getLogger(__name__)

_secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)

# In-memory token cache: {endpoint_url: {access_token, expires_at, refresh_token}}
_token_cache: dict[str, dict] = {}

# Refresh tokens 60 seconds before expiration
_TOKEN_REFRESH_BUFFER_SECONDS = 60

# FHIR resource types to query from external EHR
EHR_RESOURCE_TYPES = [
    "Patient",
    "Condition",
    "MedicationRequest",
    "AllergyIntolerance",
    "Procedure",
    "Observation",
]


def _get_smart_credentials(endpoint_url: str) -> dict:
    """Retrieve SMART on FHIR credentials from AWS Secrets Manager.

    Secret name convention: clinical-trials/ehr/{sanitized_endpoint}

    Returns:
        Dict with client_id, client_secret, token_url, and optionally
        refresh_token and access_token with expires_at.
    """
    # Sanitize endpoint URL for use as secret name component
    sanitized = endpoint_url.replace("https://", "").replace("/", "_").rstrip("_")
    secret_name = f"clinical-trials/ehr/{sanitized}"

    response = _secrets_client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


def _get_access_token(endpoint_url: str) -> str | None:
    """Get a valid access token for the EHR endpoint, refreshing if needed.

    Checks the in-memory cache first. If the token is expired or about to
    expire, refreshes it using the refresh_token from Secrets Manager.

    Returns:
        A valid access token string, or None if token retrieval fails.
    """
    now = time.time()

    # Check cache
    cached = _token_cache.get(endpoint_url)
    if cached and cached.get("expires_at", 0) > now + _TOKEN_REFRESH_BUFFER_SECONDS:
        return cached["access_token"]

    try:
        creds = _get_smart_credentials(endpoint_url)
        token_url = creds["token_url"]

        # Use refresh_token if available, otherwise client_credentials
        if creds.get("refresh_token"):
            token_response = requests.post(
                token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": creds["refresh_token"],
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"],
                },
                timeout=30,
            )
        else:
            token_response = requests.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"],
                    "scope": "system/*.read",
                },
                timeout=30,
            )

        token_response.raise_for_status()
        token_data = token_response.json()

        access_token = token_data["access_token"]
        expires_in = token_data.get("expires_in", 3600)

        # Cache the token
        _token_cache[endpoint_url] = {
            "access_token": access_token,
            "expires_at": now + expires_in,
            "refresh_token": token_data.get("refresh_token", creds.get("refresh_token")),
        }

        # Persist new refresh_token back to Secrets Manager if rotated
        new_refresh = token_data.get("refresh_token")
        if new_refresh and new_refresh != creds.get("refresh_token"):
            creds["refresh_token"] = new_refresh
            _secrets_client.put_secret_value(
                SecretId=f"clinical-trials/ehr/{endpoint_url.replace('https://', '').replace('/', '_').rstrip('_')}",
                SecretString=json.dumps(creds),
            )

        return access_token
    except Exception:
        logger.exception("Failed to obtain access token for %s", endpoint_url)
        return None


@tool
def ehr_fhir_query(
    patient_id: str,
    endpoint_url: str,
    resource_types: list[str] | None = None,
) -> dict:
    """Query an external EHR system via SMART on FHIR for patient resources.

    Retrieves clinical data from Epic, Cerner, or other FHIR R4 endpoints.
    Tokens are managed via AWS Secrets Manager and refreshed before expiration.
    On failure, returns empty results and logs the failure (graceful degradation).

    Args:
        patient_id: The patient identifier in the external EHR system.
        endpoint_url: The FHIR R4 base URL of the external EHR
            (e.g., 'https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4').
        resource_types: Optional list of FHIR resource types to query.
            Defaults to Patient, Condition, MedicationRequest,
            AllergyIntolerance, Procedure, Observation.

    Returns:
        Dict with resource_type keys mapping to lists of FHIR resources.
        On failure, returns empty lists for all resource types and an
        'ehr_query_failed' flag set to True.
    """
    types_to_query = resource_types or EHR_RESOURCE_TYPES
    empty_results = {rt: [] for rt in types_to_query}

    # Get access token
    access_token = _get_access_token(endpoint_url)
    if not access_token:
        logger.error("No access token available for %s — returning empty results", endpoint_url)
        return {**empty_results, "ehr_query_failed": True, "failure_reason": "token_retrieval_failed"}

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/fhir+json",
    }

    results: dict = {}
    query_failed = False

    for resource_type in types_to_query:
        try:
            if resource_type == "Patient":
                url = f"{endpoint_url.rstrip('/')}/Patient/{patient_id}"
                resp = requests.get(url, headers=headers, timeout=30)
                resp.raise_for_status()
                results[resource_type] = [resp.json()]
            else:
                url = f"{endpoint_url.rstrip('/')}/{resource_type}"
                resp = requests.get(
                    url,
                    headers=headers,
                    params={"patient": patient_id},
                    timeout=30,
                )
                resp.raise_for_status()
                bundle = resp.json()
                entries = bundle.get("entry", [])
                results[resource_type] = [e.get("resource", e) for e in entries]
        except Exception:
            logger.exception(
                "EHR query failed for %s/%s at %s",
                resource_type,
                patient_id,
                endpoint_url,
            )
            results[resource_type] = []
            query_failed = True

    if query_failed:
        results["ehr_query_failed"] = True
        results["failure_reason"] = "partial_query_failure"

    return results
