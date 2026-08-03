import base64
import json
import unittest
from unittest.mock import patch

from agentcore_client import (
    AgentCoreClient,
    AgentInvocationRateLimited,
    AgentInvocationTimeout,
    AgentInvocationUnavailable,
    AgentInvocationUnauthorized,
    InvalidAgentResponse,
    _MAX_AGENTCORE_RESPONSE_BYTES,
    default_agentcore_transport,
)


REQUEST_ID = "x402gw_" + "b" * 64
ENVELOPE = {
    "version": 1,
    "requestId": REQUEST_ID,
    "method": "GET",
    "path": "/x402/price",
    "publicBaseUrl": "https://gateway.example.test/stages/testnet",
    "headers": {"accept": "application/json"},
    "bodyBase64": "",
}


def response_envelope(*, request_id=REQUEST_ID, status=402, headers=None, body=b'{"error":"Payment Required"}'):
    return {
        "requestId": request_id,
        "status": status,
        "headers": headers or {"content-type": "application/json"},
        "bodyBase64": base64.b64encode(body).decode("ascii"),
    }


def a2a_response(envelope, *, status=200, jsonrpc="2.0", response_id=REQUEST_ID):
    return {
        "status": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({
            "jsonrpc": jsonrpc,
            "id": response_id,
            "result": {
                "kind": "message",
                "messageId": "agent-message-1",
                "contextId": "context-1",
                "taskId": "task-1",
                "role": "agent",
                "parts": [{"kind": "data", "data": envelope}],
            },
        }).encode("utf-8"),
    }


class AgentTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.last_url = None
        self.last_headers = None
        self.last_body = None
        self.last_timeout_seconds = None
        self.calls = []

    def __call__(self, *, url, headers, body, timeout_seconds):
        self.last_url = url
        self.last_headers = dict(headers)
        self.last_body = body
        self.last_timeout_seconds = timeout_seconds
        self.calls.append({
            "headers": dict(headers),
            "body": body,
        })
        if self.error:
            raise self.error
        return self.response


class AgentCoreClientTests(unittest.TestCase):
    def test_default_transport_timeout_is_twenty_five_seconds(self):
        transport = AgentTransport(a2a_response(response_envelope()))
        AgentCoreClient(
            "https://agentcore.example.test/runtime",
            lambda: "Bearer access-token",
            transport,
        ).invoke(ENVELOPE)

        self.assertEqual(transport.last_timeout_seconds, 25.0)

    def test_invocation_is_valid_a2a_data_part(self):
        transport = AgentTransport(a2a_response(response_envelope()))
        client = AgentCoreClient(
            "https://agentcore.example.test/runtime", lambda: "Bearer access-token", transport
        )
        response = client.invoke(ENVELOPE)
        request = json.loads(transport.last_body)
        data = request["params"]["message"]["parts"][0]["data"]
        self.assertEqual(data, {"skill": "x402_http_envelope", "envelope": ENVELOPE})
        self.assertEqual(transport.last_headers["Authorization"], "Bearer access-token")
        self.assertEqual(transport.last_headers["Content-Type"], "application/json")
        self.assertRegex(
            transport.last_headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"],
            r"\Ax402-gateway-session-[0-9a-f]{32}\Z",
        )
        self.assertEqual(response, response_envelope())

    def test_identical_requests_use_fresh_sessions_but_keep_request_binding(self):
        transport = AgentTransport(a2a_response(response_envelope()))
        client = AgentCoreClient(
            "https://agentcore.example.test/runtime",
            lambda: "Bearer access-token",
            transport,
        )

        with patch(
            "agentcore_client.secrets.token_hex",
            side_effect=["a" * 32, "b" * 32],
        ):
            client.invoke(ENVELOPE)
            client.invoke(ENVELOPE)

        session_ids = [
            call["headers"]["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"]
            for call in transport.calls
        ]
        self.assertEqual(session_ids, [
            f"x402-gateway-session-{'a' * 32}",
            f"x402-gateway-session-{'b' * 32}",
        ])
        self.assertNotEqual(session_ids[0], session_ids[1])
        requests = [json.loads(call["body"]) for call in transport.calls]
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[0]["id"], REQUEST_ID)
        self.assertEqual(
            requests[0]["params"]["message"]["messageId"],
            REQUEST_ID,
        )
        self.assertEqual(
            requests[0]["params"]["message"]["parts"][0]["data"]["envelope"]["requestId"],
            REQUEST_ID,
        )

    def test_rejects_mismatched_response_request_id(self):
        client = AgentCoreClient(
            "https://agentcore.example.test/runtime", lambda: "Bearer access-token",
            AgentTransport(a2a_response(response_envelope(request_id="x402gw_" + "a" * 64))),
        )
        with self.assertRaises(InvalidAgentResponse):
            client.invoke(ENVELOPE)

    def test_rejects_jsonrpc_version_or_id_not_bound_to_request(self):
        for jsonrpc, response_id in (("1.0", REQUEST_ID), ("2.0", "different-request"), (None, REQUEST_ID)):
            with self.subTest(jsonrpc=jsonrpc, response_id=response_id), self.assertRaises(InvalidAgentResponse):
                AgentCoreClient(
                    "https://agentcore.example.test/runtime", lambda: "Bearer access-token",
                    AgentTransport(a2a_response(response_envelope(), jsonrpc=jsonrpc, response_id=response_id)),
                ).invoke(ENVELOPE)

    def test_rejects_non_object_jsonrpc_response(self):
        with self.assertRaises(InvalidAgentResponse):
            AgentCoreClient(
                "https://agentcore.example.test/runtime", lambda: "Bearer access-token",
                AgentTransport({"status": 200, "headers": {"content-type": "application/json"}, "body": b"[]"}),
            ).invoke(ENVELOPE)

    def test_maps_documented_upstream_statuses_without_body(self):
        expected = {401: AgentInvocationUnauthorized, 403: AgentInvocationUnauthorized, 429: AgentInvocationRateLimited, 500: AgentInvocationUnavailable}
        for status, error_type in expected.items():
            with self.subTest(status=status), self.assertRaises(error_type) as captured:
                AgentCoreClient(
                    "https://agentcore.example.test/runtime", lambda: "Bearer access-token",
                    AgentTransport({"status": status, "headers": {"content-type": "text/plain"}, "body": b"private upstream response"}),
                ).invoke(ENVELOPE)
            self.assertNotIn("private upstream response", str(captured.exception))

    def test_maps_transport_timeout_to_indeterminate_error(self):
        with self.assertRaises(AgentInvocationTimeout):
            AgentCoreClient(
                "https://agentcore.example.test/runtime", lambda: "Bearer access-token",
                AgentTransport(error=TimeoutError()),
            ).invoke(ENVELOPE)

    def test_rejects_oversized_agentcore_response_before_json_parse(self):
        with self.assertRaises(InvalidAgentResponse):
            AgentCoreClient(
                "https://agentcore.example.test/runtime", lambda: "Bearer access-token",
                AgentTransport({"status": 200, "headers": {"content-type": "application/json"}, "body": b"x" * (_MAX_AGENTCORE_RESPONSE_BYTES + 1)}),
            ).invoke(ENVELOPE)

    def test_default_transport_uses_bounded_agentcore_read(self):
        response = _ReadResponse()
        with patch("agentcore_client.urlopen", return_value=response):
            default_agentcore_transport(
                url="https://agentcore.example.test/runtime", headers={}, body=b"", timeout_seconds=1
            )
        self.assertEqual(response.read_size, _MAX_AGENTCORE_RESPONSE_BYTES + 1)


class _ReadResponse:
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        self.read_size = size
        return b"{}"
