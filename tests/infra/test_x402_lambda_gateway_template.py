"""Static security and routing checks for the x402 SAM gateway template."""
from __future__ import annotations

import re
import unittest
from pathlib import Path


TEMPLATE = Path(__file__).resolve().parents[2] / "infra" / "x402-lambda-gateway.yaml"


class X402LambdaGatewayTemplateTests(unittest.TestCase):
    def test_template_has_only_four_api_events(self) -> None:
        text = TEMPLATE.read_text()

        self.assertIn("Type: AWS::Serverless::Api", text)
        self.assertIn("EndpointConfiguration: REGIONAL", text)
        self.assertIn("Path: /x402/price", text)
        self.assertIn("Path: /x402/analyze/async", text)
        self.assertIn("Path: /x402/jobs/{jobId}", text)
        self.assertIn("Path: /x402/jobs/{jobId}/resume", text)
        self.assertEqual(text.count("Type: Api"), 4)
        self.assertNotIn("{proxy+}", text)
        self.assertNotIn("Path: /x402/free", text)

    def test_lambda_has_exact_runtime_environment_and_timeout(self) -> None:
        text = TEMPLATE.read_text()

        environment = re.search(
            r"Environment:\n\s+Variables:\n(?P<variables>.*?)(?=\s{6}Policies:)",
            text,
            re.DOTALL,
        )
        self.assertIsNotNone(environment)
        names = re.findall(r"^\s{10}([A-Z0-9_]+):", environment.group("variables"), re.MULTILINE)
        self.assertEqual(names, ["AGENTCORE_INVOKE_URL", "OAUTH_SECRET_ARN", "X402_PUBLIC_BASE_URL"])
        self.assertIn("AutoPublishAlias: live", text)
        self.assertIn("Timeout: 28", text)
        self.assertNotIn("VpcConfig:", text)

    def test_lambda_secret_policy_is_resource_scoped(self) -> None:
        text = TEMPLATE.read_text()

        self.assertIn("Action: secretsmanager:GetSecretValue", text)
        self.assertEqual(text.count("Action: secretsmanager:GetSecretValue"), 1)
        self.assertIn("Resource: !Ref OAuthSecretArn", text)
        self.assertNotIn("Action: secretsmanager:*", text)
        self.assertNotRegex(
            text,
            r"secretsmanager:[^\n]+\n\s+Resource:\s+[\"']?\*[\"']?",
        )

    def test_waf_is_associated_with_generated_rest_stage(self) -> None:
        text = TEMPLATE.read_text()

        self.assertIn("Type: AWS::WAFv2::WebACL", text)
        self.assertIn("Type: AWS::WAFv2::WebACLAssociation", text)
        self.assertIn("DependsOn: X402ApiStage", text)
        self.assertIn("/restapis/${ApiId}/stages/${Stage}", text)
        self.assertIn("RateBasedStatement:", text)
        self.assertIn("Size: 262144", text)
        self.assertIn("OverrideAction: {Count: {}}", text)

    def test_logging_is_bounded_and_redacted(self) -> None:
        text = TEMPLATE.read_text()

        self.assertEqual(text.count("RetentionInDays: 30"), 2)
        self.assertIn("DataTraceEnabled: false", text)
        self.assertIn('"requestId":"$context.requestId"', text)
        self.assertIn('"route":"$context.httpMethod $context.resourcePath"', text)
        self.assertNotIn("$context.requestOverride", text)
        self.assertNotIn("$context.requestBody", text)
        self.assertNotIn("$context.responseBody", text)
        self.assertNotIn("AWS::ApiGateway::Account", text)

    def test_bounded_metrics_alarms_and_base_url_output_exist(self) -> None:
        text = TEMPLATE.read_text()

        self.assertIn("Type: AWS::Logs::MetricFilter", text)
        self.assertEqual(text.count("Type: AWS::CloudWatch::Alarm"), 3)
        self.assertIn("X402GatewayBaseUrl:", text)
        self.assertIn("https://${X402Api}.execute-api.${AWS::Region}.${AWS::URLSuffix}/${StageName}", text)


if __name__ == "__main__":
    unittest.main()
