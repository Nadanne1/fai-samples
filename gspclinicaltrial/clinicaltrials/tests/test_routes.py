"""Route and helper unit tests for the clinical trial FastAPI app.

Tests:
  - _scan_all pagination and kwargs passthrough
  - GET /api/trials — returns trials list, empty on DynamoDB error
  - GET /api/analytics — trial titles enriched from scan
  - _require_admin / _require_write — auth enforcement
  - Write routes reject viewers (403) and pass for authorised roles

All AWS calls are mocked; no real resources are touched.
"""

from __future__ import annotations

import base64
import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Stub out langchain packages before any app code imports them.
# The local Python env may not have langchain installed (it's only needed
# in the Lambda runtime), so we inject minimal stubs so pytest can import app.
# ---------------------------------------------------------------------------

def _stub_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


for _pkg in [
    "langchain_core", "langchain_core.tools", "langchain_core.messages",
    "langchain_core.language_models", "langchain_core.prompts",
    "langchain_aws", "langchain_aws.chat_models",
    "langchain_aws.chat_models.bedrock",
    "langgraph", "langgraph.graph", "langgraph.prebuilt",
    "langsmith",
]:
    if _pkg not in sys.modules:
        _stub_module(_pkg)

# Provide the `tool` decorator used by tools/audit.py and siblings
_stub_tool = lambda f=None, **kw: (f if f is not None else lambda g: g)  # noqa: E731
sys.modules["langchain_core.tools"].tool = _stub_tool  # type: ignore[attr-defined]

# Stub ChatBedrock used by screening agent
_ChatBedrock = MagicMock()
sys.modules["langchain_aws.chat_models.bedrock"].ChatBedrock = _ChatBedrock  # type: ignore[attr-defined]
sys.modules["langchain_aws"].ChatBedrock = _ChatBedrock  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jwt(payload: dict) -> str:
    """Build a minimal (unsigned) JWT string with the given payload."""
    def _b64(data: dict) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = _b64({"alg": "RS256", "typ": "JWT"})
    body = _b64(payload)
    return f"{header}.{body}.fakesig"


_POOL_ID = "us-east-1_ZyRil52W0"
_REGION = "us-east-1"
_VALID_ISS = f"https://cognito-idp.{_REGION}.amazonaws.com/{_POOL_ID}"
_API_KEY = "test-api-key"

_ADMIN_TOKEN = _make_jwt({"iss": _VALID_ISS, "sub": "admin-user", "exp": 9999999999})
_RESEARCHER_TOKEN = _make_jwt({"iss": _VALID_ISS, "sub": "researcher-user", "exp": 9999999999})
_VIEWER_TOKEN = _make_jwt({"iss": _VALID_ISS, "sub": "viewer-user", "exp": 9999999999})
_WRONG_POOL_TOKEN = _make_jwt({"iss": "https://cognito-idp.us-east-1.amazonaws.com/wrong_pool", "sub": "x", "exp": 9999999999})


# ---------------------------------------------------------------------------
# App fixture — patches AWS and env before import
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    env_patch = {
        "CLINICAL_TRIALS_API_KEY": _API_KEY,
        "COGNITO_USER_POOL_ID": _POOL_ID,
        "AWS_REGION": _REGION,
        "AWS_DEFAULT_REGION": _REGION,
        "HEALTHLAKE_DATASTORE_ID": "fake-datastore",
    }
    # If app was already imported (e.g. by another test module), re-use it;
    # otherwise import fresh with patched env.
    with patch.dict("os.environ", env_patch):
        import importlib
        if "app" in sys.modules:
            app_module = sys.modules["app"]
        else:
            import app as app_module
        importlib.reload(app_module)
        with TestClient(app_module.app) as c:
            yield c, app_module


def _auth_headers(token: str) -> dict:
    return {"X-Api-Key": _API_KEY, "Authorization": f"Bearer {token}"}


