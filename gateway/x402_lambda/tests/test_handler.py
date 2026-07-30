import base64
import json
import os
import unittest
from unittest.mock import patch

import handler
from agentcore_client import AgentInvocationTimeout, InvalidAgentResponse
from oauth_client import OAuthUnavailable


PUBLIC_BASE = "https://gateway.example.test/stages/testnet"


EVENT = {
    "resource": "/{proxy+}",
    "path": "/testnet/x402/price",
    "httpMethod": "GET",
    "headers": {"Accept": "application/json"},
    "multiValueHeaders": {},
    "queryStringParameters": None,
    "multiValueQueryStringParameters": None,
    "pathParameters": {"proxy": "x402/price"},
    "stageVariables": None,
    "requestContext": {"stage": "testnet", "requestId": "gateway-request"},
    "body": None,
    "isBase64Encoded": False,
}
CONTEXT = object()


def response_envelope(*, status=402, headers=None, body=b'{"error":"Payment Required"}'):
    return {
        "requestId": "ignored-by-test",
        "status": status,
        "headers": headers or {"content-type": "application/json"},
        "bodyBase64": base64.b64encode(body).decode("ascii"),
    }


class FakeOAuth:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = 0

    def authorization_header(self):
        self.calls += 1
        if self.failure:
            raise self.failure
        return "Bearer test-token"


class FakeAgentCore:
    def __init__(self, result=None, failure=None):
        self.result = result
        self.failure = failure
        self.calls = []

    def invoke(self, envelope, *, authorization_header=None):
        self.calls.append({"envelope": envelope, "authorization_header": authorization_header})
        if self.failure:
            raise self.failure
        return {**self.result, "requestId": envelope["requestId"]}


class HandlerTests(unittest.TestCase):
    def invoke(self, oauth, agentcore):
        app = handler.GatewayApplication(PUBLIC_BASE, oauth, agentcore)
        with patch.object(handler, "_application", app):
            return handler.lambda_handler(EVENT, CONTEXT)

    def test_reconstructs_application_402(self):
        result = self.invoke(
            FakeOAuth(),
            FakeAgentCore(response_envelope(headers={
                "content-type": "application/json",
                "x-payment-required": '{"x402Version":2}',
            })),
        )
        self.assertEqual(result["statusCode"], 402)
        self.assertEqual(result["headers"]["x-payment-required"], '{"x402Version":2}')
        self.assertTrue(result["isBase64Encoded"])

    def test_oauth_failure_returns_safe_503(self):
        result = self.invoke(FakeOAuth(OAuthUnavailable()), FakeAgentCore(response_envelope()))
        self.assertEqual(result["statusCode"], 503)
        self.assertEqual(json.loads(result["body"])["errorCode"], "service_unavailable")

    def test_malformed_agent_response_returns_safe_502(self):
        result = self.invoke(FakeOAuth(), FakeAgentCore(failure=InvalidAgentResponse()))
        self.assertEqual(result["statusCode"], 502)
        self.assertNotIn("AgentCore", result["body"])

    def test_upstream_timeout_returns_indeterminate_503(self):
        result = self.invoke(FakeOAuth(), FakeAgentCore(failure=AgentInvocationTimeout()))
        self.assertEqual(result["statusCode"], 503)
        body = json.loads(result["body"])
        self.assertEqual(body["errorCode"], "settlement_status_unknown")
        self.assertTrue(body["retryable"])
        self.assertNotIn("proof", result["body"].lower())

    def test_invalid_gateway_request_uses_its_safe_status(self):
        event = {**EVENT, "path": "/testnet/x402/free"}
        app = handler.GatewayApplication(PUBLIC_BASE, FakeOAuth(), FakeAgentCore(response_envelope()))
        with patch.object(handler, "_application", app):
            result = handler.lambda_handler(event, CONTEXT)
        self.assertEqual(result["statusCode"], 400)
        self.assertEqual(json.loads(result["body"])["errorCode"], "route_not_allowed")

    def test_missing_configuration_returns_safe_503(self):
        with patch.object(handler, "_application", None), patch.dict(os.environ, {}, clear=True):
            result = handler.lambda_handler(EVENT, CONTEXT)
        self.assertEqual(result["statusCode"], 503)
        self.assertEqual(json.loads(result["body"])["errorCode"], "service_unavailable")

    def test_exact_task_four_environment_names_construct_and_dispatch(self):
        environment = {
            "X402_PUBLIC_BASE_URL": PUBLIC_BASE,
            "AGENTCORE_INVOKE_URL": "https://agentcore.example.test/runtime",
            "OAUTH_SECRET_ARN": "arn:aws:secretsmanager:region:account:secret:gateway",
        }
        with patch("handler.OAuthClient", _ConfiguredOAuth), patch("handler.AgentCoreClient", _ConfiguredAgentCore):
            app = handler._application_from_environment(environment, lambda arn: lambda: "secret")
        with patch.object(handler, "_application", app):
            result = handler.lambda_handler(EVENT, CONTEXT)
        self.assertEqual(result["statusCode"], 200)

    def test_malformed_configured_public_base_is_safe_503(self):
        environment = {
            "X402_PUBLIC_BASE_URL": "http://not-https.example.test",
            "AGENTCORE_INVOKE_URL": "https://agentcore.example.test/runtime",
            "OAUTH_SECRET_ARN": "arn:aws:secretsmanager:region:account:secret:gateway",
        }
        with patch.object(handler, "_application", None), patch.dict(os.environ, environment, clear=True):
            result = handler.lambda_handler(EVENT, CONTEXT)
        self.assertEqual(result["statusCode"], 503)
        self.assertEqual(json.loads(result["body"])["errorCode"], "service_unavailable")

    def test_log_contains_only_safe_summary_fields(self):
        event = {**EVENT, "httpMethod": "POST", "path": "/testnet/x402/analyze/async", "body": '{"secret":"body-value"}', "headers": {"X-Payment": "payment-value", "X-Job-Token": "job-value"}}
        app = handler.GatewayApplication(PUBLIC_BASE, FakeOAuth(), FakeAgentCore(response_envelope(status=200)))
        with patch.object(handler, "_application", app), self.assertLogs(handler.logger, "INFO") as captured:
            handler.lambda_handler(event, CONTEXT)
        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        self.assertEqual(set(json.loads(message)), {"requestId", "route", "status", "outcome", "durationMilliseconds"})
        for secret in ("payment-value", "job-value", "body-value"):
            self.assertNotIn(secret, message)


class _ConfiguredOAuth:
    def __init__(self, secret_reader, transport):
        self._secret_reader = secret_reader

    def authorization_header(self):
        return "Bearer configured-token"


class _ConfiguredAgentCore:
    def __init__(self, runtime_url, authorization_header, transport):
        self.runtime_url = runtime_url

    def invoke(self, envelope, *, authorization_header=None):
        return {
            "requestId": envelope["requestId"],
            "status": 200,
            "headers": {"content-type": "application/json"},
            "bodyBase64": "e30=",
        }
