#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Clinical Trial Screening — Lambda Deploy Script
#
# Usage:
#   ./infra/deploy.sh              # Full deploy (infra + code + frontend)
#   ./infra/deploy.sh --code-only  # Lambda zip + frontend only (stack exists)
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

if [[ -f .env ]]; then set -a; source .env; set +a; fi

REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --region "${REGION}" --query Account --output text)}"
export AWS_ACCOUNT_ID="${ACCOUNT}"
APP_NAME="clinical-trial-screening"

: "${HEALTHLAKE_DATASTORE_ID:?Set HEALTHLAKE_DATASTORE_ID in .env}"

# ── Auto-resolve guardrail ────────────────────────────────────────────────────
if [[ -z "${PHI_PROTECTION_GUARDRAIL_ID:-}" ]]; then
  echo "▸ Creating/looking up PHI guardrail..."
  _OUT=$(python3 infra/create_guardrail.py --region "${REGION}")
  PHI_PROTECTION_GUARDRAIL_ID=$(echo "${_OUT}" | awk '{print $1}')
  PHI_GUARDRAIL_VERSION=$(echo "${_OUT}" | awk '{print $2}')
  echo "  Guardrail: ${PHI_PROTECTION_GUARDRAIL_ID}  version: ${PHI_GUARDRAIL_VERSION}"
fi
PHI_GUARDRAIL_VERSION="${PHI_GUARDRAIL_VERSION:-DRAFT}"
export PHI_PROTECTION_GUARDRAIL_ID PHI_GUARDRAIL_VERSION

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Clinical Trial Screening — Lambda Deploy                   ║"
printf "║  Account: %-49s║\n" "${ACCOUNT}  Region: ${REGION}"
echo "╚══════════════════════════════════════════════════════════════╝"

# ── Pre-deploy: E2E tests ─────────────────────────────────────────────────────
if [[ "${SKIP_E2E:-}" != "1" ]] && command -v python3 &>/dev/null; then
  echo ""
  echo "▸ Pre-deploy: Running E2E tests (set SKIP_E2E=1 to skip)..."
  if python3 infra/e2e_test.py --production; then
    echo "  ✓ All E2E tests passed — proceeding with deploy"
  else
    echo ""
    echo "  ✗ E2E tests FAILED — deploy blocked"
    echo "    Fix the failures above, or set SKIP_E2E=1 to force deploy."
    exit 1
  fi
fi

# ── Step 0: Prerequisites ─────────────────────────────────────────────────────
echo ""
echo "▸ Step 0: Ensuring prerequisite AWS resources exist..."
python3 infra/create_dynamodb_tables.py
python3 infra/create_sqs_queues.py

# ── Build Lambda zip (shared between full deploy and code-only) ───────────────
_build_lambda_zip() {
  echo ""
  echo "▸ Building Lambda package (excluding boto3/botocore — already in Lambda runtime)..."
  rm -rf lambda_pkg && mkdir lambda_pkg

  # Install for Lambda runtime (linux x86_64) — compiled extensions must match Lambda OS.
  # Two-pass install required: langchain-aws has a numpy dependency marker
  # (python_version < "3.12") that pip's cross-compile resolver incorrectly evaluates
  # when --python-version 3.13 is set, blocking the whole install. Pass 1 installs
  # everything except langchain-aws (gets correct binary wheels). Pass 2 installs
  # langchain-aws alone with --no-deps (numpy is not needed at runtime on Python 3.13).
  grep -v "^langchain-aws" requirements.txt > /tmp/_req_pass1.txt
  pip install -q -r /tmp/_req_pass1.txt -t lambda_pkg/ \
    --platform manylinux2014_x86_64 \
    --python-version 3.13 \
    --only-binary=:all: \
    --implementation cp \
    --upgrade
  pip install -q "$(grep '^langchain-aws' requirements.txt)" -t lambda_pkg/ \
    --platform manylinux2014_x86_64 \
    --python-version 3.13 \
    --only-binary=:all: \
    --implementation cp \
    --no-deps
  rm -f /tmp/_req_pass1.txt

  # Remove boto3/botocore — pre-installed in Lambda Python 3.13 runtime, saves ~40 MB
  rm -rf lambda_pkg/boto3* lambda_pkg/botocore* lambda_pkg/s3transfer*

  # Copy only the source files needed at runtime
  rsync -aq \
    --exclude='lambda_pkg' --exclude='lambda_pkg.zip' \
    --exclude='infra' --exclude='dashboard' \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' \
    --exclude='tests' --exclude='.env' --exclude='.env.example' \
    --exclude='amplifyapp' --exclude='static' --exclude='ui' \
    --exclude='README.md' --exclude='.git' --exclude='*.zip' \
    ./ lambda_pkg/

  cd lambda_pkg && zip -q -r ../lambda_pkg.zip . && cd ..
  rm -rf lambda_pkg
  echo "  ✓ lambda_pkg.zip built ($(du -sh lambda_pkg.zip | cut -f1))"

  # Warn if approaching Lambda 250 MB unzipped limit
  ZIPPED_MB=$(du -sm lambda_pkg.zip | cut -f1)
  if [[ "${ZIPPED_MB}" -gt 80 ]]; then
    echo "  ⚠ Zip is ${ZIPPED_MB} MB — unzipped may approach Lambda 250 MB limit"
  fi
}

