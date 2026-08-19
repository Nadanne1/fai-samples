#!/usr/bin/env python3
"""Create (or look up) the Bedrock AgentCore runtime for clinical trial screening.

The runtime wraps the screening-agent container image. Idempotent — if a runtime
named 'clinical-trial-screening' already exists, returns its ARN without creating
a duplicate.

Usage:
    python infra/create_agentcore_runtime.py [--region REGION] [--account ACCOUNT]

Prints the runtime ARN to stdout so deploy.sh can capture it.

Prerequisites:
    - ECR repo must exist and contain at least one image (run create_ecr_repos.py + docker push first)
    - IAM execution role must exist (run agentcore_deploy.py first, or create it manually)
"""

import argparse
import sys

import boto3
from botocore.exceptions import ClientError

RUNTIME_NAME = "clinical-trial-screening"


def get_account_id(region: str) -> str:
    return boto3.client("sts", region_name=region).get_caller_identity()["Account"]


def get_existing_runtime_arn(client, name: str) -> str | None:
    """Return the ARN of an existing runtime with this name, or None."""
    paginator = client.get_paginator("list_agent_runtimes")
    for page in paginator.paginate():
        for r in page.get("agentRuntimes", []):
            if r.get("agentRuntimeName") == name:
                return r["agentRuntimeArn"]
    return None


def get_execution_role_arn(region: str, account: str) -> str:
    """Look up the AgentCore execution role created by agentcore_deploy.py."""
    role_name = f"{RUNTIME_NAME}-agentcore-role"
    iam = boto3.client("iam", region_name=region)
    try:
        return iam.get_role(RoleName=role_name)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        print(f"ERROR: IAM role '{role_name}' not found. Run infra/agentcore_deploy.py first.", file=sys.stderr)
        sys.exit(1)


def create_runtime(client, region: str, account: str) -> str:
    """Create the AgentCore runtime and return its ARN."""
    image_uri = (
        f"{account}.dkr.ecr.{region}.amazonaws.com"
        f"/{RUNTIME_NAME}/screening-agent:latest"
    )
    role_arn = get_execution_role_arn(region, account)

    print(f"  Image URI:  {image_uri}", file=sys.stderr)
    print(f"  Role ARN:   {role_arn}", file=sys.stderr)

    response = client.create_agent_runtime(
        agentRuntimeName=RUNTIME_NAME,
        description="Clinical trial patient screening and monitoring LangGraph agent",
        agentRuntimeArtifact={
            "containerConfiguration": {
                "containerUri": image_uri,
            }
        },
        roleArn=role_arn,
        networkConfiguration={"networkMode": "PUBLIC"},
    )
    return response["agentRuntimeArn"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Create AgentCore runtime for clinical-trial-screening")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--account", default="")
    args = parser.parse_args()

    region = args.region
    account = args.account or get_account_id(region)

    client = boto3.client("bedrock-agentcore", region_name=region)

    arn = get_existing_runtime_arn(client, RUNTIME_NAME)
    if arn:
        print(f"Already exists: {RUNTIME_NAME} — {arn}", file=sys.stderr)
    else:
        print(f"Creating AgentCore runtime: {RUNTIME_NAME}...", file=sys.stderr)
        try:
            arn = create_runtime(client, region, account)
            print(f"Created: {RUNTIME_NAME} — {arn}", file=sys.stderr)
        except ClientError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

    # Print ARN to stdout for shell capture
    print(arn)


if __name__ == "__main__":
    main()
