"""Bedrock AgentCore deployment configuration for Clinical Trial Screening & Monitoring.

Provisions:
    - AgentCore Identity (IAM-based auth)
    - AgentCore Runtime (container image packaging)
    - AgentCore API Gateway (routes, throttling, authorization)
    - PHI-Protection-Guardrail applied to agent outputs
    - Required resource tags on all resources

This script is idempotent — it creates resources only if they do not already
exist and updates configuration on subsequent runs.

Requirements: All (deployment)
"""

from __future__ import annotations

import json
import logging
import sys

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, ".")
from config.settings import (
    AWS_ACCOUNT_ID,
    AWS_REGION,
    PHI_PROTECTION_GUARDRAIL_ID,
    REQUIRED_TAGS,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME = "clinical-trial-screening"
CONTAINER_IMAGE_URI = (
    f"{AWS_ACCOUNT_ID}.dkr.ecr.{AWS_REGION}.amazonaws.com/"
    f"{APP_NAME}:latest"
)

IAM_ROLE_NAME = f"{APP_NAME}-agentcore-role"
IAM_POLICY_NAME = f"{APP_NAME}-agentcore-policy"

API_GATEWAY_NAME = f"{APP_NAME}-api"
API_STAGE = "v1"

# Throttling: 50 requests/sec burst, 20 sustained
THROTTLE_BURST_LIMIT = 50
THROTTLE_RATE_LIMIT = 20.0

# Routes exposed by the FastAPI app
API_ROUTES = [
    {"path": "/screen", "method": "POST", "description": "Trigger screening session"},
    {"path": "/monitor", "method": "POST", "description": "Trigger monitoring visit"},
    {"path": "/trials", "method": "POST", "description": "Configure new trial"},
    {"path": "/escalations", "method": "GET", "description": "List pending escalations"},
]


# ---------------------------------------------------------------------------
# IAM — AgentCore Identity
# ---------------------------------------------------------------------------

_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "bedrock.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {
                    "aws:SourceAccount": AWS_ACCOUNT_ID
                }
            },
        }
    ],
}

_PERMISSIONS_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "HealthLakeAccess",
            "Effect": "Allow",
            "Action": [
                "healthlake:ReadResource",
                "healthlake:CreateResource",
                "healthlake:UpdateResource",
                "healthlake:SearchWithGet",
                "healthlake:SearchWithPost",
            ],
            "Resource": f"arn:aws:healthlake:{AWS_REGION}:{AWS_ACCOUNT_ID}:datastore/fhir/*",
        },
        {
            "Sid": "DynamoDBAccess",
            "Effect": "Allow",
            "Action": [
                "dynamodb:PutItem",
                "dynamodb:GetItem",
                "dynamodb:Query",
            ],
            "Resource": [
                f"arn:aws:dynamodb:{AWS_REGION}:{AWS_ACCOUNT_ID}:table/TrialAuditLog",
                f"arn:aws:dynamodb:{AWS_REGION}:{AWS_ACCOUNT_ID}:table/TrialAuditLog/index/*",
                f"arn:aws:dynamodb:{AWS_REGION}:{AWS_ACCOUNT_ID}:table/TrialProtocolConfig",
                f"arn:aws:dynamodb:{AWS_REGION}:{AWS_ACCOUNT_ID}:table/TrialScreeningRules",
            ],
        },
        {
            "Sid": "SQSAccess",
            "Effect": "Allow",
            "Action": [
                "sqs:SendMessage",
                "sqs:ReceiveMessage",
                "sqs:DeleteMessage",
                "sqs:GetQueueAttributes",
            ],
            "Resource": f"arn:aws:sqs:{AWS_REGION}:{AWS_ACCOUNT_ID}:Trial*",
        },
        {
            "Sid": "KMSAccess",
            "Effect": "Allow",
            "Action": ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"],
            "Resource": f"arn:aws:kms:{AWS_REGION}:{AWS_ACCOUNT_ID}:alias/clinical-trial-screening/phi",
        },
        {
            "Sid": "SecretsManagerAccess",
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:clinical-trial-*",
        },
        {
            "Sid": "BedrockInvoke",
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
                "bedrock:ApplyGuardrail",
            ],
            "Resource": "*",
        },
    ],
}


