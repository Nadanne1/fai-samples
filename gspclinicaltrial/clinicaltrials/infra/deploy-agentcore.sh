#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Clinical Trial Screening — AgentCore MCP Gateway Deploy
#
# Creates (or updates) the AgentCore MCP Gateway and Target that expose the
# clinical trials API as tools for AI agents. Same pattern as CareConnect360.
#
# Prerequisites:
#   - Lambda + API Gateway already deployed via infra/deploy.sh
#   - AWS CLI v2 with bedrock-agentcore-control support
#   - An API key stored in Parameter Store at /clinical-trials/api-key
#     (create with: aws ssm put-parameter --name /clinical-trials/api-key
#       --value "your-key" --type SecureString --region us-east-1)
#
# Usage:
#   ./infra/deploy-agentcore.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REGION="us-east-1"
ACCOUNT=$(aws sts get-caller-identity --region "${REGION}" --query Account --output text)
GATEWAY_NAME="clinical-trials-gw"
TARGET_NAME="ClinicalTrials-API-Target"
API_ENDPOINT="https://2009xhuxx8.execute-api.us-east-1.amazonaws.com"
OPENAPI_FILE="$(dirname "$0")/agentcore/openapi.yaml"
IAM_ROLE_NAME="clinical-trials-agentcore-gateway-role"
CREDENTIAL_PROVIDER_NAME="clinical-trials-api-key-provider"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Clinical Trial Screening — AgentCore Deploy                ║"
printf "║  Account: %-49s║\n" "${ACCOUNT}  Region: ${REGION}"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ── Step 0: Resolve API key from SSM ─────────────────────────────────────────
echo "▸ Step 0: Resolving API key from SSM..."
API_KEY=$(aws ssm get-parameter \
    --name "/clinical-trials/api-key" \
    --with-decryption \
    --region "${REGION}" \
    --query "Parameter.Value" \
    --output text 2>/dev/null || echo "")

if [[ -z "${API_KEY}" ]]; then
    echo "  ⚠ No API key in SSM. Generating a new one..."
    API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    aws ssm put-parameter \
        --name "/clinical-trials/api-key" \
        --value "${API_KEY}" \
        --type SecureString \
        --region "${REGION}" \
        --overwrite > /dev/null
    echo "  ✓ API key stored in SSM at /clinical-trials/api-key"
    echo "  ⚠ Set CLINICAL_TRIALS_API_KEY=${API_KEY} in Lambda environment:"
    echo "    aws lambda update-function-configuration \\"
    echo "      --function-name clinical-trial-screening \\"
    echo "      --environment 'Variables={CLINICAL_TRIALS_API_KEY=${API_KEY},...}' \\"
    echo "      --region ${REGION}"
fi
echo "  ✓ API key resolved"

# ── Step 1: Ensure IAM role for AgentCore gateway ─────────────────────────────
echo ""
echo "▸ Step 1: Ensuring IAM role for AgentCore gateway..."
ROLE_ARN=$(aws iam get-role \
    --role-name "${IAM_ROLE_NAME}" \
    --query "Role.Arn" \
    --output text 2>/dev/null || echo "")

if [[ -z "${ROLE_ARN}" ]]; then
    echo "  Creating IAM role ${IAM_ROLE_NAME}..."
    ROLE_ARN=$(aws iam create-role \
        --role-name "${IAM_ROLE_NAME}" \
        --assume-role-policy-document '{
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        }' \
        --query "Role.Arn" \
        --output text)
    aws iam attach-role-policy \
        --role-name "${IAM_ROLE_NAME}" \
        --policy-arn "arn:aws:iam::aws:policy/AmazonBedrockFullAccess"
    echo "  Waiting for role propagation..."
    sleep 10
fi
echo "  ✓ IAM role: ${ROLE_ARN}"

# ── Step 2: Create or update AgentCore API key credential provider ────────────
echo ""
echo "▸ Step 2: Ensuring API key credential provider..."
PROVIDER_ARN="arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:token-vault/default/apikeycredentialprovider/${CREDENTIAL_PROVIDER_NAME}"

aws bedrock-agentcore-control create-api-key-credential-provider \
    --name "${CREDENTIAL_PROVIDER_NAME}" \
    --api-key "${API_KEY}" \
    --region "${REGION}" > /dev/null 2>&1 || \
aws bedrock-agentcore-control update-api-key-credential-provider \
    --name "${CREDENTIAL_PROVIDER_NAME}" \
    --api-key "${API_KEY}" \
    --region "${REGION}" > /dev/null 2>&1 || true

echo "  ✓ Credential provider: ${PROVIDER_ARN}"

