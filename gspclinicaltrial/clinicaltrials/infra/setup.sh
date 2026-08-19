#!/usr/bin/env bash
# setup.sh — One-time environment setup for Clinical Trial Screening
#
# Usage:
#   cd clinicaltrials
#   cp .env.example .env        # fill in required values first
#   set -a; source .env; set +a
#   bash infra/setup.sh
#
# What it does (in order):
#   1. Python virtualenv + dependencies
#   2. DynamoDB tables (TrialAuditLog, TrialProtocolConfig, TrialScreeningRules)
#   3. SQS queues (intake, escalation, audit DLQ)
#   4. Bedrock guardrail (PHI protection)
#   5. Seed trial protocols into TrialProtocolConfig
#   6. Seed synthetic analytics records into TrialAuditLog
#   7. (Optional) Migrate patients into HealthLake — pass --with-patients flag
#   8. (Optional) Deploy AgentCore MCP gateway — pass --with-agentcore flag

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"

WITH_PATIENTS=false
WITH_AGENTCORE=false

for arg in "$@"; do
  case $arg in
    --with-patients)  WITH_PATIENTS=true ;;
    --with-agentcore) WITH_AGENTCORE=true ;;
  esac
done

echo "╔══════════════════════════════════════════════════════╗"
echo "║  Clinical Trial Screening — Environment Setup        ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── Step 1: Python environment ───────────────────────────────────────────────
echo "▸ Step 1: Python environment…"
cd "$ROOT"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt
echo "  ✓ Python env ready"

# ── Step 2: DynamoDB tables ──────────────────────────────────────────────────
echo "▸ Step 2: DynamoDB tables…"
python3 infra/create_dynamodb_tables.py
# TrialScreeningRules is lightweight — create via AWS CLI if not exists
aws dynamodb describe-table --table-name TrialScreeningRules \
  --region "${AWS_REGION:-us-east-1}" > /dev/null 2>&1 || \
aws dynamodb create-table \
  --table-name TrialScreeningRules \
  --attribute-definitions AttributeName=id,AttributeType=S \
  --key-schema AttributeName=id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region "${AWS_REGION:-us-east-1}" > /dev/null
echo "  ✓ DynamoDB tables ready"

# ── Step 3: SQS queues ───────────────────────────────────────────────────────
echo "▸ Step 3: SQS queues…"
python3 infra/create_sqs_queues.py
echo "  ✓ SQS queues ready"

# ── Step 4: Bedrock guardrail ────────────────────────────────────────────────
echo "▸ Step 4: Bedrock guardrail…"
python3 infra/create_guardrail.py
echo "  ✓ Guardrail ready"

# ── Step 5: Seed trial protocols ─────────────────────────────────────────────
echo "▸ Step 5: Seeding trial protocols…"
python3 infra/seed_trials.py
echo "  ✓ Trial protocols seeded"

# ── Step 6: Seed analytics ───────────────────────────────────────────────────
echo "▸ Step 6: Seeding analytics…"
python3 infra/seed_analytics.py
echo "  ✓ Analytics seeded"

# ── Step 7: Load patients into HealthLake (optional) ─────────────────────────
if [ "$WITH_PATIENTS" = true ]; then
  echo "▸ Step 7: Importing sample FHIR data into HealthLake…"
  python3 infra/import_sampledata.py
  echo "  ✓ Sample data imported"
  echo "  (run  python3 infra/refresh_insights.py  to generate AI insights)"
else
  echo "  (skipping HealthLake data import — pass --with-patients to include)"
fi

# ── Step 8: AgentCore MCP gateway (optional) ─────────────────────────────────
if [ "$WITH_AGENTCORE" = true ]; then
  echo "▸ Step 8: Deploying AgentCore MCP gateway…"
  bash infra/deploy-agentcore.sh
  echo "  ✓ AgentCore deployed"
else
  echo "  (skipping AgentCore — pass --with-agentcore to include)"
fi

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  ✓ Setup complete!                                   ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  Local dev:                                          ║"
echo "║    set -a; source .env; set +a                       ║"
echo "║    uvicorn app:app --reload --port 8000              ║"
echo "║                                                      ║"
echo "║  Frontend:                                           ║"
echo "║    cd dashboard && npm install && npm run dev        ║"
echo "║                                                      ║"
echo "║  Deploy to AWS:                                      ║"
echo "║    bash infra/deploy.sh --code-only                  ║"
echo "╚══════════════════════════════════════════════════════╝"