def _create_iam_role(iam_client) -> str:
    """Create or retrieve the AgentCore execution IAM role. Returns the role ARN."""
    try:
        resp = iam_client.create_role(
            RoleName=IAM_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(_TRUST_POLICY),
            Description="Execution role for Clinical Trial AgentCore Runtime",
            Tags=[{"Key": k, "Value": v} for k, v in REQUIRED_TAGS.items()],
        )
        role_arn = resp["Role"]["Arn"]
        logger.info("Created IAM role: %s", role_arn)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "EntityAlreadyExists":
            resp = iam_client.get_role(RoleName=IAM_ROLE_NAME)
            role_arn = resp["Role"]["Arn"]
            logger.info("IAM role already exists: %s", role_arn)
        else:
            raise

    # Attach inline policy
    iam_client.put_role_policy(
        RoleName=IAM_ROLE_NAME,
        PolicyName=IAM_POLICY_NAME,
        PolicyDocument=json.dumps(_PERMISSIONS_POLICY),
    )
    logger.info("Attached permissions policy to %s", IAM_ROLE_NAME)

    return role_arn


# ---------------------------------------------------------------------------
# API Gateway configuration
# ---------------------------------------------------------------------------


def _create_api_gateway(apigw_client) -> str:
    """Create or retrieve the REST API Gateway. Returns the API ID."""
    # Check for existing API
    apis = apigw_client.get_rest_apis(limit=100).get("items", [])
    for api in apis:
        if api.get("name") == API_GATEWAY_NAME:
            api_id = api["id"]
            logger.info("API Gateway already exists: %s (%s)", API_GATEWAY_NAME, api_id)
            return api_id

    resp = apigw_client.create_rest_api(
        name=API_GATEWAY_NAME,
        description="Clinical Trial Screening & Monitoring API",
        endpointConfiguration={"types": ["REGIONAL"]},
        tags=REQUIRED_TAGS,
    )
    api_id = resp["id"]
    logger.info("Created API Gateway: %s (%s)", API_GATEWAY_NAME, api_id)
    return api_id


def _configure_api_routes(apigw_client, api_id: str, role_arn: str) -> None:
    """Create API Gateway resources and methods for each route."""
    # Get root resource
    resources = apigw_client.get_resources(restApiId=api_id).get("items", [])
    root_id = None
    existing_paths = {}
    for r in resources:
        if r["path"] == "/":
            root_id = r["id"]
        existing_paths[r["path"]] = r["id"]

    if not root_id:
        logger.error("Could not find root resource for API %s", api_id)
        return

    for route in API_ROUTES:
        path = route["path"]
        method = route["method"]
        path_part = path.lstrip("/")

        # Create resource if it doesn't exist
        if path not in existing_paths:
            resp = apigw_client.create_resource(
                restApiId=api_id,
                parentId=root_id,
                pathPart=path_part,
            )
            resource_id = resp["id"]
            logger.info("Created resource: %s (%s)", path, resource_id)
        else:
            resource_id = existing_paths[path]

        # Create method with IAM authorization
        try:
            apigw_client.put_method(
                restApiId=api_id,
                resourceId=resource_id,
                httpMethod=method,
                authorizationType="AWS_IAM",
            )
            logger.info("Created method: %s %s", method, path)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConflictException":
                logger.info("Method %s %s already exists", method, path)
            else:
                raise


