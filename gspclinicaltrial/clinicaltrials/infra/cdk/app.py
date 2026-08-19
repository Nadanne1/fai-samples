#!/usr/bin/env python3
"""CDK application entry point — Lambda + API Gateway stack."""
import os
import aws_cdk as cdk
from clinical_trials_stack import ClinicalTrialsStack

app = cdk.App()

account = os.environ.get("CDK_DEFAULT_ACCOUNT") or os.environ.get("AWS_ACCOUNT_ID")
region = os.environ.get("CDK_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "us-east-1"

def _ctx(key: str, env_key: str = "") -> str:
    return os.environ.get(env_key or key, "") or app.node.try_get_context(key) or ""

ClinicalTrialsStack(
    app, "ClinicalTrialsStack",
    env=cdk.Environment(account=account, region=region),
    healthlake_datastore_id=_ctx("healthlake_datastore_id", "HEALTHLAKE_DATASTORE_ID"),
    phi_guardrail_id=_ctx("phi_guardrail_id", "PHI_PROTECTION_GUARDRAIL_ID"),
    phi_guardrail_version=_ctx("phi_guardrail_version", "PHI_GUARDRAIL_VERSION") or "DRAFT",
    screening_runtime_arn=_ctx("screening_runtime_arn", "SCREENING_RUNTIME_ARN"),
    description="Clinical Trial Screening & Monitoring — Lambda + API Gateway",
)

app.synth()
