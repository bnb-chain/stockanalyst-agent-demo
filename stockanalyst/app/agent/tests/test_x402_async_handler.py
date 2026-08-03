from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from unittest.mock import AsyncMock, Mock, patch

from stockanalyst.app.agent.x402_handler import X402Handler
from stockanalyst.app.agent.x402_job_service import (
    CreateJobResult,
    JobView,
    X402JobError,
)


JOB_ID = "x402_" + "a" * 32
EXPIRES_AT = 1_785_945_600_123
SUPPORTED_EXTRA = {
    "name": "U",
    "version": "1",
    "assetTransferMethod": "eip3009",
    "signerAddress": "0x1111111111111111111111111111111111111111",
}


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> dict:
        return json.loads(self.body)


async def call_handler(
    handler: X402Handler,
    *,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
    body_chunks: list[bytes] | None = None,
    scope_overrides: dict | None = None,
) -> Response:
    sent: list[dict] = []
    if body_chunks is None:
        body_chunks = [
            json.dumps(json_body).encode() if json_body is not None else b""
        ]
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index + 1 < len(body_chunks),
        }
        for index, chunk in enumerate(body_chunks)
    ]

    async def receive() -> dict:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [
            (name.encode(), value.encode())
            for name, value in (headers or {}).items()
        ],
    }
    if scope_overrides:
        scope.update(scope_overrides)
    await handler(scope, receive, send)
    start = next(item for item in sent if item["type"] == "http.response.start")
    response_body = b"".join(
        item.get("body", b"")
        for item in sent
        if item["type"] == "http.response.body"
    )
    return Response(
        status=start["status"],
        headers={
            name.decode().lower(): value.decode()
            for name, value in start.get("headers", [])
        },
        body=response_body,
    )


async def call_disconnected_handler(
    handler: X402Handler,
    *,
    messages: list[dict],
) -> AsyncMock:
    pending = list(messages)

    async def receive() -> dict:
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    send = AsyncMock(
        side_effect=AssertionError("must not send after http.disconnect")
    )

    await handler(
        {
            "type": "http",
            "method": "POST",
            "path": "/x402/analyze/async",
            "query_string": b"",
            "headers": [(b"x-payment", b"proof")],
        },
        receive,
        send,
    )
    return send


def make_handler(service=None, *, b402_client=None) -> X402Handler:
    if b402_client is None:
        b402_client = AsyncMock()
        b402_client.payment_extra.return_value = SUPPORTED_EXTRA
    return X402Handler(
        AsyncMock(),
        free_stream_work=Mock(),
        job_service=service,
        b402_client=b402_client,
    )