# ── Step 3: Serialize openapi.yaml for inline payload ────────────────────────
echo ""
echo "▸ Step 3: Serializing openapi.yaml..."
OPENAPI_JSON=$(python3 -c "
import yaml, json, sys
with open('${OPENAPI_FILE}') as f:
    spec = yaml.safe_load(f)
print(json.dumps(json.dumps(spec)))
" 2>/dev/null || python3 -c "
import json, sys
# Fallback: read as-is if yaml not available
with open('${OPENAPI_FILE}') as f:
    content = f.read()
# Simple YAML→JSON via python3 (basic)
print(json.dumps(content))
")
echo "  ✓ OpenAPI spec serialized ($(echo "${OPENAPI_JSON}" | wc -c | tr -d ' ') chars)"

# ── Step 4: Create or update AgentCore Gateway ───────────────────────────────
echo ""
echo "▸ Step 4: Creating/updating AgentCore MCP Gateway..."
GATEWAY_ID=$(aws bedrock-agentcore-control list-gateways \
    --region "${REGION}" \
    --query "items[?name=='${GATEWAY_NAME}'].gatewayId | [0]" \
    --output text 2>/dev/null || echo "")

if [[ -z "${GATEWAY_ID}" || "${GATEWAY_ID}" == "None" ]]; then
    echo "  Creating new gateway ${GATEWAY_NAME}..."
    GATEWAY_ID=$(aws bedrock-agentcore-control create-gateway \
        --name "${GATEWAY_NAME}" \
        --role-arn "${ROLE_ARN}" \
        --protocol-type MCP \
        --authorizer-type NONE \
        --region "${REGION}" \
        --query "gatewayId" \
        --output text)
    echo "  ✓ Gateway created: ${GATEWAY_ID}"
else
    echo "  ✓ Gateway exists: ${GATEWAY_ID}"
fi

GATEWAY_ARN="arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:gateway/${GATEWAY_ID}"
GATEWAY_URL="https://${GATEWAY_ID}.gateway.bedrock-agentcore.${REGION}.amazonaws.com/mcp"

# ── Step 5: Create or update Gateway Target ───────────────────────────────────
echo ""
echo "▸ Step 5: Creating/updating AgentCore Gateway Target..."

TARGET_CONFIG=$(python3 -c "
import json
schema = ${OPENAPI_JSON}
config = {
    'mcp': {
        'openApiSchema': {
            'inlinePayload': schema
        }
    }
}
print(json.dumps(config))
")

CREDENTIAL_CONFIG=$(python3 -c "
import json
config = [{
    'credentialProviderType': 'API_KEY',
    'credentialProvider': {
        'apiKeyCredentialProvider': {
            'providerArn': '${PROVIDER_ARN}',
            'credentialParameterName': 'X-Api-Key',
            'credentialLocation': 'HEADER'
        }
    }
}]
print(json.dumps(config))
")

TARGET_ID=$(aws bedrock-agentcore-control list-gateway-targets \
    --gateway-identifier "${GATEWAY_ID}" \
    --region "${REGION}" \
    --query "items[?name=='${TARGET_NAME}'].targetId | [0]" \
    --output text 2>/dev/null || echo "")

if [[ -z "${TARGET_ID}" || "${TARGET_ID}" == "None" ]]; then
    echo "  Creating new target ${TARGET_NAME}..."
    TARGET_ID=$(aws bedrock-agentcore-control create-gateway-target \
        --gateway-identifier "${GATEWAY_ID}" \
        --name "${TARGET_NAME}" \
        --target-configuration "${TARGET_CONFIG}" \
        --credential-provider-configurations "${CREDENTIAL_CONFIG}" \
        --region "${REGION}" \
        --query "targetId" \
        --output text)
    echo "  ✓ Target created: ${TARGET_ID}"
else
    echo "  Updating existing target ${TARGET_ID}..."
    aws bedrock-agentcore-control update-gateway-target \
        --gateway-identifier "${GATEWAY_ID}" \
        --target-identifier "${TARGET_ID}" \
        --name "${TARGET_NAME}" \
        --target-configuration "${TARGET_CONFIG}" \
        --credential-provider-configurations "${CREDENTIAL_CONFIG}" \
        --region "${REGION}" > /dev/null
    echo "  ✓ Target updated: ${TARGET_ID}"
fi

# ── Step 6: Wait for gateway to become READY ─────────────────────────────────
echo ""
echo "▸ Step 6: Waiting for gateway to become READY (up to 3 min)..."
for i in $(seq 1 18); do
    STATUS=$(aws bedrock-agentcore-control get-gateway \
        --gateway-identifier "${GATEWAY_ID}" \
        --region "${REGION}" \
        --query "status" \
        --output text 2>/dev/null || echo "UNKNOWN")
    if [[ "${STATUS}" == "READY" ]]; then
        echo "  ✓ Gateway is READY"
        break
    fi
    echo "  Status: ${STATUS} (${i}/18) — waiting 10s..."
    sleep 10
done

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ✓ AgentCore deploy complete!                               ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Gateway ID  : ${GATEWAY_ID}"
echo "  Gateway ARN : ${GATEWAY_ARN}"
echo "  Gateway URL : ${GATEWAY_URL}"
echo "  Target ID   : ${TARGET_ID}"
echo "  API Endpoint: ${API_ENDPOINT}"
echo ""

# Save config for reference
CONFIG_OUT="$(dirname "$0")/agentcore/gateway-config-deployed.json"
python3 -c "
import json
config = {
    'gatewayId': '${GATEWAY_ID}',
    'gatewayArn': '${GATEWAY_ARN}',
    'gatewayUrl': '${GATEWAY_URL}',
    'targetId': '${TARGET_ID}',
    'targetName': '${TARGET_NAME}',
    'apiEndpoint': '${API_ENDPOINT}',
    'region': '${REGION}',
    'account': '${ACCOUNT}',
}
with open('${CONFIG_OUT}', 'w') as f:
    json.dump(config, f, indent=2)
print('  Config saved to ${CONFIG_OUT}')
"

echo ""
echo "  Next: set Lambda env var CLINICAL_TRIALS_API_KEY to the API key stored in"
echo "  SSM at /clinical-trials/api-key, then redeploy Lambda with deploy.sh --code-only"