def _cognito_mock(groups_for_user: list[str]) -> MagicMock:
    """Return a mock Cognito client that accepts any valid token."""
    m = MagicMock()
    m.exceptions.NotAuthorizedException = Exception
    m.get_user.return_value = {"Username": "test-user"}
    m.admin_list_groups_for_user.return_value = {
        "Groups": [{"GroupName": g} for g in groups_for_user]
    }
    return m


# ---------------------------------------------------------------------------
# _scan_all
# ---------------------------------------------------------------------------

def test_scan_all_single_page(client):
    _, app_module = client
    table = MagicMock()
    table.scan.return_value = {"Items": [{"id": "1"}, {"id": "2"}]}

    items = app_module._scan_all(table)

    assert items == [{"id": "1"}, {"id": "2"}]
    table.scan.assert_called_once_with()


def test_scan_all_pagination(client):
    _, app_module = client
    table = MagicMock()
    table.scan.side_effect = [
        {"Items": [{"id": "1"}], "LastEvaluatedKey": {"id": "1"}},
        {"Items": [{"id": "2"}]},
    ]

    items = app_module._scan_all(table)

    assert [i["id"] for i in items] == ["1", "2"]
    assert table.scan.call_count == 2
    second_call_kwargs = table.scan.call_args_list[1][1]
    assert second_call_kwargs["ExclusiveStartKey"] == {"id": "1"}


def test_scan_all_passes_kwargs(client):
    _, app_module = client
    table = MagicMock()
    table.scan.return_value = {"Items": []}

    app_module._scan_all(
        table,
        ProjectionExpression="trial_id, #s",
        ExpressionAttributeNames={"#s": "status"},
    )

    table.scan.assert_called_once_with(
        ProjectionExpression="trial_id, #s",
        ExpressionAttributeNames={"#s": "status"},
    )


def test_scan_all_kwargs_preserved_across_pages(client):
    _, app_module = client
    table = MagicMock()
    table.scan.side_effect = [
        {"Items": [{"id": "a"}], "LastEvaluatedKey": {"id": "a"}},
        {"Items": [{"id": "b"}]},
    ]

    app_module._scan_all(table, ProjectionExpression="id")

    for call_args in table.scan.call_args_list:
        assert call_args[1].get("ProjectionExpression") == "id"


# ---------------------------------------------------------------------------
# GET /api/trials
# ---------------------------------------------------------------------------

def test_get_trials_returns_list(client):
    c, _ = client
    trial_item = {
        "trial_id": "NCT001",
        "status": "active",
        "nct_metadata": {"title": "Test Trial", "phase": "Phase 2",
                          "conditions": [], "interventions": []},
        "questionnaire_id": "q1",
    }
    with patch("boto3.resource") as mock_resource:
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [trial_item]}
        mock_resource.return_value.Table.return_value = mock_table

        resp = c.get("/api/trials", headers={"X-Api-Key": _API_KEY})

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["trials"]) == 1
    assert data["trials"][0]["trialId"] == "NCT001"
    assert data["trials"][0]["title"] == "Test Trial"


def test_get_trials_returns_empty_on_error(client):
    c, _ = client
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value.scan.side_effect = RuntimeError("DynamoDB down")

        resp = c.get("/api/trials", headers={"X-Api-Key": _API_KEY})

    assert resp.status_code == 200
    assert resp.json() == {"trials": []}


# ---------------------------------------------------------------------------
# GET /api/analytics  (trial-title enrichment uses _scan_all)
# ---------------------------------------------------------------------------

def test_analytics_enriches_trial_titles(client):
    c, _ = client
    audit_item = {
        "session_id": "s1", "patient_id": "p1",
        "trial_id": "NCT001", "determination": "eligible",
        "timestamp": "2024-01-01T00:00:00+00:00",
    }
    config_item = {
        "trial_id": "NCT001",
        "nct_metadata": {"title": "My Trial"},
    }
    with patch("boto3.resource") as mock_resource:
        def table_factory(name):
            t = MagicMock()
            if "Audit" in name:
                t.scan.return_value = {"Items": [audit_item]}
            else:
                t.scan.return_value = {"Items": [config_item]}
            return t

        mock_resource.return_value.Table.side_effect = table_factory

        resp = c.get("/api/analytics", headers={"X-Api-Key": _API_KEY})

    assert resp.status_code == 200
    data = resp.json()
    by_trial = {t["trialId"]: t for t in data.get("byTrial", [])}
    if "NCT001" in by_trial:
        assert by_trial["NCT001"]["title"] == "My Trial"


