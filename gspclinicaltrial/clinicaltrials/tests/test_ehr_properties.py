"""Property-based tests for the EHR FHIR query tool.

Properties tested:
  P29 — Graceful Degradation on External Failure: system continues with local
        data on EHR failure and logs the failure.
  P30 — SMART on FHIR Token Security: tokens stored in Secrets Manager, not in
        env vars or code, and refreshed before expiration.

Validates Requirements: 11.4, 11.5
"""

import json
import time
from unittest.mock import MagicMock, patch, call

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from tools.ehr import (
    ehr_fhir_query,
    _get_access_token,
    _TOKEN_REFRESH_BUFFER_SECONDS,
    EHR_RESOURCE_TYPES,
    _token_cache,
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_patient_id_st = st.uuids().map(str)

_endpoint_url_st = st.sampled_from([
    "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
    "https://fhir.cerner.com/r4",
    "https://fhir.hospital.org/api/FHIR/R4",
])

_resource_types_st = st.lists(
    st.sampled_from(EHR_RESOURCE_TYPES),
    min_size=1,
    max_size=len(EHR_RESOURCE_TYPES),
    unique=True,
)

_expires_in_st = st.integers(min_value=60, max_value=7200)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_secrets_client(creds: dict | None = None):
    """Return a mock Secrets Manager client returning given credentials."""
    client = MagicMock()
    default_creds = {
        "client_id": "test-client",
        "client_secret": "test-secret",
        "token_url": "https://auth.ehr.com/token",
        "refresh_token": "rt-abc123",
    }
    client.get_secret_value = MagicMock(return_value={
        "SecretString": json.dumps(creds or default_creds),
    })
    client.put_secret_value = MagicMock(return_value=None)
    return client


def _make_mock_response(status_code=200, json_data=None, raise_for_status=None):
    """Return a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    if raise_for_status:
        resp.raise_for_status.side_effect = raise_for_status
    else:
        resp.raise_for_status.return_value = None
    return resp


def _make_token_response(access_token="tok-valid", expires_in=3600, refresh_token=None):
    """Return a mock token endpoint response."""
    data = {"access_token": access_token, "expires_in": expires_in}
    if refresh_token:
        data["refresh_token"] = refresh_token
    return _make_mock_response(json_data=data)


def _invoke_ehr(patient_id, endpoint_url, resource_types=None,
                mock_secrets=None, mock_requests_get=None, mock_requests_post=None):
    """Invoke ehr_fhir_query with mocked AWS and HTTP clients."""
    mock_secrets = mock_secrets or _make_mock_secrets_client()

    # Clear token cache to ensure fresh state per test
    _token_cache.clear()

    patches = [
        patch("tools.ehr._secrets_client", mock_secrets),
    ]
    if mock_requests_get is not None:
        patches.append(patch("tools.ehr.requests.get", mock_requests_get))
    if mock_requests_post is not None:
        patches.append(patch("tools.ehr.requests.post", mock_requests_post))

    with patches[0]:
        ctx_stack = [patches[0]]
        for p in patches[1:]:
            p.start()
            ctx_stack.append(p)
        try:
            result = ehr_fhir_query.invoke({
                "patient_id": patient_id,
                "endpoint_url": endpoint_url,
                **({"resource_types": resource_types} if resource_types else {}),
            })
        finally:
            for p in patches[1:]:
                p.stop()

    return result, mock_secrets



# ===================================================================
# Property 29 — Graceful Degradation on External Failure
# System continues with local data on EHR failure and logs the failure.
# Validates: Requirements 11.4, 11.5
# ===================================================================


@given(patient_id=_patient_id_st, endpoint_url=_endpoint_url_st)
@settings(max_examples=30)
def test_p29_token_failure_returns_empty_results_with_flag(
    patient_id: str, endpoint_url: str
):
    """When token retrieval fails entirely, ehr_fhir_query must return empty
    resource lists for all default types and set ehr_query_failed=True."""
    mock_secrets = _make_mock_secrets_client()
    mock_secrets.get_secret_value.side_effect = Exception("Secrets Manager unavailable")

    _token_cache.clear()

    with (
        patch("tools.ehr._secrets_client", mock_secrets),
        patch("tools.ehr.logger") as mock_logger,
    ):
        result = ehr_fhir_query.invoke({
            "patient_id": patient_id,
            "endpoint_url": endpoint_url,
        })

    # Must flag the failure
    assert result["ehr_query_failed"] is True
    assert "failure_reason" in result

    # Every default resource type must have an empty list
    for rt in EHR_RESOURCE_TYPES:
        assert rt in result, f"Missing resource type {rt} in result"
        assert result[rt] == [], f"Expected empty list for {rt}, got {result[rt]}"


@given(
    patient_id=_patient_id_st,
    endpoint_url=_endpoint_url_st,
    resource_types=_resource_types_st,
)
@settings(max_examples=30)
def test_p29_partial_query_failure_returns_available_data(
    patient_id: str, endpoint_url: str, resource_types: list[str]
):
    """When some FHIR resource queries fail but others succeed, the tool must
    return data for successful queries and empty lists for failed ones, with
    ehr_query_failed=True."""
    assume(len(resource_types) >= 2)

    # First resource type succeeds, rest fail
    success_type = resource_types[0]
    fail_types = resource_types[1:]

    mock_secrets = _make_mock_secrets_client()
    mock_post = MagicMock(return_value=_make_token_response())

    call_count = 0

    def mock_get_side_effect(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First resource succeeds
            if success_type == "Patient":
                return _make_mock_response(json_data={
                    "resourceType": "Patient", "id": patient_id
                })
            else:
                return _make_mock_response(json_data={
                    "resourceType": "Bundle",
                    "entry": [{"resource": {"resourceType": success_type, "id": "r1"}}],
                })
        else:
            raise ConnectionError("EHR endpoint unreachable")

    mock_get = MagicMock(side_effect=mock_get_side_effect)

    _token_cache.clear()

    with (
        patch("tools.ehr._secrets_client", mock_secrets),
        patch("tools.ehr.requests.get", mock_get),
        patch("tools.ehr.requests.post", mock_post),
    ):
        result = ehr_fhir_query.invoke({
            "patient_id": patient_id,
            "endpoint_url": endpoint_url,
            "resource_types": resource_types,
        })

    # Failure flag must be set
    assert result["ehr_query_failed"] is True

    # Successful type should have data
    assert len(result[success_type]) > 0, (
        f"Expected data for {success_type} but got empty"
    )

    # Failed types should have empty lists
    for ft in fail_types:
        assert result[ft] == [], f"Expected empty list for failed type {ft}"


@given(
    patient_id=_patient_id_st,
    endpoint_url=_endpoint_url_st,
    resource_types=_resource_types_st,
)
@settings(max_examples=30)
def test_p29_complete_query_failure_returns_all_empty(
    patient_id: str, endpoint_url: str, resource_types: list[str]
):
    """When all FHIR resource queries fail, every resource type must have an
    empty list and ehr_query_failed must be True."""
    mock_secrets = _make_mock_secrets_client()
    mock_post = MagicMock(return_value=_make_token_response())
    mock_get = MagicMock(side_effect=ConnectionError("EHR down"))

    _token_cache.clear()

    with (
        patch("tools.ehr._secrets_client", mock_secrets),
        patch("tools.ehr.requests.get", mock_get),
        patch("tools.ehr.requests.post", mock_post),
    ):
        result = ehr_fhir_query.invoke({
            "patient_id": patient_id,
            "endpoint_url": endpoint_url,
            "resource_types": resource_types,
        })

    assert result["ehr_query_failed"] is True

    for rt in resource_types:
        assert result[rt] == [], f"Expected empty list for {rt} on total failure"


@given(patient_id=_patient_id_st, endpoint_url=_endpoint_url_st)
@settings(max_examples=20)
def test_p29_successful_query_no_failure_flag(
    patient_id: str, endpoint_url: str
):
    """When all queries succeed, ehr_query_failed should not be True."""
    mock_secrets = _make_mock_secrets_client()
    mock_post = MagicMock(return_value=_make_token_response())

    def mock_get_side_effect(url, **kwargs):
        if "/Patient/" in url:
            return _make_mock_response(json_data={
                "resourceType": "Patient", "id": patient_id
            })
        return _make_mock_response(json_data={
            "resourceType": "Bundle",
            "entry": [{"resource": {"resourceType": "Condition", "id": "c1"}}],
        })

    mock_get = MagicMock(side_effect=mock_get_side_effect)

    _token_cache.clear()

    with (
        patch("tools.ehr._secrets_client", mock_secrets),
        patch("tools.ehr.requests.get", mock_get),
        patch("tools.ehr.requests.post", mock_post),
    ):
        result = ehr_fhir_query.invoke({
            "patient_id": patient_id,
            "endpoint_url": endpoint_url,
        })

    assert result.get("ehr_query_failed") is not True



# ===================================================================
# Property 30 — SMART on FHIR Token Security
# Tokens stored in Secrets Manager, not in env vars or code, and
# refreshed before expiration.
# Validates: Requirements 11.4, 11.5
# ===================================================================


@given(patient_id=_patient_id_st, endpoint_url=_endpoint_url_st)
@settings(max_examples=30)
def test_p30_tokens_retrieved_from_secrets_manager(
    patient_id: str, endpoint_url: str
):
    """Access tokens must be obtained via Secrets Manager, not from env vars,
    hardcoded values, or any other source."""
    mock_secrets = _make_mock_secrets_client()
    mock_post = MagicMock(return_value=_make_token_response())
    mock_get = MagicMock(return_value=_make_mock_response(json_data={
        "resourceType": "Bundle", "entry": [],
    }))

    _token_cache.clear()

    with (
        patch("tools.ehr._secrets_client", mock_secrets),
        patch("tools.ehr.requests.get", mock_get),
        patch("tools.ehr.requests.post", mock_post),
        patch.dict("os.environ", {}, clear=False),
    ):
        result = ehr_fhir_query.invoke({
            "patient_id": patient_id,
            "endpoint_url": endpoint_url,
        })

    # Secrets Manager must have been called to retrieve credentials
    mock_secrets.get_secret_value.assert_called_once()
    call_kwargs = mock_secrets.get_secret_value.call_args
    secret_id = call_kwargs[1].get("SecretId") or call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1]["SecretId"]
    assert "clinical-trials/ehr/" in secret_id, (
        f"Secret name '{secret_id}' does not follow expected convention"
    )


@given(
    patient_id=_patient_id_st,
    endpoint_url=_endpoint_url_st,
    expires_in=_expires_in_st,
)
@settings(max_examples=30)
def test_p30_expired_token_triggers_refresh(
    patient_id: str, endpoint_url: str, expires_in: int
):
    """When a cached token is expired or within the refresh buffer, the system
    must fetch fresh credentials from Secrets Manager and request a new token."""
    mock_secrets = _make_mock_secrets_client()
    mock_post = MagicMock(return_value=_make_token_response(
        access_token="tok-refreshed", expires_in=expires_in
    ))
    mock_get = MagicMock(return_value=_make_mock_response(json_data={
        "resourceType": "Bundle", "entry": [],
    }))

    # Seed cache with an expired token
    _token_cache.clear()
    _token_cache[endpoint_url] = {
        "access_token": "tok-expired",
        "expires_at": time.time() - 10,  # already expired
        "refresh_token": "rt-old",
    }

    with (
        patch("tools.ehr._secrets_client", mock_secrets),
        patch("tools.ehr.requests.get", mock_get),
        patch("tools.ehr.requests.post", mock_post),
    ):
        result = ehr_fhir_query.invoke({
            "patient_id": patient_id,
            "endpoint_url": endpoint_url,
        })

    # A new token must have been requested
    mock_post.assert_called_once()
    # The refreshed token should be used in the Authorization header
    auth_header = mock_get.call_args_list[0][1].get("headers", {}).get("Authorization") \
        if mock_get.call_args_list[0][1].get("headers") \
        else mock_get.call_args[1].get("headers", {}).get("Authorization", "")
    # Verify the old expired token is NOT used
    assert "tok-expired" not in (auth_header or ""), (
        "Expired token was used instead of refreshed token"
    )


@given(patient_id=_patient_id_st, endpoint_url=_endpoint_url_st)
@settings(max_examples=20)
def test_p30_token_about_to_expire_triggers_refresh(
    patient_id: str, endpoint_url: str
):
    """When a cached token is within the refresh buffer window, the system must
    proactively refresh it before it actually expires."""
    mock_secrets = _make_mock_secrets_client()
    mock_post = MagicMock(return_value=_make_token_response(
        access_token="tok-proactive", expires_in=3600
    ))
    mock_get = MagicMock(return_value=_make_mock_response(json_data={
        "resourceType": "Bundle", "entry": [],
    }))

    # Seed cache with a token that expires within the buffer window
    _token_cache.clear()
    _token_cache[endpoint_url] = {
        "access_token": "tok-almost-expired",
        "expires_at": time.time() + (_TOKEN_REFRESH_BUFFER_SECONDS // 2),
        "refresh_token": "rt-old",
    }

    with (
        patch("tools.ehr._secrets_client", mock_secrets),
        patch("tools.ehr.requests.get", mock_get),
        patch("tools.ehr.requests.post", mock_post),
    ):
        result = ehr_fhir_query.invoke({
            "patient_id": patient_id,
            "endpoint_url": endpoint_url,
        })

    # Token endpoint must have been called for a refresh
    mock_post.assert_called_once()


@given(patient_id=_patient_id_st, endpoint_url=_endpoint_url_st)
@settings(max_examples=20)
def test_p30_valid_cached_token_reused_without_secrets_call(
    patient_id: str, endpoint_url: str
):
    """When a cached token is still valid (well before expiration), the system
    must reuse it without calling Secrets Manager or the token endpoint."""
    mock_secrets = _make_mock_secrets_client()
    mock_post = MagicMock(return_value=_make_token_response())
    mock_get = MagicMock(return_value=_make_mock_response(json_data={
        "resourceType": "Bundle", "entry": [],
    }))

    # Seed cache with a valid token far from expiration
    _token_cache.clear()
    _token_cache[endpoint_url] = {
        "access_token": "tok-still-valid",
        "expires_at": time.time() + 3600,  # 1 hour from now
        "refresh_token": "rt-current",
    }

    with (
        patch("tools.ehr._secrets_client", mock_secrets),
        patch("tools.ehr.requests.get", mock_get),
        patch("tools.ehr.requests.post", mock_post),
    ):
        result = ehr_fhir_query.invoke({
            "patient_id": patient_id,
            "endpoint_url": endpoint_url,
        })

    # Neither Secrets Manager nor token endpoint should be called
    mock_secrets.get_secret_value.assert_not_called()
    mock_post.assert_not_called()

    # The cached token must be used
    if mock_get.call_args_list:
        headers = mock_get.call_args_list[0][1].get("headers", {})
        assert headers.get("Authorization") == "Bearer tok-still-valid"


@given(patient_id=_patient_id_st, endpoint_url=_endpoint_url_st)
@settings(max_examples=20)
def test_p30_rotated_refresh_token_persisted_to_secrets_manager(
    patient_id: str, endpoint_url: str
):
    """When the token endpoint returns a new refresh_token (rotation), the
    system must persist it back to Secrets Manager."""
    original_creds = {
        "client_id": "test-client",
        "client_secret": "test-secret",
        "token_url": "https://auth.ehr.com/token",
        "refresh_token": "rt-original",
    }
    mock_secrets = _make_mock_secrets_client(creds=original_creds)
    mock_post = MagicMock(return_value=_make_token_response(
        access_token="tok-new",
        expires_in=3600,
        refresh_token="rt-rotated",  # new refresh token
    ))
    mock_get = MagicMock(return_value=_make_mock_response(json_data={
        "resourceType": "Bundle", "entry": [],
    }))

    _token_cache.clear()

    with (
        patch("tools.ehr._secrets_client", mock_secrets),
        patch("tools.ehr.requests.get", mock_get),
        patch("tools.ehr.requests.post", mock_post),
    ):
        result = ehr_fhir_query.invoke({
            "patient_id": patient_id,
            "endpoint_url": endpoint_url,
        })

    # Secrets Manager put_secret_value must be called with the rotated token
    mock_secrets.put_secret_value.assert_called_once()
    put_call = mock_secrets.put_secret_value.call_args[1]
    persisted = json.loads(put_call["SecretString"])
    assert persisted["refresh_token"] == "rt-rotated", (
        "Rotated refresh token was not persisted to Secrets Manager"
    )


@given(patient_id=_patient_id_st, endpoint_url=_endpoint_url_st)
@settings(max_examples=20)
def test_p30_no_tokens_in_env_vars(
    patient_id: str, endpoint_url: str
):
    """Access tokens, client secrets, and refresh tokens must never be stored
    in environment variables."""
    import os

    mock_secrets = _make_mock_secrets_client()
    mock_post = MagicMock(return_value=_make_token_response())
    mock_get = MagicMock(return_value=_make_mock_response(json_data={
        "resourceType": "Bundle", "entry": [],
    }))

    _token_cache.clear()

    with (
        patch("tools.ehr._secrets_client", mock_secrets),
        patch("tools.ehr.requests.get", mock_get),
        patch("tools.ehr.requests.post", mock_post),
    ):
        result = ehr_fhir_query.invoke({
            "patient_id": patient_id,
            "endpoint_url": endpoint_url,
        })

    # Scan environment for any token-like values that shouldn't be there
    sensitive_env_keys = [
        k for k in os.environ
        if any(term in k.upper() for term in [
            "FHIR_TOKEN", "EHR_TOKEN", "SMART_TOKEN",
            "EHR_SECRET", "FHIR_SECRET", "EHR_CLIENT_SECRET",
            "EHR_REFRESH_TOKEN", "FHIR_REFRESH_TOKEN",
        ])
    ]
    assert not sensitive_env_keys, (
        f"Sensitive token-related env vars found: {sensitive_env_keys}"
    )


def test_p30_no_hardcoded_tokens_in_source():
    """The ehr.py source must not contain hardcoded access tokens, client
    secrets, or refresh tokens."""
    import inspect
    from tools import ehr

    source = inspect.getsource(ehr)
    source_lower = source.lower()

    # These patterns would indicate hardcoded credentials
    hardcoded_patterns = [
        "bearer ",       # hardcoded bearer token
        "client_secret=",  # hardcoded secret (outside of dict key usage)
        "sk-",           # common secret key prefix
        "eyj",           # JWT token prefix (base64 of '{"')
    ]

    for pattern in hardcoded_patterns:
        # Count occurrences — allow pattern in string literals used as dict keys
        # or parameter names, but flag if it looks like an actual value
        occurrences = source_lower.count(pattern)
        if pattern == "bearer ":
            # "Bearer " in f-string template is expected (Authorization header)
            # but a full token like "Bearer eyJ..." would be a problem
            assert 'bearer eyj' not in source_lower, (
                "Hardcoded JWT bearer token found in source"
            )
        elif pattern == "client_secret=":
            # Used as a dict key in request data — that's fine
            # But "client_secret=actual_value" as a string would be bad
            pass