class X402AsyncHandlerTests(unittest.IsolatedAsyncioTestCase):
    def assert_private_no_store(
        self,
        response: Response,
        *,
        token_authenticated: bool,
    ) -> None:
        self.assertEqual(
            response.headers.get("cache-control"),
            "private, no-store",
        )
        if token_authenticated:
            self.assertEqual(
                response.headers.get("vary", "").lower(),
                "x-job-token",
            )
        else:
            self.assertNotIn("vary", response.headers)

    async def test_async_create_returns_accepted_handle(self) -> None:
        service = AsyncMock()
        service.create_job.return_value = CreateJobResult(
            job_id=JOB_ID,
            job_token="token",
            status="queued",
            expires_at=EXPIRES_AT,
        )

        response = await call_handler(
            make_handler(service),
            method="POST",
            path="/x402/analyze/async",
            headers={"x-payment": "proof"},
            json_body={"symbols": ["AAPL"]},
        )

        self.assertEqual(response.status, 202)
        self.assert_private_no_store(response, token_authenticated=False)
        self.assertEqual(response.headers["location"], f"/x402/jobs/{JOB_ID}")
        self.assertEqual(response.headers["retry-after"], "10")
        self.assertEqual(response.json, {
            "jobId": JOB_ID,
            "jobToken": "token",
            "status": "queued",
            "statusUrl": f"/x402/jobs/{JOB_ID}",
            "expiresAt": EXPIRES_AT,
        })
        service.create_job.assert_awaited_once_with(
            "proof",
            {"symbols": ["AAPL"]},
        )

    async def test_async_create_without_payment_returns_challenge(self) -> None:
        service = AsyncMock()

        response = await call_handler(
            make_handler(service),
            method="POST",
            path="/x402/analyze/async",
            json_body={"symbols": ["AAPL"]},
        )

        self.assertEqual(response.status, 402)
        self.assert_private_no_store(response, token_authenticated=False)
        self.assertIn("x-payment-required", response.headers)
        service.create_job.assert_not_awaited()

    async def test_async_challenge_uses_trusted_public_resource_url(self) -> None:
        service = AsyncMock()

        response = await call_handler(
            make_handler(service),
            method="POST",
            path="/x402/analyze/async",
            json_body={"symbols": ["AAPL"]},
            scope_overrides={
                "x402_public_base_url": "https://api.example.test/testnet",
            },
        )

        self.assertEqual(response.status, 402)
        self.assertEqual(
            response.json["paymentRequired"]["resource"]["url"],
            "https://api.example.test/testnet/x402/analyze/async",
        )
        self.assertEqual(
            response.json["paymentRequired"]["accepts"][0]["extra"],
            SUPPORTED_EXTRA,
        )

    async def test_async_challenge_fails_closed_when_supported_is_unavailable(
        self,
    ) -> None:
        b402_client = AsyncMock()
        b402_client.payment_extra.side_effect = RuntimeError(
            "credential detail must not leak"
        )

        response = await call_handler(
            make_handler(AsyncMock(), b402_client=b402_client),
            method="POST",
            path="/x402/analyze/async",
            json_body={"symbols": ["AAPL"]},
        )

        self.assertEqual(response.status, 503)
        self.assertEqual(response.json, {
            "errorCode": "payment_backend_unavailable",
            "retryable": True,
        })
        self.assertNotIn("credential", response.body.decode())

    async def test_async_create_rejects_invalid_json(self) -> None:
        service = AsyncMock()

        response = await call_handler(
            make_handler(service),
            method="POST",
            path="/x402/analyze/async",
            headers={"x-payment": "proof"},
            body_chunks=[b"{not-json"],
        )

        self.assertEqual(response.status, 400)
        self.assert_private_no_store(response, token_authenticated=False)
        self.assertEqual(response.json["errorCode"], "invalid_request")
        service.create_job.assert_not_awaited()

    async def test_async_create_rejects_non_object_json(self) -> None:
        service = AsyncMock()

        response = await call_handler(
            make_handler(service),
            method="POST",
            path="/x402/analyze/async",
            headers={"x-payment": "proof"},
            body_chunks=[b"[]"],
        )

        self.assertEqual(response.status, 400)
        self.assertEqual(response.json["errorCode"], "invalid_request")
        service.create_job.assert_not_awaited()

    async def test_async_create_stops_when_body_exceeds_256_kib(self) -> None:
        service = AsyncMock()
        extra_receive = AsyncMock(return_value={"type": "http.disconnect"})
        chunks = [
            b"x" * (256 * 1024),
            b"x",
            b"must-not-be-read",
        ]
        sent: list[dict] = []
        receive_count = 0

        async def receive() -> dict:
            nonlocal receive_count
            receive_count += 1
            if chunks:
                chunk = chunks.pop(0)
                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": True,
                }
            return await extra_receive()

        async def send(message: dict) -> None:
            sent.append(message)

        await make_handler(service)(
            {
                "type": "http",
                "method": "POST",
                "path": "/x402/analyze/async",
                "query_string": b"",
                "headers": [(b"x-payment", b"proof")],
            },
            receive,
            send,
        )

        start = next(item for item in sent if item["type"] == "http.response.start")
        self.assertEqual(start["status"], 413)
        response_headers = {
            name.decode().lower(): value.decode()
            for name, value in start.get("headers", [])
        }
        self.assertEqual(
            response_headers.get("cache-control"),
            "private, no-store",
        )
        self.assertEqual(receive_count, 2)
        self.assertEqual(len(chunks), 1)
        service.create_job.assert_not_awaited()

    async def test_async_create_rejects_disconnect_after_partial_body(self) -> None:
        service = AsyncMock()

        send = await call_disconnected_handler(
            make_handler(service),
            messages=[
                {
                    "type": "http.request",
                    "body": b'{"symbols":["AAPL"]}',
                    "more_body": True,
                },
                {"type": "http.disconnect"},
            ],
        )

        send.assert_not_awaited()
        service.create_job.assert_not_awaited()

    async def test_async_create_rejects_disconnect_before_body(self) -> None:
        service = AsyncMock()

        send = await call_disconnected_handler(
            make_handler(service),
            messages=[{"type": "http.disconnect"}],
        )

        send.assert_not_awaited()
        service.create_job.assert_not_awaited()

    async def test_async_create_maps_payment_rejection_to_402(self) -> None:
        service = AsyncMock()
        service.create_job.side_effect = X402JobError("payment_rejected")

        response = await call_handler(
            make_handler(service),
            method="POST",
            path="/x402/analyze/async",
            headers={"x-payment": "proof"},
            json_body={"symbols": ["AAPL"]},
        )

        self.assertEqual(response.status, 402)
        self.assertEqual(response.json["errorCode"], "payment_rejected")

    async def test_async_create_maps_invalid_request_to_400(self) -> None:
        service = AsyncMock()
        service.create_job.side_effect = X402JobError("invalid_request")

        response = await call_handler(
            make_handler(service),
            method="POST",
            path="/x402/analyze/async",
            headers={"x-payment": "proof"},
            json_body={"symbols": []},
        )

        self.assertEqual(response.status, 400)
        self.assertEqual(response.json["errorCode"], "invalid_request")

    async def test_paused_creation_is_503_but_query_remains_active(self) -> None:
        service = AsyncMock()
        service.create_job.side_effect = X402JobError(
            "async_jobs_paused",
            retryable=True,
        )
        service.get_job.return_value = JobView(
            job_id=JOB_ID,
            status="queued",
            expires_at=EXPIRES_AT,
        )
        service.resume_job.return_value = JobView(
            job_id=JOB_ID,
            status="queued",
            expires_at=EXPIRES_AT,
        )

        create = await call_handler(
            make_handler(service),
            method="POST",
            path="/x402/analyze/async",
            headers={"x-payment": "proof"},
            json_body={"symbols": ["AAPL"]},
        )
        query = await call_handler(
            make_handler(service),
            method="GET",
            path=f"/x402/jobs/{JOB_ID}",
            headers={"x-job-token": "token"},
        )
        resume = await call_handler(
            make_handler(service),
            method="POST",
            path=f"/x402/jobs/{JOB_ID}/resume",
            headers={"x-job-token": "token"},
        )

        self.assertEqual(create.status, 503)
        self.assert_private_no_store(create, token_authenticated=False)
        self.assertEqual(create.json, {
            "errorCode": "async_jobs_paused",
            "retryable": True,
        })
        self.assertEqual(query.status, 200)
        self.assert_private_no_store(query, token_authenticated=True)
        self.assertEqual(resume.status, 202)
        self.assert_private_no_store(resume, token_authenticated=True)

    async def test_invalid_token_and_missing_job_have_identical_404(self) -> None:
        service = AsyncMock()
        service.get_job.side_effect = X402JobError("job_not_found")

        invalid = await call_handler(
            make_handler(service),
            method="GET",
            path=f"/x402/jobs/{JOB_ID}",
            headers={"x-job-token": "bad-token"},
        )
        missing = await call_handler(
            make_handler(service),
            method="GET",
            path=f"/x402/jobs/{JOB_ID}",
            headers={"x-job-token": "another-token"},
        )

        self.assertEqual(
            (invalid.status, invalid.body),
            (missing.status, missing.body),
        )
        self.assertEqual(invalid.status, 404)
        self.assert_private_no_store(invalid, token_authenticated=True)
        self.assert_private_no_store(missing, token_authenticated=True)

    async def test_job_token_header_is_case_insensitive(self) -> None:
        service = AsyncMock()
        service.get_job.return_value = JobView(
            job_id=JOB_ID,
            status="queued",
            expires_at=EXPIRES_AT,
        )

        response = await call_handler(
            make_handler(service),
            method="GET",
            path=f"/x402/jobs/{JOB_ID}",
            headers={"X-Job-Token": "token"},
        )

        self.assertEqual(response.status, 200)
        service.get_job.assert_awaited_once_with(JOB_ID, "token")

    async def test_missing_job_token_is_passed_as_empty_for_hidden_404(self) -> None:
        service = AsyncMock()
        service.get_job.side_effect = X402JobError("job_not_found")

        response = await call_handler(
            make_handler(service),
            method="GET",
            path=f"/x402/jobs/{JOB_ID}",
        )

        self.assertEqual(response.status, 404)
        service.get_job.assert_awaited_once_with(JOB_ID, "")

    async def test_malformed_job_id_returns_404_without_service_call(self) -> None:
        service = AsyncMock()

        response = await call_handler(
            make_handler(service),
            method="GET",
            path="/x402/jobs/x402_not-hex",
            headers={"x-job-token": "token"},
        )

        self.assertEqual(response.status, 404)
        self.assert_private_no_store(response, token_authenticated=True)
        service.get_job.assert_not_awaited()

    async def test_queued_and_running_views_return_retry_after(self) -> None:
        for status in ("queued", "running"):
            with self.subTest(status=status):
                service = AsyncMock()
                service.get_job.return_value = JobView(
                    job_id=JOB_ID,
                    status=status,
                    expires_at=EXPIRES_AT,
                )

                response = await call_handler(
                    make_handler(service),
                    method="GET",
                    path=f"/x402/jobs/{JOB_ID}",
                    headers={"x-job-token": "token"},
                )

                self.assertEqual(response.status, 200)
                self.assert_private_no_store(
                    response,
                    token_authenticated=True,
                )
                self.assertEqual(response.headers["retry-after"], "10")
                self.assertEqual(response.json["status"], status)

    async def test_failed_view_includes_stable_failure_fields(self) -> None:
        service = AsyncMock()
        service.get_job.return_value = JobView(
            job_id=JOB_ID,
            status="failed",
            expires_at=EXPIRES_AT,
            error_code="analysis_timeout",
            retryable=True,
        )

        response = await call_handler(
            make_handler(service),
            method="GET",
            path=f"/x402/jobs/{JOB_ID}",
            headers={"x-job-token": "token"},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.json, {
            "jobId": JOB_ID,
            "status": "failed",
            "expiresAt": EXPIRES_AT,
            "errorCode": "analysis_timeout",
            "retryable": True,
        })
        self.assertNotIn("retry-after", response.headers)

    async def test_succeeded_view_includes_download_fields(self) -> None:
        service = AsyncMock()
        service.get_job.return_value = JobView(
            job_id=JOB_ID,
            status="succeeded",
            expires_at=EXPIRES_AT,
            download_url="https://signed.example/report",
            download_url_expires_at=1_785_343_400_123,
        )

        response = await call_handler(
            make_handler(service),
            method="GET",
            path=f"/x402/jobs/{JOB_ID}",
            headers={"x-job-token": "token"},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.json["downloadUrl"],
            "https://signed.example/report",
        )
        self.assertEqual(
            response.json["downloadUrlExpiresAt"],
            1_785_343_400_123,
        )
        self.assertNotIn("retry-after", response.headers)

    async def test_resume_returns_accepted_view(self) -> None:
        service = AsyncMock()
        service.resume_job.return_value = JobView(
            job_id=JOB_ID,
            status="queued",
            expires_at=EXPIRES_AT,
        )

        response = await call_handler(
            make_handler(service),
            method="POST",
            path=f"/x402/jobs/{JOB_ID}/resume",
            headers={"x-job-token": "token"},
        )

        self.assertEqual(response.status, 202)
        self.assert_private_no_store(response, token_authenticated=True)
        self.assertEqual(response.headers["retry-after"], "10")
        self.assertEqual(response.json["status"], "queued")
        service.resume_job.assert_awaited_once_with(JOB_ID, "token")

    async def test_conflicts_map_to_409(self) -> None:
        for code in ("job_conflict", "attempts_exhausted"):
            with self.subTest(code=code):
                service = AsyncMock()
                service.resume_job.side_effect = X402JobError(code)

                response = await call_handler(
                    make_handler(service),
                    method="POST",
                    path=f"/x402/jobs/{JOB_ID}/resume",
                    headers={"x-job-token": "token"},
                )

                self.assertEqual(response.status, 409)
                self.assert_private_no_store(
                    response,
                    token_authenticated=True,
                )
                self.assertEqual(response.json["errorCode"], code)

    async def test_expired_job_maps_to_410(self) -> None:
        service = AsyncMock()
        service.get_job.side_effect = X402JobError("job_expired")

        response = await call_handler(
            make_handler(service),
            method="GET",
            path=f"/x402/jobs/{JOB_ID}",
            headers={"x-job-token": "token"},
        )

        self.assertEqual(response.status, 410)
        self.assert_private_no_store(response, token_authenticated=True)
        self.assertEqual(response.json["errorCode"], "job_expired")

    async def test_unexpected_service_failure_is_generic_503(self) -> None:
        service = AsyncMock()
        service.get_job.side_effect = RuntimeError(
            "secret bucket/key/token detail"
        )

        with self.assertLogs("seller-agent.x402", level="WARNING") as logs:
            response = await call_handler(
                make_handler(service),
                method="GET",
                path=f"/x402/jobs/{JOB_ID}",
                headers={"x-job-token": "token"},
            )

        self.assertEqual(response.status, 503)
        self.assert_private_no_store(response, token_authenticated=True)
        self.assertEqual(response.json, {
            "errorCode": "job_service_unavailable",
            "retryable": True,
        })
        self.assertNotIn(b"secret", response.body)
        self.assertIn("dependency=RuntimeError", logs.output[0])
        self.assertNotIn("secret bucket/key/token detail", logs.output[0])

    async def test_new_routes_are_404_when_service_is_not_configured(self) -> None:
        create = await call_handler(
            make_handler(),
            method="POST",
            path="/x402/analyze/async",
            headers={"x-payment": "proof"},
            json_body={"symbols": ["AAPL"]},
        )
        query = await call_handler(
            make_handler(),
            method="GET",
            path=f"/x402/jobs/{JOB_ID}",
            headers={"x-job-token": "token"},
        )

        self.assertEqual(create.status, 404)
        self.assert_private_no_store(create, token_authenticated=False)
        self.assertEqual(query.status, 404)
        self.assert_private_no_store(query, token_authenticated=True)

    async def test_retired_paid_sse_routes_return_404(self) -> None:
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                response = await call_handler(
                    make_handler(AsyncMock()),
                    method=method,
                    path="/x402/analyze",
                    json_body={"symbols": ["AAPL"]},
                )

                self.assertEqual(response.status, 404)
                self.assertNotIn(
                    "GET  /x402/analyze",
                    response.json["x402_routes"],
                )
                self.assertNotIn(
                    "POST /x402/analyze",
                    response.json["x402_routes"],
                )

    async def test_free_post_still_uses_original_handler(self) -> None:
        handler = make_handler(AsyncMock())
        with patch.object(
            handler,
            "_handle_free",
            new=AsyncMock(),
        ) as free:
            await handler(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/x402/free",
                    "headers": [],
                },
                AsyncMock(),
                AsyncMock(),
            )

        free.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
