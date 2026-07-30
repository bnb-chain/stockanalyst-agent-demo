import base64
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import handler
from agentcore_client import AgentInvocationTimeout, InvalidAgentResponse
from oauth_client import OAuthUnavailable


PUBLIC_BASE = "https://a1b2c3d4e5.execute-api.us-east-1.amazonaws.com/testnet"


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
    "requestContext": {
        "domainName": "a1b2c3d4e5.execute-api.us-east-1.amazonaws.com",
        "stage": "testnet",
        "requestId": "gateway-request",
    },
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
    def invoke(self, oauth, agentcore, event=EVENT):
        app = handler.GatewayApplication(oauth, agentcore)
        with patch.object(handler, "_application", app):
            return handler.lambda_handler(event, CONTEXT)

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
        self.assertFalse(result["isBase64Encoded"])
        self.assertEqual(
            json.loads(result["body"]),
            json.loads(base64.b64decode(response_envelope()["bodyBase64"])),
        )

    def test_rejects_non_utf8_upstream_body(self):
        response = response_envelope()
        response["bodyBase64"] = base64.b64encode(b"\xff").decode("ascii")

        result = self.invoke(FakeOAuth(), FakeAgentCore(response))

        self.assertEqual(result["statusCode"], 502)
        self.assertEqual(json.loads(result["body"]), {"errorCode": "invalid_upstream_response"})

    def test_derives_exact_public_base_from_trusted_rest_context(self):
        agentcore = FakeAgentCore(response_envelope(status=200))

        result = self.invoke(FakeOAuth(), agentcore)

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(agentcore.calls[0]["envelope"]["publicBaseUrl"], PUBLIC_BASE)

    def test_caller_host_and_forwarded_headers_cannot_influence_public_base(self):
        event = {
            **EVENT,
            "headers": {
                "Host": "attacker.example.test",
                "X-Forwarded-Host": "attacker.example.test",
                "X-Forwarded-Proto": "http",
            },
        }
        agentcore = FakeAgentCore(response_envelope(status=200))

        self.invoke(FakeOAuth(), agentcore, event)

        self.assertEqual(agentcore.calls[0]["envelope"]["publicBaseUrl"], PUBLIC_BASE)

    def test_missing_or_malformed_trusted_context_returns_safe_503(self):
        for request_context in (
            {},
            {"domainName": "attacker.example.test", "stage": "testnet"},
            {"domainName": "a1b2c3d4e5.execute-api.us-east-1.amazonaws.com", "stage": "test/net"},
        ):
            with self.subTest(request_context=request_context):
                agentcore = FakeAgentCore(response_envelope(status=200))
                result = self.invoke(
                    FakeOAuth(),
                    agentcore,
                    {**EVENT, "requestContext": request_context},
                )
                self.assertEqual(result["statusCode"], 503)
                self.assertEqual(json.loads(result["body"])["errorCode"], "service_unavailable")
                self.assertEqual(agentcore.calls, [])

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
        app = handler.GatewayApplication(FakeOAuth(), FakeAgentCore(response_envelope()))
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
            "AGENTCORE_INVOKE_URL": "https://agentcore.example.test/runtime",
            "OAUTH_SECRET_ARN": "arn:aws:secretsmanager:region:account:secret:gateway",
        }
        with patch("handler.OAuthClient", _ConfiguredOAuth), patch("handler.AgentCoreClient", _ConfiguredAgentCore):
            app = handler._application_from_environment(environment, lambda arn: lambda: "secret")
        with patch.object(handler, "_application", app):
            result = handler.lambda_handler(EVENT, CONTEXT)
        self.assertEqual(result["statusCode"], 200)

    def test_removed_public_base_environment_variable_is_ignored(self):
        environment = {
            "X402_PUBLIC_BASE_URL": "http://attacker.example.test",
            "AGENTCORE_INVOKE_URL": "https://agentcore.example.test/runtime",
            "OAUTH_SECRET_ARN": "arn:aws:secretsmanager:region:account:secret:gateway",
        }
        with patch("handler.OAuthClient", _ConfiguredOAuth), patch("handler.AgentCoreClient", _ConfiguredAgentCore):
            app = handler._application_from_environment(environment, lambda arn: lambda: "secret")
        with patch.object(handler, "_application", app):
            result = handler.lambda_handler(EVENT, CONTEXT)
        self.assertEqual(result["statusCode"], 200)

    def test_log_contains_only_safe_summary_fields(self):
        event = {**EVENT, "httpMethod": "POST", "path": "/testnet/x402/analyze/async", "body": '{"secret":"body-value"}', "headers": {"X-Payment": "payment-value", "X-Job-Token": "job-value"}}
        app = handler.GatewayApplication(FakeOAuth(), FakeAgentCore(response_envelope(status=200)))
        stdout = io.StringIO()
        with patch.object(handler, "_application", app), redirect_stdout(stdout):
            handler.lambda_handler(event, CONTEXT)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        message = lines[0]
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
