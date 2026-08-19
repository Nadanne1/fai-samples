"""Shared configuration for the Clinical Trial Screening & Monitoring System.

All environment-specific values are read from environment variables with sensible
defaults so the same code runs across accounts/regions without edits. Copy
``.env.example`` to ``.env`` and adjust for your deployment.
"""

import os

# ── AWS Account & Region ─────────────────────────────────────────────────────
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")

# ── AWS HealthLake ───────────────────────────────────────────────────────────
HEALTHLAKE_DATASTORE_ID = os.environ.get("HEALTHLAKE_DATASTORE_ID", "")
FHIR_BASE = (
    f"https://healthlake.{AWS_REGION}.amazonaws.com"
    f"/datastore/{HEALTHLAKE_DATASTORE_ID}/r4"
)

# ── Bedrock Guardrails ───────────────────────────────────────────────────────
PHI_PROTECTION_GUARDRAIL_ID = os.environ.get("PHI_PROTECTION_GUARDRAIL_ID", "")
PHI_GUARDRAIL_VERSION = os.environ.get("PHI_GUARDRAIL_VERSION", "DRAFT")

# ── AgentCore Runtime ────────────────────────────────────────────────────────
# Provide either the full ARN (SCREENING_RUNTIME_ARN) or the runtime name
# (SCREENING_RUNTIME_NAME) and let the ARN be composed from account/region.
SCREENING_RUNTIME_NAME = os.environ.get("SCREENING_RUNTIME_NAME", "")
SCREENING_RUNTIME_ARN = os.environ.get("SCREENING_RUNTIME_ARN", "")
if not SCREENING_RUNTIME_ARN and SCREENING_RUNTIME_NAME and AWS_ACCOUNT_ID:
    SCREENING_RUNTIME_ARN = (
        f"arn:aws:bedrock-agentcore:{AWS_REGION}:{AWS_ACCOUNT_ID}"
        f":runtime/{SCREENING_RUNTIME_NAME}"
    )

# ── Cognito ──────────────────────────────────────────────────────────────────
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "us-east-1_ZyRil52W0")

# ── DynamoDB Tables ──────────────────────────────────────────────────────────
AUDIT_LOG_TABLE = os.environ.get("AUDIT_LOG_TABLE", "TrialAuditLog")
PROTOCOL_CONFIG_TABLE = os.environ.get("PROTOCOL_CONFIG_TABLE", "TrialProtocolConfig")
SCREENING_RULES_TABLE = os.environ.get("SCREENING_RULES_TABLE", "TrialScreeningRules")
DEVOPS_TESTS_TABLE = os.environ.get("DEVOPS_TESTS_TABLE", "TrialDevOpsTests")

# ── SQS Queues ───────────────────────────────────────────────────────────────
SCREENING_INTAKE_QUEUE = os.environ.get("SCREENING_INTAKE_QUEUE", "TrialScreeningIntakeQueue")
SCREENING_INTAKE_DLQ = os.environ.get("SCREENING_INTAKE_DLQ", "TrialScreeningIntakeDLQ")
ESCALATION_QUEUE = os.environ.get("ESCALATION_QUEUE", "TrialEscalationQueue")
ESCALATION_DLQ = os.environ.get("ESCALATION_DLQ", "TrialEscalationDLQ")
AUDIT_DLQ = os.environ.get("AUDIT_DLQ", "AuditDeadLetterQueue")

# ── Required resource tags for all AWS resources ─────────────────────────────
REQUIRED_TAGS = {
    "owner": os.environ.get("RESOURCE_OWNER", "gokhul"),
    "scope": os.environ.get("RESOURCE_SCOPE", "poc"),
    "project": "clinical trials",
    "partner": "langchain",
}

# ── LangSmith project names ──────────────────────────────────────────────────
LANGSMITH_PROJECTS = {
    "screening": "clinical-trials-screening",
    "monitoring": "clinical-trials-monitoring",
    "protocol": "clinical-trials-protocol",
    "safety": "clinical-trials-safety",
}

# ── Audit retry configuration ────────────────────────────────────────────────
AUDIT_RETRY_ATTEMPTS = 3
AUDIT_RETRY_BACKOFF_SECONDS = [1, 2, 4]

# ── EDC retry configuration ──────────────────────────────────────────────────
EDC_RETRY_ATTEMPTS = 3
EDC_RETRY_BACKOFF_SECONDS = [5, 15, 45]

# ── Minimum audit record retention (years) ───────────────────────────────────
MIN_RETENTION_YEARS = 15