# ── Full deploy: build zip + CDK ──────────────────────────────────────────────
if [[ "${1:-}" != "--code-only" ]]; then
  _build_lambda_zip

  echo ""
  echo "▸ Step 1: Deploying CDK stack..."
  pushd infra/cdk > /dev/null
  pip install -q -r requirements.txt
  export AWS_REGION="${REGION}"
  JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 npx cdk deploy --require-approval never \
    -c healthlake_datastore_id="${HEALTHLAKE_DATASTORE_ID}" \
    -c phi_guardrail_id="${PHI_PROTECTION_GUARDRAIL_ID}" \
    -c phi_guardrail_version="${PHI_GUARDRAIL_VERSION}" \
    -c screening_runtime_arn="${SCREENING_RUNTIME_ARN:-}" \
    --tags domain=hcls --tags use-case=clinical-trial --tags owner=gokhul
  popd > /dev/null
  echo "  ✓ CDK stack deployed"
fi

# ── Code-only: update existing Lambda via S3 (zip may exceed 50MB direct limit) ──
if [[ "${1:-}" == "--code-only" ]]; then
  _build_lambda_zip
  echo "▸ Updating Lambda function code via S3..."
  DEPLOY_BUCKET=$(aws cloudformation describe-stacks --stack-name ClinicalTrialsStack \
    --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='FrontendBucketName'].OutputValue" \
    --output text 2>/dev/null || echo "")
  if [[ -z "${DEPLOY_BUCKET}" ]]; then
    DEPLOY_BUCKET="clinical-trial-screening-frontend-${ACCOUNT}"
  fi
  S3_KEY="deploys/lambda_pkg.zip"
  aws s3 cp lambda_pkg.zip "s3://${DEPLOY_BUCKET}/${S3_KEY}" --region "${REGION}" > /dev/null
  aws lambda update-function-code \
    --function-name "${APP_NAME}" \
    --s3-bucket "${DEPLOY_BUCKET}" \
    --s3-key "${S3_KEY}" \
    --region "${REGION}" > /dev/null
  echo "  ✓ Lambda updated via s3://${DEPLOY_BUCKET}/${S3_KEY}"
fi

# ── Step 2: Deploy React dashboard to S3 ─────────────────────────────────────
echo ""
echo "▸ Step 2: Deploying React dashboard..."
BUCKET=$(aws cloudformation describe-stacks --stack-name ClinicalTrialsStack \
  --region "${REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='FrontendBucketName'].OutputValue" \
  --output text)
pushd dashboard > /dev/null
npm ci --silent
npm run build --silent
aws s3 sync dist/ "s3://${BUCKET}/" --delete --region "${REGION}"
popd > /dev/null
echo "  ✓ Frontend deployed to s3://${BUCKET}"

# ── Step 3: CloudFront invalidation ──────────────────────────────────────────
echo ""
echo "▸ Step 3: Invalidating CloudFront cache..."
CF_DIST_ID=$(aws cloudformation describe-stacks --stack-name ClinicalTrialsStack \
  --region "${REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDistributionId'].OutputValue" \
  --output text)
if [[ -n "${CF_DIST_ID}" ]]; then
  aws cloudfront create-invalidation \
    --distribution-id "${CF_DIST_ID}" --paths "/*" --region "${REGION}" > /dev/null
  echo "  ✓ Cache invalidated"
else
  echo "  ⚠ No CloudFront distribution ID — skipping invalidation"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ✓ Deploy complete!                                         ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
aws cloudformation describe-stacks --stack-name ClinicalTrialsStack \
  --region "${REGION}" \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" --output table 2>/dev/null || true
