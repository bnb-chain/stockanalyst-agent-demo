from __future__ import annotations

import base64
import unittest
from unittest.mock import AsyncMock

from x402_envelope import EnvelopeError, dispatch_x402_envelope


PUBLIC_BASE = "https://gateway.example.test/stages/testnet"
JOB_ID = "x402_" + "a" * 32
REQUEST_ID = "x402gw_" + "a" * 64


def request_envelope(**overrides) -> dict:
    envelope = {
        "version": 1,
        "requestId": REQUEST_ID,
        "method": "GET",
        "path": "/x402/price",
        "headers": {"accept": "application/json"},
        "bodyBase64": "",
        "publicBaseUrl": PUBLIC_BASE,
    }
    envelope.update(overrides)
    return envelope


async def recording_app(scope, receive, send) -> None:
    await receive()
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"application/json"),
            (b"set-cookie", b"must-not-cross-the-bridge"),
        ],
    })
    await send({"type": "http.response.body", "body": b'{"ok":true}', "more_body": False})


async def payment_required_app(scope, receive, send) -> None:
    await receive()
    await send({
        "type": "http.response.start",
        "status": 402,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({
        "type": "http.response.body",
        "body": b'{"error":"Payment Required"}',
        "more_body": False,
    })


async def streaming_app(scope, receive, send) -> None:
    await receive()
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"text/event-stream")],
    })
    await send({"type": "http.response.body", "body": b"event: update\n\n", "more_body": True})


