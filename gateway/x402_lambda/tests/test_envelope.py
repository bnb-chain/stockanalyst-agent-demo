import base64
import unittest

from envelope import GatewayRequestError, build_envelope


PUBLIC_BASE = "https://gateway.example.test/stages/testnet"
JOB_ID = "x402_" + "a" * 32


def api_event(
    *,
    method="GET",
    path="/x402/price",
    headers=None,
    body=b"",
    base64_encoded=False,
    stage="testnet",
):
    encoded = base64.b64encode(body).decode("ascii") if base64_encoded else body.decode("utf-8")
    return {
        "resource": "/{proxy+}",
        "path": f"/{stage}{path}",
        "httpMethod": method,
        "headers": headers or {},
        "multiValueHeaders": {},
        "queryStringParameters": None,
        "multiValueQueryStringParameters": None,
        "pathParameters": {"proxy": path.lstrip("/")},
        "stageVariables": None,
        "requestContext": {"stage": stage, "requestId": "gateway-request"},
        "body": encoded,
        "isBase64Encoded": base64_encoded,
    }


class EnvelopeTests(unittest.TestCase):
    def test_create_route_builds_envelope(self):
        envelope = build_envelope(
            api_event(
                method="POST",
                path="/x402/analyze/async",
                headers={"X-Payment": "proof", "Content-Type": "application/json"},
                body=b'{"symbols":["AAPL"]}',
            ),
            public_base_url=PUBLIC_BASE,
        )
        self.assertEqual(envelope["version"], 1)
        self.assertEqual(envelope["method"], "POST")
        self.assertEqual(envelope["path"], "/x402/analyze/async")
        self.assertEqual(envelope["publicBaseUrl"], PUBLIC_BASE)
        self.assertEqual(envelope["headers"]["x-payment"], "proof")

    def test_rejects_unpublished_route(self):
        with self.assertRaisesRegex(GatewayRequestError, "route_not_allowed"):
            build_envelope(api_event(path="/x402/free"), public_base_url=PUBLIC_BASE)

    def test_request_id_is_stable_for_exact_retry(self):
        event = api_event(headers={"X-Payment": "proof"})
        first = build_envelope(event, public_base_url=PUBLIC_BASE)
        second = build_envelope(event, public_base_url=PUBLIC_BASE)
        self.assertEqual(first["requestId"], second["requestId"])
        self.assertRegex(first["requestId"], r"\Ax402gw_[0-9a-f]{64}\Z")

    def test_removes_stage_prefix_and_rejects_query_string(self):
        event = api_event(path=f"/x402/jobs/{JOB_ID}")
        event["queryStringParameters"] = {"unexpected": "value"}
        with self.assertRaisesRegex(GatewayRequestError, "query_not_allowed"):
            build_envelope(event, public_base_url=PUBLIC_BASE)
        event["queryStringParameters"] = None
        self.assertEqual(
            build_envelope(event, public_base_url=PUBLIC_BASE)["path"],
            f"/x402/jobs/{JOB_ID}",
        )

    def test_rejects_invalid_base64_and_oversized_body(self):
        invalid = api_event(body=b"ignored", base64_encoded=True)
        invalid["body"] = "%%%"
        with self.assertRaisesRegex(GatewayRequestError, "invalid_body_base64"):
            build_envelope(invalid, public_base_url=PUBLIC_BASE)
        with self.assertRaisesRegex(GatewayRequestError, "request_too_large"):
            build_envelope(api_event(body=b"x" * (256 * 1024 + 1)), public_base_url=PUBLIC_BASE)

    def test_rejects_get_body_and_invalid_job_id_shape(self):
        with self.assertRaisesRegex(GatewayRequestError, "get_request_body_not_allowed"):
            build_envelope(api_event(body=b"no"), public_base_url=PUBLIC_BASE)
        with self.assertRaisesRegex(GatewayRequestError, "route_not_allowed"):
            build_envelope(api_event(path="/x402/jobs/not-a-job"), public_base_url=PUBLIC_BASE)

    def test_rejects_forbidden_caller_headers(self):
        for name in ("Host", "Authorization", "X-Forwarded-For", "Connection"):
            with self.subTest(name=name), self.assertRaisesRegex(
                GatewayRequestError, "request_header_not_allowed"
            ):
                build_envelope(
                    api_event(headers={name: "attacker-value", "X-Payment": "proof"}),
                    public_base_url=PUBLIC_BASE,
                )

    def test_rejects_forbidden_multivalue_headers(self):
        event = api_event(headers={"X-Payment": "proof"})
        event["multiValueHeaders"] = {"X-Forwarded-For": ["127.0.0.1"]}
        with self.assertRaisesRegex(GatewayRequestError, "request_header_not_allowed"):
            build_envelope(event, public_base_url=PUBLIC_BASE)

    def test_rejects_header_values_the_bridge_cannot_encode(self):
        with self.assertRaisesRegex(GatewayRequestError, "request_header_not_allowed"):
            build_envelope(
                api_event(headers={"X-Payment": "proof-\U0001f600"}),
                public_base_url=PUBLIC_BASE,
            )

    def test_rejects_invalid_public_base_url(self):
        for value in (
            "http://gateway.example.test",
            "https://user@gateway.example.test",
            "https://gateway.example.test/?query=1",
            "https://gateway.example.test/#fragment",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                GatewayRequestError, "invalid_public_base_url"
            ):
                build_envelope(api_event(), public_base_url=value)