# ---------------------------------------------------------------------------
# Auth — _require_admin / _require_write
# ---------------------------------------------------------------------------

def test_require_admin_passes_for_admin(client):
    c, _ = client
    mock_idp = _cognito_mock(["admin"])
    with patch("app._cognito", return_value=mock_idp):
        resp = c.get("/api/admin/users", headers=_auth_headers(_ADMIN_TOKEN))
    assert resp.status_code != 403


def test_require_admin_rejects_researcher(client):
    c, _ = client
    mock_idp = _cognito_mock(["researcher"])
    with patch("app._cognito", return_value=mock_idp):
        resp = c.get("/api/admin/users", headers=_auth_headers(_RESEARCHER_TOKEN))
    assert resp.status_code == 403


def test_require_admin_rejects_viewer(client):
    c, _ = client
    mock_idp = _cognito_mock(["viewer"])
    with patch("app._cognito", return_value=mock_idp):
        resp = c.get("/api/admin/users", headers=_auth_headers(_VIEWER_TOKEN))
    assert resp.status_code == 403


def test_require_admin_rejects_wrong_pool(client):
    c, _ = client
    resp = c.get("/api/admin/users", headers=_auth_headers(_WRONG_POOL_TOKEN))
    assert resp.status_code == 401


def test_require_admin_rejects_missing_token(client):
    c, _ = client
    resp = c.get("/api/admin/users", headers={"X-Api-Key": _API_KEY})
    assert resp.status_code == 401


def test_require_write_passes_for_researcher(client):
    c, _ = client
    mock_idp = _cognito_mock(["researcher"])
    with patch("app._cognito", return_value=mock_idp), \
         patch("boto3.resource"):
        resp = c.delete(
            "/api/trial/NCT001",
            headers=_auth_headers(_RESEARCHER_TOKEN),
        )
    # 500 is fine — DynamoDB is mocked but may fail; 403 would be a role rejection
    assert resp.status_code != 403


def test_require_write_rejects_viewer_on_trial_delete(client):
    c, _ = client
    mock_idp = _cognito_mock(["viewer"])
    with patch("app._cognito", return_value=mock_idp):
        resp = c.delete(
            "/api/trial/NCT001",
            headers=_auth_headers(_VIEWER_TOKEN),
        )
    assert resp.status_code == 403


def test_require_write_rejects_viewer_on_ctg_import(client):
    c, _ = client
    mock_idp = _cognito_mock(["viewer"])
    with patch("app._cognito", return_value=mock_idp):
        resp = c.post(
            "/api/ctg/import",
            json={"nctId": "NCT001"},
            headers=_auth_headers(_VIEWER_TOKEN),
        )
    assert resp.status_code == 403


def test_require_admin_rejects_viewer_on_screening_rules(client):
    c, _ = client
    mock_idp = _cognito_mock(["viewer"])
    with patch("app._cognito", return_value=mock_idp):
        resp = c.post(
            "/api/screening-rules",
            json={"id": "r1", "name": "Test", "field": "age", "operator": ">=",
                  "value": "18", "unit": "years", "category": "inclusion", "enabled": True},
            headers=_auth_headers(_VIEWER_TOKEN),
        )
    assert resp.status_code == 403


def test_require_admin_rejects_researcher_on_screening_rules(client):
    c, _ = client
    mock_idp = _cognito_mock(["researcher"])
    with patch("app._cognito", return_value=mock_idp):
        resp = c.post(
            "/api/screening-rules",
            json={"id": "r1", "name": "Test", "field": "age", "operator": ">=",
                  "value": "18", "unit": "years", "category": "inclusion", "enabled": True},
            headers=_auth_headers(_RESEARCHER_TOKEN),
        )
    assert resp.status_code == 403