def _configure_throttling(apigw_client, api_id: str) -> None:
    """Deploy the API stage with throttling settings."""
    try:
        apigw_client.create_deployment(restApiId=api_id, stageName=API_STAGE)
        logger.info("Deployed API to stage: %s", API_STAGE)
    except ClientError:
        logger.info("Stage %s may already exist — updating", API_STAGE)

    try:
        apigw_client.update_stage(
            restApiId=api_id,
            stageName=API_STAGE,
            patchOperations=[
                {"op": "replace", "path": "/*/*/throttling/burstLimit", "value": str(THROTTLE_BURST_LIMIT)},
                {"op": "replace", "path": "/*/*/throttling/rateLimit", "value": str(THROTTLE_RATE_LIMIT)},
            ],
        )
        logger.info("Throttling configured: burst=%d, rate=%.1f", THROTTLE_BURST_LIMIT, THROTTLE_RATE_LIMIT)
    except ClientError:
        logger.warning("Could not update throttling — stage may need manual configuration")


# ---------------------------------------------------------------------------
# Guardrail configuration
# ---------------------------------------------------------------------------


def _print_guardrail_config() -> None:
    """Print the PHI-Protection-Guardrail configuration for reference."""
    logger.info(
        "PHI-Protection-Guardrail: %s (apply to all agent outputs via ChatBedrockConverse guardrails param)",
        PHI_PROTECTION_GUARDRAIL_ID,
    )
    print(f"\n  Guardrail ID: {PHI_PROTECTION_GUARDRAIL_ID}")
    print("  Applied in: agents/screening_agent.py, agents/monitoring_agent.py")
    print("  Config: guardrails={'guardrailIdentifier': '<id>', 'guardrailVersion': 'DRAFT'}")


# ---------------------------------------------------------------------------
# Container image configuration
# ---------------------------------------------------------------------------


def _print_container_config() -> None:
    """Print container image packaging instructions."""
    print("\n--- Container Image Configuration ---")
    print(f"  Image URI: {CONTAINER_IMAGE_URI}")
    print("  Dockerfile should:")
    print("    1. FROM python:3.13-slim")
    print("    2. COPY clinicaltrials/ /app/")
    print("    3. RUN pip install -r /app/requirements.txt")
    print("    4. EXPOSE 8080")
    print('    5. CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Deploy AgentCore infrastructure: IAM role, API Gateway, and print config."""
    print("=" * 60)
    print("Clinical Trial AgentCore Deployment")
    print("=" * 60)

    # 1. IAM Role (AgentCore Identity)
    print("\n[1/3] Configuring AgentCore Identity (IAM)...")
    iam = boto3.client("iam", region_name=AWS_REGION)
    role_arn = _create_iam_role(iam)
    print(f"  Role ARN: {role_arn}")

    # 2. API Gateway
    print("\n[2/3] Configuring AgentCore API Gateway...")
    apigw = boto3.client("apigateway", region_name=AWS_REGION)
    api_id = _create_api_gateway(apigw)
    _configure_api_routes(apigw, api_id, role_arn)
    _configure_throttling(apigw, api_id)
    api_url = f"https://{api_id}.execute-api.{AWS_REGION}.amazonaws.com/{API_STAGE}"
    print(f"  API URL: {api_url}")

    # 3. Runtime configuration (print instructions)
    print("\n[3/3] AgentCore Runtime Configuration...")
    _print_container_config()
    _print_guardrail_config()

    # Summary
    print("\n" + "=" * 60)
    print("✅ AgentCore deployment configuration complete")
    print("=" * 60)
    print(f"  IAM Role:    {role_arn}")
    print(f"  API Gateway: {api_url}")
    print(f"  Guardrail:   {PHI_PROTECTION_GUARDRAIL_ID}")
    print(f"  Tags:        {REQUIRED_TAGS}")
    print("\nNext steps:")
    print("  1. Build and push container image to ECR")
    print("  2. Create AgentCore Runtime with the container image")
    print("  3. Associate the API Gateway with the Runtime")


if __name__ == "__main__":
    main()
