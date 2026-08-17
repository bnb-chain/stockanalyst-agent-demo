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
    def test_stage_defaults_to_mainnet(self) -> None:
        text = TEMPLATE.read_text()
        parameter = re.search(
            r"(?ms)^  StageName:\n(?P<section>.*?)(?=^  AgentCoreInvokeUrl:)",
            text,
        )

        self.assertIsNotNone(parameter)
        self.assertIn("Default: mainnet", parameter.group("section"))
        self.assertNotIn("Default: testnet", parameter.group("section"))

    def test_rest_api_does_not_enable_binary_json_media_type(self) -> None:
        text = TEMPLATE.read_text()
        api = resource_section(text, "X402Api", "X402CustomDomain")

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

    def test_regional_custom_domain_maps_root_to_mainnet(self) -> None:
        text = TEMPLATE.read_text()

        self.assertIn("CustomDomainName:", text)
        self.assertIn("Default: stock-agent.bnbchain.org", text)
        self.assertIn("CustomDomainCertificateArn:", text)
        self.assertRegex(
            text,
            r"(?s)X402CustomDomain:.*?Type: AWS::ApiGateway::DomainName.*?"
            r"DomainName: !Ref CustomDomainName.*?Types:\n\s+- REGIONAL.*?"
            r"RegionalCertificateArn: !Ref CustomDomainCertificateArn.*?"
            r"SecurityPolicy: TLS_1_2",
        )
        mapping = resource_section(
            text, "X402CustomDomainMapping", "X402Adapter"
        )
        self.assertIn("DomainName: !Ref X402CustomDomain", mapping)
        self.assertIn("RestApiId: !Ref X402Api", mapping)
        self.assertIn("Stage: !Ref StageName", mapping)
        self.assertNotIn("BasePath:", mapping)

    def test_default_endpoint_switch_is_explicit_and_defaults_open(self) -> None:
        text = TEMPLATE.read_text()

        self.assertRegex(
            text,
            r'(?s)DisableExecuteApiEndpoint:.*?Default: "false".*?'
            r'AllowedValues: \["true", "false"\]',
        )
        self.assertIn(
            'DisableDefaultEndpoint: !Equals [!Ref DisableExecuteApiEndpoint, "true"]',
            text,
        )
        self.assertIn(
            "DisableExecuteApiEndpoint: !If [DisableDefaultEndpoint, true, false]",
            text,
        )

    def test_default_endpoint_switch_also_updates_the_managed_stage(self) -> None:
        text = TEMPLATE.read_text()
        api = resource_section(text, "X402Api", "X402CustomDomain")

        self.assertIn("Variables:", api)
        self.assertIn(
            "DefaultEndpointDisabled: !Ref DisableExecuteApiEndpoint", api
        )
        self.assertNotIn("Type: AWS::ApiGateway::Deployment", text)

    def test_default_endpoint_switch_forces_a_managed_api_deployment(self) -> None:
        text = TEMPLATE.read_text()
        api = resource_section(text, "X402Api", "X402CustomDomain")

        self.assertIn("AlwaysDeploy: true", api)
        self.assertNotIn("Type: AWS::ApiGateway::Deployment", text)

    def test_lambda_receives_only_exact_nonsecret_custom_domain_setting(self) -> None:
        text = TEMPLATE.read_text()
        adapter = resource_section(text, "X402Adapter", "X402ApiInvokePermission")
        environment = re.search(
            r"(?m)^      Environment:\n        Variables:\n"
            r"(?P<variables>(?:          .*\n)+)",
            adapter,
        )

        self.assertIsNotNone(environment)
        names = re.findall(
            r"^\s{10}([A-Z0-9_]+):",
            environment.group("variables"),
            re.MULTILINE,
        )
        self.assertEqual(
            names,
            ["AGENTCORE_INVOKE_URL", "OAUTH_SECRET_ARN", "X402_CUSTOM_DOMAIN_NAME"],
        )
        self.assertIn("X402_CUSTOM_DOMAIN_NAME: !Ref CustomDomainName", adapter)

    def test_create_waf_matcher_covers_the_paid_async_route(self) -> None:
        text = TEMPLATE.read_text()
        web_acl = resource_section(text, "X402WebAcl", "X402WebAclAssociation")
        create_rule = re.search(
            r"(?ms)^        - Name: X402CreateRateLimit\n"
            r"(?P<section>.*?)(?=^        - Name: X402ReadRateLimit)",
            web_acl,
        )

        self.assertIsNotNone(create_rule)
        section = create_rule.group("section")
        self.assertRegex(
            section,
            r"(?s)FieldToMatch: \{Method: \{\}\}\n"
            r"\s+PositionalConstraint: EXACTLY\n"
            r"\s+SearchString: POST",
        )
        self.assertIn("OrStatement:", section)
        self.assertRegex(
            section,
            r"(?s)FieldToMatch: \{UriPath: \{\}\}\n"
            r"\s+PositionalConstraint: EXACTLY\n"
            r"\s+SearchString: /x402/analyze/async",
        )

    def test_template_adds_no_shared_promotional_state_resources(self) -> None:
        text = TEMPLATE.read_text()
        resources = text.split("Resources:\n", 1)[1].split("Outputs:\n", 1)[0]
        logical_ids = set(re.findall(r"(?m)^  ([A-Za-z0-9]+):$", resources))

        self.assertEqual(logical_ids, {
            "X402Api",
            "X402CustomDomain",
            "X402CustomDomainMapping",
            "X402Adapter",
            "X402ApiInvokePermission",
            "X402ApiAccessLogGroup",
            "X402AdapterLogGroup",
            "X402Gateway5xxMetric",
            "X402WebAcl",
            "X402WebAclAssociation",
            "X402LambdaErrors",
            "X402LambdaThrottles",
            "X402ApiServerErrors",
        })
        for forbidden in (
            "AWS::Route53::",
            "AWS::CloudFront::",
            "AWS::IAM::",
            "AWS::DynamoDB::",
            "AWS::ElastiCache::",
        ):
            self.assertNotIn(forbidden, resources)

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
        self.assertIn("Role: !Ref LambdaExecutionRoleArn", adapter)
        self.assertNotIn("VpcConfig:", adapter)
        self.assertNotIn("X402Api", adapter)
        self.assertNotIn("X402_PUBLIC_BASE_URL", adapter)
        environment = re.search(
            r"(?m)^      Environment:\n        Variables:\n(?P<variables>(?:          .*\n)+)",
            adapter,
        )
        self.assertIsNotNone(environment)
        names = re.findall(r"^\s{10}([A-Z0-9_]+):", environment.group("variables"), re.MULTILINE)
        self.assertEqual(
            names,
            ["AGENTCORE_INVOKE_URL", "OAUTH_SECRET_ARN", "X402_CUSTOM_DOMAIN_NAME"],
        )
        self.assertNotIn("\n      Policies:", adapter)

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

    def test_lambda_uses_supplied_execution_role_without_inline_permissions(self) -> None:
        text = TEMPLATE.read_text()
        adapter = resource_section(text, "X402Adapter", "X402ApiInvokePermission")

        self.assertEqual(adapter.count("Role: !Ref LambdaExecutionRoleArn"), 1)
        self.assertNotIn("\n      Policies:", adapter)
        self.assertNotRegex(adapter, r"(?m)^\s+Action:\s+[\"']?\*[\"']?$")
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
        self.assertIn('Value: !Sub "https://${CustomDomainName}"', text)
        self.assertIn("X402RegionalDomainName:", text)
        self.assertIn("Value: !GetAtt X402CustomDomain.RegionalDomainName", text)
        self.assertIn("X402RegionalHostedZoneId:", text)
        self.assertIn("Value: !GetAtt X402CustomDomain.RegionalHostedZoneId", text)

    def test_all_integrations_and_permission_target_the_live_alias(self) -> None:
        text = TEMPLATE.read_text()

        self.assertEqual(text.count("${X402Adapter.Arn}:live/invocations"), 4)
        self.assertNotIn("${X402Adapter.Arn}/invocations", text)
        permission = resource_section(text, "X402ApiInvokePermission", "X402ApiAccessLogGroup")
        self.assertIn("DependsOn: X402AdapterAliaslive", permission)
        self.assertIn("FunctionName: !Sub \"${X402Adapter.Arn}:live\"", permission)


if __name__ == "__main__":
    unittest.main()
