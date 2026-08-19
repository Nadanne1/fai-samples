# Clinical Trials CDK Infrastructure

CDK stack that provisions all AWS resources for the Clinical Trial Screening & Monitoring System.

## Resources Provisioned

| Resource | Name / ID | Purpose |
|---|---|---|
| KMS Key | `alias/clinical-trial-screening/phi` | PHI encryption at rest |
| DynamoDB | `TrialAuditLog` | 21 CFR Part 11 audit trail (KMS-encrypted, TTL, PITR) |
| SQS | `TrialScreeningIntakeQueue` + DLQ | Patient screening requests |
| SQS | `TrialEscalationQueue` + DLQ | Borderline cases and safety signals |
| SQS | `AuditDeadLetterQueue` | Failed audit writes |
| ECR | `clinical-trial-screening` | Container image repository |
| IAM Role | `clinical-trial-screening-agentcore-role` | AgentCore Runtime execution role |
| API Gateway | `clinical-trial-screening-api` | REST API (IAM auth, throttled) |

Note: `TrialProtocolConfig` has been migrated from DynamoDB to HealthLake `ResearchStudy` resources, so no DynamoDB table is created for it. HealthLake datastores are provisioned separately (not yet supported as a CDK L2 construct).

## Prerequisites

```bash
npm install -g aws-cdk
```

## Configuration

The stack is environment-driven — no account, region, VPC, or datastore IDs are
hardcoded. Copy `7-clinical-trials/.env.example` to `.env` and set values, or pass
them via CDK context. Resolution order is **env var → CDK context → default**.

| Value | Env var | CDK context key |
|---|---|---|
| Account | `CDK_DEFAULT_ACCOUNT` / `AWS_ACCOUNT_ID` | (from AWS profile) |
| Region | `CDK_DEFAULT_REGION` / `AWS_REGION` | (from AWS profile) |
| HealthLake datastore | `HEALTHLAKE_DATASTORE_ID` | `healthlake_datastore_id` |
| VPC id | `VPC_ID` | `vpc_id` |
| VPC AZs (CSV) | `VPC_AZS` | `vpc_azs` |
| Public subnet IDs (CSV) | `VPC_PUBLIC_SUBNET_IDS` | `vpc_public_subnet_ids` |

## Deploy

```bash
cd 7-clinical-trials/infra/cdk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Provide config via env (or -c context flags shown below)
export AWS_ACCOUNT_ID=123456789012
export AWS_REGION=us-east-1
export HEALTHLAKE_DATASTORE_ID=<your-datastore-id>
export VPC_ID=<your-vpc-id>
export VPC_PUBLIC_SUBNET_IDS=<subnet-a>,<subnet-b>
export VPC_AZS=us-east-1a,us-east-1b

cdk synth          # preview the CloudFormation template
cdk diff           # compare with deployed stack
cdk deploy         # deploy to AWS

# Equivalent using CDK context instead of env vars:
# cdk deploy -c healthlake_datastore_id=... -c vpc_id=... \
#            -c vpc_public_subnet_ids=subnet-a,subnet-b -c vpc_azs=us-east-1a,us-east-1b
```

## Post-Deploy Steps

1. **HealthLake datastore** — Provision separately via console or CLI (CDK L2
   construct not available). Set its ID in `HEALTHLAKE_DATASTORE_ID`.

2. **Build and push container image:**
   ```bash
   cd 7-clinical-trials
   ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
   REGION=${AWS_REGION:-us-east-1}
   ECR="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
   aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR"
   docker build --platform linux/arm64 -t clinical-trial-screening .
   docker tag clinical-trial-screening:latest "$ECR/clinical-trial-screening:latest"
   docker push "$ECR/clinical-trial-screening:latest"
   ```

3. **Create AgentCore Runtime** — Associate the container image with the AgentCore Runtime and link the API Gateway.

## Destroy

```bash
cdk destroy
```

Note: KMS key, DynamoDB table, and ECR repo use `RETAIN` removal policy for data safety. Delete them manually if needed.
