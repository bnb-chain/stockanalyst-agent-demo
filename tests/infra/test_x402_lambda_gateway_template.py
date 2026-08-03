"""Deterministic security and routing checks for the x402 SAM template."""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "infra" / "x402-lambda-gateway.yaml"
LAMBDA_ENVELOPE = ROOT / "gateway" / "x402_lambda" / "src" / "envelope.py"


def resource_section(text: str, logical_id: str, next_logical_id: str) -> str:
    match = re.search(
        rf"^  {logical_id}:\n(?P<section>.*?)(?=^  {next_logical_id}:)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    if match is None:
        raise AssertionError(f"missing {logical_id} resource section")
    return match.group("section")


class X402LambdaGatewayTemplateTests(unittest.TestCase):
    def test_rest_api_does_not_enable_binary_json_media_type(self) -> None:
        text = TEMPLATE.read_text()
        api = resource_section(text, "X402Api", "X402Adapter")

        self.assertNotIn("BinaryMediaTypes:", api)

    def test_rest_api_has_only_the_four_published_routes(self) -> None:
        text = TEMPLATE.read_text()

        self.assertIn("Type: AWS::Serverless::Api", text)
        self.assertIn("EndpointConfiguration: REGIONAL", text)
        self.assertEqual(text.count("x-amazon-apigateway-integration:"), 4)
        for method, path in (
            ("get", "/x402/price"),
            ("post", "/x402/analyze/async"),
            ("get", "/x402/jobs/{jobId}"),
            ("post", "/x402/jobs/{jobId}/resume"),
        ):
            self.assertRegex(text, rf"(?ms)^          {re.escape(path)}:\n            {method}:")
        self.assertNotIn("{proxy+}", text)
        self.assertNotIn("Path: /x402/free", text)
        self.assertNotIn("AWS::ApiGateway::DomainName", text)

    def test_lambda_has_hardened_runtime_configuration_without_api_reference(self) -> None:
        text = TEMPLATE.read_text()
        adapter = resource_section(text, "X402Adapter", "X402ApiInvokePermission")

        self.assertIn("Runtime: python3.13", adapter)
        self.assertIn("Handler: handler.lambda_handler", adapter)
        self.assertIn("CodeUri: ../gateway/x402_lambda/src", adapter)
        self.assertIn("AutoPublishAlias: live", adapter)
        self.assertIn("Timeout: 28", adapter)
        self.assertIn(
            'ReservedConcurrentExecutions: !If [UseReservedConcurrency, !Ref ReservedConcurrency, !Ref "AWS::NoValue"]',
            adapter,
        )
        self.assertIn("Tracing: Active", adapter)
        self.assertIn("LoggingConfig:\n        LogFormat: Text", adapter)
        self.assertNotIn("VpcConfig:", adapter)
        self.assertNotIn("X402Api", adapter)
        self.assertNotIn("X402_PUBLIC_BASE_URL", adapter)
        environment = re.search(
            r"Environment:\n\s+Variables:\n(?P<variables>.*?)(?=\s{6}Policies:)",
            adapter,
            re.DOTALL,
        )
        self.assertIsNotNone(environment)
        names = re.findall(r"^\s{10}([A-Z0-9_]+):", environment.group("variables"), re.MULTILINE)
        self.assertEqual(names, ["AGENTCORE_INVOKE_URL", "OAUTH_SECRET_ARN"])

    def test_reserved_concurrency_is_optional_and_defaults_off(self) -> None:
        text = TEMPLATE.read_text()
        adapter = resource_section(text, "X402Adapter", "X402ApiInvokePermission")
        parameter = re.search(
            r"(?ms)^  ReservedConcurrency:\n(?P<section>(?:    .*\n)+)",
            text,
        )

        self.assertIsNotNone(parameter)
        self.assertIn("Default: 0", parameter.group("section"))
        self.assertIn("MinValue: 0", parameter.group("section"))
        self.assertIn(
            "UseReservedConcurrency: !Not [!Equals [!Ref ReservedConcurrency, 0]]",
            text,
        )
        self.assertIn(
            'ReservedConcurrentExecutions: !If [UseReservedConcurrency, !Ref ReservedConcurrency, !Ref "AWS::NoValue"]',
            adapter,
        )

    def test_lambda_secret_policy_is_one_resource_scoped_action(self) -> None:
        text = TEMPLATE.read_text()
        adapter = resource_section(text, "X402Adapter", "X402ApiInvokePermission")

        self.assertEqual(adapter.count("Action: secretsmanager:GetSecretValue"), 1)
        self.assertIn("Resource: !Ref OAuthSecretArn", adapter)
        self.assertNotIn("Action: secretsmanager:*", adapter)
        self.assertNotRegex(adapter, r"Resource:\s+[\"']?\*[\"']?")

    def test_waf_has_rate_limit_and_observe_only_common_rules_but_no_body_claim(self) -> None:
        text = TEMPLATE.read_text()

        self.assertIn("Type: AWS::WAFv2::WebACL", text)
        self.assertIn("Type: AWS::WAFv2::WebACLAssociation", text)
        self.assertIn("DependsOn: X402ApiStage", text)
        self.assertIn("/restapis/${ApiId}/stages/${Stage}", text)
        self.assertIn("RateBasedStatement:", text)
        self.assertIn("SearchString: /x402/", text)
        self.assertIn("OverrideAction: {Count: {}}", text)
        self.assertIn("Name: AWSManagedRulesCommonRuleSet", text)
        self.assertNotIn("SizeConstraintStatement:", text)
        self.assertNotIn("RejectOversizedBody", text)
        self.assertNotIn("OversizeHandling:", text)
        self.assertNotIn("262144", text)

    def test_lambda_retains_exact_256_kib_body_enforcement(self) -> None:
        envelope = LAMBDA_ENVELOPE.read_text()

        self.assertIn("_MAX_BODY_BYTES = 256 * 1024", envelope)
        self.assertIn('GatewayRequestError("request_too_large", 413)', envelope)

    def test_access_logging_is_bounded_to_the_five_safe_fields(self) -> None:
        text = TEMPLATE.read_text()
        match = re.search(
            r"AccessLogSetting:\n(?P<access_log>.*?)(?=\s{6}MethodSettings:)",
            text,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        access_log = match.group("access_log")

        self.assertIn("DataTraceEnabled: false", text)
        for field in (
            '"requestId":"$context.requestId"',
            '"route":"$context.httpMethod $context.resourcePath"',
            '"status":"$context.status"',
            '"latency":"$context.responseLatency"',
            '"integrationStatus":"$context.integrationStatus"',
        ):
            self.assertIn(field, access_log)
        self.assertNotRegex(access_log.lower(), r"header|body")
        self.assertEqual(text.count("RetentionInDays: 30"), 2)
        self.assertNotIn("AWS::ApiGateway::Account", text)

    def test_metrics_and_alarms_have_safe_bounded_scope(self) -> None:
        text = TEMPLATE.read_text()

        self.assertIn("FilterPattern: '{ $.status >= 500 }'", text)
        self.assertEqual(text.count("Type: AWS::CloudWatch::Alarm"), 3)
        self.assertRegex(text, r"(?s)MetricName: Errors.*?Name: FunctionName\n\s+Value: !Ref X402Adapter")
        self.assertRegex(text, r"(?s)MetricName: Throttles.*?Name: FunctionName\n\s+Value: !Ref X402Adapter")
        self.assertRegex(text, r"(?s)MetricName: 5XXError.*?Name: ApiName.*?Name: Stage\n\s+Value: !Ref StageName")

    def test_api_url_is_output_only_and_invocation_permission_is_explicit(self) -> None:
        text = TEMPLATE.read_text()

        self.assertIn("Type: AWS::Lambda::Permission", text)
        self.assertIn("X402GatewayBaseUrl:", text)
        self.assertIn("https://${X402Api}.execute-api.${AWS::Region}.${AWS::URLSuffix}/${StageName}", text)

    def test_all_integrations_and_permission_target_the_live_alias(self) -> None:
        text = TEMPLATE.read_text()

        self.assertEqual(text.count("${X402Adapter.Arn}:live/invocations"), 4)
        self.assertNotIn("${X402Adapter.Arn}/invocations", text)
        permission = resource_section(text, "X402ApiInvokePermission", "X402ApiAccessLogGroup")
        self.assertIn("DependsOn: X402AdapterAliaslive", permission)
        self.assertIn("FunctionName: !Sub \"${X402Adapter.Arn}:live\"", permission)


if __name__ == "__main__":
    unittest.main()