class X402EnvelopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_accepts_integer_envelope_version_only(self) -> None:
        result = await dispatch_x402_envelope(
            recording_app,
            request_envelope(version=1),
            expected_public_base_url=PUBLIC_BASE,
        )
        self.assertEqual(result["status"], 200)

        for version in ("1", "envelope-v1", True, 1.0, 2):
            with self.subTest(version=version), self.assertRaisesRegex(
                EnvelopeError, "invalid_envelope_version"
            ):
                await dispatch_x402_envelope(
                    recording_app,
                    request_envelope(version=version),
                    expected_public_base_url=PUBLIC_BASE,
                )

    async def test_dispatch_requires_bounded_gateway_request_id(self) -> None:
        for request_id in (
            "request-123",
            "x402gw_" + "A" * 64,
            "x402gw_" + "a" * 63,
            "x402gw_" + "a" * 65,
            "x402gw_" + "a" * 10_000,
        ):
            with self.subTest(request_id_length=len(request_id)), self.assertRaisesRegex(
                EnvelopeError, "invalid_request_id"
            ):
                await dispatch_x402_envelope(
                    recording_app,
                    request_envelope(requestId=request_id),
                    expected_public_base_url=PUBLIC_BASE,
                )

    async def test_dispatch_preserves_402_response(self) -> None:
        envelope = request_envelope(method="POST", path="/x402/analyze/async")

        result = await dispatch_x402_envelope(
            payment_required_app,
            envelope,
            expected_public_base_url=PUBLIC_BASE,
        )

        self.assertEqual(result["status"], 402)
        self.assertEqual(result["requestId"], envelope["requestId"])
        self.assertEqual(
            base64.b64decode(result["bodyBase64"]),
            b'{"error":"Payment Required"}',
        )

    async def test_dispatch_rejects_arbitrary_path(self) -> None:
        with self.assertRaisesRegex(EnvelopeError, "route_not_allowed"):
            await dispatch_x402_envelope(
                recording_app,
                request_envelope(path="/admin"),
                expected_public_base_url=PUBLIC_BASE,
            )

    async def test_dispatch_rejects_public_base_mismatch(self) -> None:
        envelope = request_envelope(publicBaseUrl="https://evil.example")
        with self.assertRaisesRegex(EnvelopeError, "public_base_mismatch"):
            await dispatch_x402_envelope(
                recording_app,
                envelope,
                expected_public_base_url=PUBLIC_BASE,
            )

    async def test_dispatch_rejects_streaming_response(self) -> None:
        with self.assertRaisesRegex(EnvelopeError, "streaming_not_supported"):
            await dispatch_x402_envelope(
                streaming_app,
                request_envelope(),
                expected_public_base_url=PUBLIC_BASE,
            )

    async def test_dispatch_only_allows_x402_method_path_pairs(self) -> None:
        for method, path in (
            ("POST", "/x402/price"),
            ("GET", "/x402/analyze/async"),
            ("POST", f"/x402/jobs/{JOB_ID}"),
            ("GET", f"/x402/jobs/{JOB_ID}/resume"),
            ("GET", "/x402/free"),
        ):
            with self.subTest(method=method, path=path), self.assertRaisesRegex(
                EnvelopeError, "route_not_allowed"
            ):
                await dispatch_x402_envelope(
                    recording_app,
                    request_envelope(method=method, path=path),
                    expected_public_base_url=PUBLIC_BASE,
                )

    async def test_dispatch_allows_job_method_path_pairs(self) -> None:
        for method, path in (
            ("GET", f"/x402/jobs/{JOB_ID}"),
            ("POST", f"/x402/jobs/{JOB_ID}/resume"),
        ):
            with self.subTest(method=method, path=path):
                result = await dispatch_x402_envelope(
                    recording_app,
                    request_envelope(method=method, path=path),
                    expected_public_base_url=PUBLIC_BASE,
                )
                self.assertEqual(result["status"], 200)

    async def test_dispatch_rejects_nonempty_get_bodies(self) -> None:
        for path in ("/x402/price", f"/x402/jobs/{JOB_ID}"):
            with self.subTest(path=path), self.assertRaisesRegex(
                EnvelopeError, "get_request_body_not_allowed"
            ):
                await dispatch_x402_envelope(
                    recording_app,
                    request_envelope(
                        path=path,
                        bodyBase64=base64.b64encode(b"unexpected").decode(),
                    ),
                    expected_public_base_url=PUBLIC_BASE,
                )

    async def test_dispatch_validates_https_public_base_urls(self) -> None:
        valid_stage_base = "https://gateway.example.test/prod/v1"
        result = await dispatch_x402_envelope(
            recording_app,
            request_envelope(publicBaseUrl=valid_stage_base),
            expected_public_base_url=valid_stage_base,
        )
        self.assertEqual(result["status"], 200)

        for invalid_base in (
            "http://gateway.example.test/prod",
            "https://user:pass@gateway.example.test/prod",
            "https://gateway.example.test/prod?stage=1",
            "https://gateway.example.test/prod#fragment",
        ):
            with self.subTest(source="configured", base=invalid_base), self.assertRaisesRegex(
                EnvelopeError, "invalid_public_base_url"
            ):
                await dispatch_x402_envelope(
                    recording_app,
                    request_envelope(),
                    expected_public_base_url=invalid_base,
                )
            with self.subTest(source="supplied", base=invalid_base), self.assertRaisesRegex(
                EnvelopeError, "invalid_public_base_url"
            ):
                await dispatch_x402_envelope(
                    recording_app,
                    request_envelope(publicBaseUrl=invalid_base),
                    expected_public_base_url=PUBLIC_BASE,
                )

    async def test_dispatch_rejects_non_lowercase_or_unsafe_request_headers(self) -> None:
        for headers in (
            {"Accept": "application/json"},
            {"x-forwarded-for": "127.0.0.1"},
            {"host": "evil.example"},
            {"authorization": "Bearer caller-token"},
        ):
            with self.subTest(headers=headers), self.assertRaisesRegex(
                EnvelopeError, "request_header_not_allowed"
            ):
                await dispatch_x402_envelope(
                    recording_app,
                    request_envelope(headers=headers),
                    expected_public_base_url=PUBLIC_BASE,
                )

    async def test_dispatch_rejects_invalid_or_oversize_base64_body(self) -> None:
        for body in ("not base64!", base64.b64encode(b"x" * (256 * 1024 + 1)).decode()):
            with self.subTest(body_length=len(body)), self.assertRaisesRegex(
                EnvelopeError, "(invalid_body_base64|request_too_large)"
            ):
                await dispatch_x402_envelope(
                    recording_app,
                    request_envelope(bodyBase64=body),
                    expected_public_base_url=PUBLIC_BASE,
                )

    async def test_dispatch_filters_response_headers_to_allowlist(self) -> None:
        result = await dispatch_x402_envelope(
            recording_app,
            request_envelope(),
            expected_public_base_url=PUBLIC_BASE,
        )

        self.assertEqual(result["headers"], {"content-type": "application/json"})

    async def test_dispatch_rejects_oversize_response(self) -> None:
        async def oversize_app(scope, receive, send) -> None:
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({
                "type": "http.response.body",
                "body": b"x" * (2 * 1024 * 1024 + 1),
                "more_body": False,
            })

        with self.assertRaisesRegex(EnvelopeError, "response_too_large"):
            await dispatch_x402_envelope(
                oversize_app,
                request_envelope(),
                expected_public_base_url=PUBLIC_BASE,
            )

    async def test_dispatch_rejects_invalid_response_lifecycle(self) -> None:
        async def duplicate_start(scope, receive, send) -> None:
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.start", "status": 200, "headers": []})

        async def missing_start(scope, receive, send) -> None:
            await receive()
            await send({"type": "http.response.body", "body": b"oops", "more_body": False})

        for app, error in (
            (duplicate_start, "duplicate_response_start"),
            (missing_start, "missing_response_start"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(EnvelopeError, error):
                await dispatch_x402_envelope(
                    app,
                    request_envelope(),
                    expected_public_base_url=PUBLIC_BASE,
                )


class X402EnvelopeExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_rejects_invalid_public_base_at_startup(self) -> None:
        from executor import SellerAgentExecutor

        with self.assertRaisesRegex(EnvelopeError, "invalid_public_base_url"):
            SellerAgentExecutor(
                run_work=AsyncMock(),
                generator="stockanalyst",
                network="bsc-testnet",
                x402_app=payment_required_app,
                x402_public_base_url="http://gateway.example.test/stages/testnet",
            )

    async def test_hidden_envelope_skill_dispatches_without_llm(self) -> None:
        from executor import SellerAgentExecutor

        executor = SellerAgentExecutor(
            run_work=AsyncMock(),
            generator="stockanalyst",
            network="bsc-testnet",
            x402_app=payment_required_app,
            x402_public_base_url=PUBLIC_BASE,
        )

        result = await executor.dispatch_skill({
            "skill": "x402_http_envelope",
            "envelope": request_envelope(),
        })

        self.assertEqual(result["status"], 402)
        executor._run_work.assert_not_awaited()