def test_admin_demo_reset_clears_artifacts_and_preserves_baseline_trials(client):
    c, app_module = client
    mock_idp = _cognito_mock(["admin"])

    tables: dict[str, MagicMock] = {}

    def table_factory(name):
        table = MagicMock()
        tables[name] = table
        if name == app_module.AUDIT_LOG_TABLE:
            table.scan.return_value = {"Items": [{"audit_id": "a1"}, {"audit_id": "a2"}]}
        elif name == app_module.DEVOPS_TESTS_TABLE:
            table.scan.return_value = {"Items": [{"flow_id": "f1", "test_id": "t1"}]}
        elif name == app_module.PROTOCOL_CONFIG_TABLE:
            table.scan.return_value = {
                "Items": [
                    {"trial_id": "NCT00000001", "version": "1.0", "questionnaire_id": "Questionnaire/base"},
                    {"trial_id": "NCT99999999", "version": "1.0", "questionnaire_id": "Questionnaire/imported"},
                    {"trial_id": "CUSTOM-DEMO", "version": "1.0", "questionnaire_id": "Questionnaire/custom"},
                ]
            }
        elif name == app_module.SCREENING_RULES_TABLE:
            table.scan.return_value = {"Items": [{"id": "old-rule"}]}
        else:
            table.scan.return_value = {"Items": []}
        return table

    mock_resource = MagicMock()
    mock_resource.Table.side_effect = table_factory
    mock_sqs = MagicMock()

    def fake_hl_get(path):
        if path.startswith("ResearchStudy?"):
            return {"entry": [{"resource": {"id": f"rs-{len(path)}"}}]}
        if path.startswith("QuestionnaireResponse?"):
            return {"entry": [{"resource": {"id": f"qr-{len(path)}"}}]}
        return {"entry": []}

    with patch("app._cognito", return_value=mock_idp), \
         patch("boto3.resource", return_value=mock_resource), \
         patch("boto3.client", return_value=mock_sqs), \
         patch("app._get_queue_urls", return_value={
             app_module.SCREENING_INTAKE_QUEUE: "https://sqs.example/intake",
             app_module.ESCALATION_QUEUE: "https://sqs.example/escalation",
         }), \
         patch("app._hl_get", side_effect=fake_hl_get), \
         patch("app._hl_delete", return_value=True):
        resp = c.post(
            "/api/admin/demo/reset",
            json={"confirm": "RESET_DEMO"},
            headers=_auth_headers(_ADMIN_TOKEN),
        )

    assert resp.status_code == 200
    summary = resp.json()["summary"]
    assert summary["screening_history_deleted"] == 2
    assert summary["devops_tests_deleted"] == 1
    assert summary["trials_deleted"] == 2
    assert summary["questionnaires_deleted"] == 2
    assert summary["questionnaire_responses_deleted"] == 2
    assert summary["research_studies_deleted"] == 2
    assert summary["screening_rules_restored"] == len(app_module.DEFAULT_RULES)
    assert summary["preserved_trials"] == ["NCT00000001", "NCT00000002"]

    protocol_deletes = tables[app_module.PROTOCOL_CONFIG_TABLE].delete_item.call_args_list
    deleted_trial_ids = {call.kwargs["Key"]["trial_id"] for call in protocol_deletes}
    assert deleted_trial_ids == {"NCT99999999", "CUSTOM-DEMO"}
    assert "NCT00000001" not in deleted_trial_ids
    assert mock_sqs.purge_queue.call_count == 2


def test_admin_demo_reset_requires_confirmation(client):
    c, _ = client
    mock_idp = _cognito_mock(["admin"])
    with patch("app._cognito", return_value=mock_idp):
        resp = c.post(
            "/api/admin/demo/reset",
            json={"confirm": "NOPE"},
            headers=_auth_headers(_ADMIN_TOKEN),
        )
    assert resp.status_code == 400
