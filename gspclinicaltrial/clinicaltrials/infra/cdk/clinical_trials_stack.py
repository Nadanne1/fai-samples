"""CDK stack for Clinical Trial Screening & Monitoring — Lambda + API Gateway.

Provisions:
  - KMS key (PHI encryption)
  - DynamoDB TrialAuditLog (imported)
  - SQS queues (imported)
  - Lambda function (screening + monitoring + trial config API)
  - API Gateway HTTP API
  - S3 + CloudFront for React dashboard
  - IAM role (least privilege)
  - WAF on CloudFront
"""

import os

from aws_cdk import (
    Duration,
    Lazy,
    RemovalPolicy,
    Stack,
    Tags,
    CfnOutput,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as integrations,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sqs as sqs,
    aws_wafv2 as wafv2,
)
from constructs import Construct

REQUIRED_TAGS = {
    "domain": "hcls",
    "use-case": "clinical-trial",
    "owner": "gokhul",
    "project": "clinical-trials",
    "partner": "langchain",
    "compliance": "hipaa-21cfr11",
}

APP_NAME = "clinical-trial-screening"


class ClinicalTrialsStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        healthlake_datastore_id: str = "",
        phi_guardrail_id: str = "",
        phi_guardrail_version: str = "DRAFT",
        screening_runtime_arn: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.healthlake_datastore_id = healthlake_datastore_id
        self.phi_guardrail_id = phi_guardrail_id
        self.phi_guardrail_version = phi_guardrail_version
        self.screening_runtime_arn = screening_runtime_arn

        for key, value in REQUIRED_TAGS.items():
            Tags.of(self).add(key, value)

        self.phi_key = self._create_kms_key()
        self.audit_table = self._create_audit_table()
        self._create_sqs_queues()
        self.lambda_role = self._create_lambda_role()
        self.fn = self._create_lambda()
        self.api = self._create_api_gateway()
        self.frontend_bucket = self._create_frontend_bucket()
        self.distribution = self._create_cloudfront()
        self._create_outputs()

    def _create_kms_key(self) -> kms.Key:
        return kms.Key(
            self, "PhiEncryptionKey",
            alias=f"alias/{APP_NAME}/phi",
            description="Encrypts PHI data in clinical trial audit records",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

    def _create_audit_table(self) -> dynamodb.ITable:
        return dynamodb.Table.from_table_name(self, "TrialAuditLog", "TrialAuditLog")

    def _create_sqs_queues(self) -> None:
        # Import existing queues — created by create_sqs_queues.py
        self.intake_queue = sqs.Queue.from_queue_arn(
            self, "IntakeQueue",
            f"arn:aws:sqs:{self.region}:{self.account}:TrialScreeningIntakeQueue",
        )
        self.escalation_queue = sqs.Queue.from_queue_arn(
            self, "EscalationQueue",
            f"arn:aws:sqs:{self.region}:{self.account}:TrialEscalationQueue",
        )

    def _create_lambda_role(self) -> iam.Role:
        role = iam.Role(
            self, "LambdaRole",
            role_name=f"{APP_NAME}-lambda-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            ],
        )

        hl_resource = (
            f"arn:aws:healthlake:{self.region}:{self.account}:datastore/fhir/{self.healthlake_datastore_id}"
            if self.healthlake_datastore_id
            else f"arn:aws:healthlake:{self.region}:{self.account}:datastore/fhir/*"
        )
        role.add_to_policy(iam.PolicyStatement(
            actions=["healthlake:ReadResource", "healthlake:CreateResource",
                     "healthlake:UpdateResource", "healthlake:SearchWithGet", "healthlake:SearchWithPost"],
            resources=[hl_resource],
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:Scan"],
            resources=[
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/TrialAuditLog",
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/TrialAuditLog/index/*",
            ],
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
            resources=[
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/TrialProtocolConfig",
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/TrialProtocolConfig/index/*",
            ],
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
            resources=[
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/TrialScreeningRules",
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/TrialScreeningRules/index/*",
            ],
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:DeleteItem"],
            resources=[
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/TrialDevOpsTests",
                f"arn:aws:dynamodb:{self.region}:{self.account}:table/TrialDevOpsTests/index/*",
            ],
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage",
                     "sqs:GetQueueUrl", "sqs:GetQueueAttributes"],
            resources=[
                f"arn:aws:sqs:{self.region}:{self.account}:Trial*",
                f"arn:aws:sqs:{self.region}:{self.account}:AuditDeadLetterQueue",
            ],
        ))
        self.phi_key.grant_encrypt_decrypt(role)
        role.add_to_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:clinical-trials/*"],
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream", "bedrock:ApplyGuardrail"],
            resources=[
                f"arn:aws:bedrock:{self.region}::foundation-model/anthropic.claude-3-haiku-*",
                f"arn:aws:bedrock:{self.region}::foundation-model/anthropic.claude-3-5-sonnet-*",
                f"arn:aws:bedrock:{self.region}::foundation-model/us.anthropic.claude-haiku-*",
                f"arn:aws:bedrock:{self.region}::foundation-model/us.anthropic.claude-3-5-sonnet-*",
                f"arn:aws:bedrock:{self.region}:{self.account}:guardrail/*",
            ],
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/*"],
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=[
                "cognito-idp:ListUsers",
                "cognito-idp:ListGroups",
                "cognito-idp:ListUsersInGroup",
                "cognito-idp:AdminListGroupsForUser",
                "cognito-idp:AdminCreateUser",
                "cognito-idp:AdminDeleteUser",
                "cognito-idp:AdminAddUserToGroup",
                "cognito-idp:AdminRemoveUserFromGroup",
                "cognito-idp:AdminSetUserPassword",
            ],
            resources=[f"arn:aws:cognito-idp:{self.region}:{self.account}:userpool/us-east-1_ZyRil52W0"],
        ))
        return role

    def _create_lambda(self) -> lambda_.Function:
        fn = lambda_.Function(
            self, "ScreeningFn",
            function_name=APP_NAME,
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="app.handler",
            code=lambda_.Code.from_asset(
                os.path.join(os.path.dirname(__file__), "../../lambda_pkg.zip")
            ),
            role=self.lambda_role,
            timeout=Duration.minutes(15),
            memory_size=1024,
            environment={
                "AWS_ACCOUNT_ID": self.account,
                "HEALTHLAKE_DATASTORE_ID": self.healthlake_datastore_id,
                "AUDIT_LOG_TABLE": "TrialAuditLog",
                "PROTOCOL_CONFIG_TABLE": "TrialProtocolConfig",
                "SCREENING_INTAKE_QUEUE": "TrialScreeningIntakeQueue",
                "ESCALATION_QUEUE": "TrialEscalationQueue",
                "PHI_PROTECTION_GUARDRAIL_ID": self.phi_guardrail_id,
                "PHI_GUARDRAIL_VERSION": self.phi_guardrail_version,
                "SCREENING_RUNTIME_ARN": self.screening_runtime_arn,
                "COGNITO_USER_POOL_ID": "us-east-1_ZyRil52W0",
            },
            log_retention=logs.RetentionDays.ONE_MONTH,
            tracing=lambda_.Tracing.ACTIVE,
        )
        Tags.of(fn).add("domain", "hcls")
        Tags.of(fn).add("use-case", "clinical-trial")
        return fn

    def _create_api_gateway(self) -> apigwv2.HttpApi:
        integration = integrations.HttpLambdaIntegration("LambdaIntegration", self.fn)

        api = apigwv2.HttpApi(
            self, "Api",
            api_name=f"{APP_NAME}-api",
            default_integration=integration,
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=[Lazy.string({"produce": lambda _: f"https://{self.distribution.distribution_domain_name}"})],
                allow_methods=[apigwv2.CorsHttpMethod.ANY],
                allow_headers=["Content-Type", "Authorization", "X-Api-Key"],
                max_age=Duration.hours(1),
            ),
        )
        Tags.of(api).add("domain", "hcls")
        return api

    def _create_frontend_bucket(self) -> s3.Bucket:
        return s3.Bucket(
            self, "FrontendBucket",
            bucket_name=f"{APP_NAME}-frontend-{self.account}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
        )

    def _create_cloudfront(self) -> cloudfront.Distribution:
        s3_origin = origins.S3BucketOrigin.with_origin_access_control(self.frontend_bucket)
        api_origin = origins.HttpOrigin(
            f"{self.api.api_id}.execute-api.{self.region}.amazonaws.com",
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        )

        waf_acl = wafv2.CfnWebACL(
            self, "WafAcl",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=f"{APP_NAME}-waf",
                sampled_requests_enabled=True,
            ),
            rules=[wafv2.CfnWebACL.RuleProperty(
                name="RateLimit", priority=1,
                action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                statement=wafv2.CfnWebACL.StatementProperty(
                    rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                        limit=2000, aggregate_key_type="IP",
                    ),
                ),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True, metric_name="RateLimit", sampled_requests_enabled=True,
                ),
            )],
        )

        return cloudfront.Distribution(
            self, "Distribution",
            comment=f"{APP_NAME} dashboard",
            default_behavior=cloudfront.BehaviorOptions(
                origin=s3_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=api_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                ),
            },
            default_root_object="index.html",
            error_responses=[cloudfront.ErrorResponse(
                http_status=404, response_http_status=200,
                response_page_path="/index.html", ttl=Duration.seconds(0),
            )],
            web_acl_id=waf_acl.attr_arn,
        )

    def _create_outputs(self) -> None:
        CfnOutput(self, "LambdaFunctionName", value=self.fn.function_name)
        CfnOutput(self, "ApiEndpoint", value=self.api.api_endpoint)
        CfnOutput(self, "CloudFrontUrl", value=f"https://{self.distribution.distribution_domain_name}")
        CfnOutput(self, "CloudFrontDistributionId", value=self.distribution.distribution_id)
        CfnOutput(self, "FrontendBucketName", value=self.frontend_bucket.bucket_name)
        CfnOutput(self, "PhiKeyArn", value=self.phi_key.key_arn)
        CfnOutput(self, "LambdaRoleArn", value=self.lambda_role.role_arn)
