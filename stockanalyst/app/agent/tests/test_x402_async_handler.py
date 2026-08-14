from __future__ import annotations

import base64
import hashlib
import json
import unittest
from dataclasses import dataclass
from unittest.mock import AsyncMock, Mock, patch

from eth_account import Account
from eth_account.messages import encode_typed_data
from stockanalyst.app.agent import x402_handler as handler_module
from stockanalyst.app.agent import x402_verify
from stockanalyst.app.agent.tests.test_x402_verify import (
    NOW as SIGNED_NOW,
)
from stockanalyst.app.agent.tests.test_x402_verify import (
    signed_free_proof,
    signed_proof,
)
from stockanalyst.app.agent.x402_handler import X402Handler
from stockanalyst.app.agent.x402_job_service import (
    CreateJobResult,
    JobView,
    SettlementIndeterminate,
    X402JobError,
)
from stockanalyst.app.agent.x402_settlement import SettlementOutcome
from stockanalyst.app.agent.x402_tokens import (
    TOKENS,
    U_TOKEN,
    USD1_TOKEN,
    USDC_TOKEN,
    USDT_TOKEN,
)

JOB_ID = "x402_" + "a" * 32
EXPIRES_AT = 1_785_945_600_123
PAID_PRICE_WEI = 210_000_000_000_000_000
SUPPORTED_EXTRA = {
    "name": U_TOKEN.domain_name,
    "version": "1",
    "assetTransferMethod": "eip3009",
    "signerAddress": "0x1111111111111111111111111111111111111111",
}
USD1_SUPPORTED_EXTRA = {
    "name": USD1_TOKEN.domain_name,
    "version": USD1_TOKEN.domain_version,
    "assetTransferMethod": "eip3009",
    "signerAddress": "0x2222222222222222222222222222222222222222",
}
USDC_SUPPORTED_EXTRA = {
    "name": USDC_TOKEN.domain_name,
    "version": USDC_TOKEN.domain_version,
    "assetTransferMethod": "permit2-exact",
    "signerAddress": "0x3333333333333333333333333333333333333333",
    "spenderAddress": "0x4444444444444444444444444444444444444444",
}
USDT_SUPPORTED_EXTRA = {
    "name": USDT_TOKEN.domain_name,
    "version": USDT_TOKEN.domain_version,
    "assetTransferMethod": "permit2-exact",
    "signerAddress": "0x5555555555555555555555555555555555555555",
    "spenderAddress": "0x6666666666666666666666666666666666666666",
}
SUPPORTED_EXTRAS = {
    U_TOKEN.symbol: SUPPORTED_EXTRA,
    USD1_TOKEN.symbol: USD1_SUPPORTED_EXTRA,
    USDC_TOKEN.symbol: USDC_SUPPORTED_EXTRA,
    USDT_TOKEN.symbol: USDT_SUPPORTED_EXTRA,
}
SUPPORTED_ASSETS = [
    {
        "symbol": token.symbol,
        "asset": token.address,
        "decimals": token.decimals,
        "transferMethod": token.transfer_method,
    }
    for token in TOKENS
]


def promo_wallet_header(
    request: dict,
    *,
    now: int,
    expires_at: int | None = None,
) -> tuple[str, str]:
    account = Account.create("promo-handler-test")
    nonce = "0x" + "34" * 32
    expires_at = now + 600 if expires_at is None else expires_at
    body = json.dumps(request).encode()
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "PromoAuthorization": [
                {"name": "address", "type": "address"},
                {"name": "method", "type": "string"},
                {"name": "path", "type": "string"},
                {"name": "bodyHash", "type": "bytes32"},
                {"name": "nonce", "type": "bytes32"},
                {"name": "expiresAt", "type": "uint64"},
            ],
        },
        "primaryType": "PromoAuthorization",
        "domain": {
            "name": "Stock Analyst Promo",
            "version": "1",
            "chainId": 56,
        },
        "message": {
            "address": account.address,
            "method": "POST",
            "path": "/x402/analyze/async",
            "bodyHash": "0x" + hashlib.sha256(body).hexdigest(),
            "nonce": nonce,
            "expiresAt": expires_at,
        },
    }
    signature = Account.sign_message(
        encode_typed_data(full_message=typed),
        account.key,
    ).signature.hex()
    envelope = {
        "version": 1,
        "address": account.address,
        "nonce": nonce,
        "expiresAt": expires_at,
        "signature": "0x" + signature.removeprefix("0x"),
    }
    return (
        base64.urlsafe_b64encode(
            json.dumps(envelope, separators=(",", ":")).encode()
        ).decode().rstrip("="),
        account.address.lower(),
    )


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> dict:
        return json.loads(self.body)


def decode_header(response: Response, name: str) -> dict[str, object]:
    return json.loads(base64.b64decode(response.headers[name], validate=True))


def settlement_proof(token=U_TOKEN) -> str:
    proof = {
        "x402Version": 2,
        "accepted": {
            "scheme": "exact",
            "network": "eip155:56",
            "amount": str(PAID_PRICE_WEI),
            "asset": token.address,
            "payTo": "0x7777777777777777777777777777777777777777",
            "maxTimeoutSeconds": 600,
            "extra": SUPPORTED_EXTRAS[token.symbol],
        },
        "payload": {"authorization": {}},
    }
    return base64.b64encode(json.dumps(proof).encode()).decode()


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
            "headers": [(b"payment-signature", b"proof")],
        },
        receive,
        send,
    )
    return send


def make_handler(service=None, *, b402_client=None) -> X402Handler:
    if b402_client is None:
        b402_client = AsyncMock()
        b402_client.payment_extras.return_value = SUPPORTED_EXTRAS
    if service is not None and not isinstance(
        getattr(service, "promo_free", None), bool
    ):
        service.promo_free = False
    return X402Handler(
        AsyncMock(),
        free_work=Mock(),
        job_service=service,
        b402_client=b402_client,
    )


async def free_report_work(symbol: str):
    yield "progress", {"stage": "collecting"}
    yield "report", {"content": f"# {symbol} report", "format": "markdown"}
    yield "done", {}


class X402AsyncHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_b402_settlement_adapter_dispatches_requested_mode(self) -> None:
        client = AsyncMock()
        verified = SettlementOutcome("settled", transaction="0xverified")
        resumed = SettlementOutcome("settled", transaction="0xresumed")
        client.verify_and_settle.return_value = verified
        client.settle_only.return_value = resumed

        with patch.object(handler_module, "_B402_CLIENT", client):
            self.assertIs(
                await handler_module._settle_via_facilitator(
                    settlement_proof(),
                    "verify-and-settle",
                ),
                verified,
            )
            self.assertIs(
                await handler_module._settle_via_facilitator(
                    settlement_proof(),
                    "settle-only",
                ),
                resumed,
            )

        client.verify_and_settle.assert_awaited_once()
        client.settle_only.assert_awaited_once()

    async def test_b402_pending_settlement_remains_typed_pending(self) -> None:
        pending = SettlementOutcome("pending", transaction="b402-pending-id")
        client = AsyncMock()
        client.verify_and_settle.return_value = pending

        with patch.object(handler_module, "_B402_CLIENT", client):
            outcome = await handler_module._settle_via_facilitator(
                settlement_proof(USDT_TOKEN),
                "verify-and-settle",
            )

        self.assertIs(outcome, pending)
        self.assertEqual(outcome.status, "pending")

    async def test_b402_indeterminate_settlement_is_not_treated_as_settled(
        self,
    ) -> None:
        client = AsyncMock()
        client.verify_and_settle.side_effect = handler_module.B402IndeterminateError(
            "response lost"
        )

        with (
            patch.object(handler_module, "_B402_CLIENT", client),
            self.assertRaises(SettlementIndeterminate),
        ):
            await handler_module._settle_via_facilitator(
                settlement_proof(),
                "verify-and-settle",
            )

    async def test_non_b402_backends_reject_permit2(self) -> None:
        generic = AsyncMock(
            side_effect=AssertionError("generic must not receive Permit2")
        )
        for facilitator_url, demo_mode in (
            ("https://facilitator.example.test", False),
            ("", True),
        ):
            with (
                self.subTest(
                    facilitator_url=facilitator_url,
                    demo_mode=demo_mode,
                ),
                patch.object(handler_module, "_B402_CLIENT", None),
                patch.object(handler_module, "FACILITATOR_URL", facilitator_url),
                patch.object(handler_module, "X402_DEMO_MODE", demo_mode),
                patch.object(handler_module, "_settle_generic", generic),
            ):
                outcome = await handler_module._settle_via_facilitator(
                    settlement_proof(USDT_TOKEN),
                    "verify-and-settle",
                )

            self.assertEqual(outcome.status, "rejected")
            self.assertIsNone(outcome.transaction)
        generic.assert_not_awaited()

    async def test_price_exposes_dedicated_b402_pay_to(self) -> None:
        pay_to = "0x15958aad30b758dAbfbB9788Da69dfcd56e89078"
        with patch.object(x402_verify, "B402_PAY_TO_ADDRESS", pay_to):
            response = await call_handler(
                make_handler(), method="GET", path="/x402/price"
            )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json["payTo"], pay_to.lower())
        self.assertEqual(
            [item["asset"] for item in response.json["accepts"]],
            [token.address for token in TOKENS],
        )
        self.assertEqual(
            [item["amount"] for item in response.json["accepts"]],
            [str(PAID_PRICE_WEI)] * len(TOKENS),
        )
        self.assertEqual(
            [
                item["extra"]["assetTransferMethod"]
                for item in response.json["accepts"]
            ],
            ["eip3009", "eip3009", "permit2-exact", "permit2-exact"],
        )
        self.assertEqual(response.json["asset"], U_TOKEN.address)
        self.assertEqual(response.json["signingScheme"], "eip3009")
        self.assertEqual(
            response.json["signingSchemes"],
            ["eip3009", "permit2-exact"],
        )
        self.assertEqual(response.json["price_u"], "0.21")
        self.assertEqual(response.json["price_wei"], str(PAID_PRICE_WEI))
        self.assertEqual(response.json["supportedAssets"], SUPPORTED_ASSETS)
        self.assertTrue(response.json["paymentRequired"])
        self.assertNotIn("min_price_u", response.json)
        self.assertNotIn("min_price_wei", response.json)

    async def test_paid_requirements_request_registry_tokens_from_b402(self) -> None:
        client = AsyncMock()
        client.payment_extras.return_value = SUPPORTED_EXTRAS
        handler = make_handler(AsyncMock(), b402_client=client)

        requirements = await handler._paid_requirements()

        self.assertEqual(
            [item["asset"] for item in requirements],
            [token.address for token in TOKENS],
        )
        client.payment_extras.assert_awaited_once_with(
            "eip155:56",
            TOKENS,
        )

    async def test_paid_requirements_need_b402_backend(self) -> None:
        handler = X402Handler(
            AsyncMock(),
            free_work=Mock(),
            job_service=AsyncMock(),
            b402_client=None,
        )

        with (
            self.assertRaisesRegex(
                handler_module.B402IndeterminateError,
                "payment backend unavailable",
            ),
            patch.object(handler_module, "FACILITATOR_URL", "https://example.test"),
            patch.object(handler_module, "X402_DEMO_MODE", False),
            patch.dict(handler_module.os.environ, {}, clear=True),
        ):
            await handler._paid_requirements()

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
            headers={"payment-signature": "proof"},
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

    async def test_promotional_create_requires_wallet_signature(
        self,
    ) -> None:
        service = AsyncMock()
        service.promo_free = True
        service.create_promotional_job.return_value = CreateJobResult(
            job_id=JOB_ID,
            job_token="token",
            status="queued",
            expires_at=EXPIRES_AT,
        )
        client = AsyncMock()
        client.payment_extras.side_effect = AssertionError(
            "promo must not call B402"
        )

        response = await call_handler(
            make_handler(service, b402_client=client),
            method="POST",
            path="/x402/analyze/async",
            json_body={"symbols": ["AAPL"]},
            scope_overrides={"x402_source_ip": "198.51.100.8"},
        )

        self.assertEqual(response.status, 401)
        self.assert_private_no_store(response, token_authenticated=False)
        self.assertEqual(response.json["errorCode"], "wallet_signature_required")
        service.create_promotional_job.assert_not_awaited()
        service.create_job.assert_not_awaited()
        client.payment_extras.assert_not_awaited()

    async def test_promotional_create_rejects_invalid_or_duplicate_wallet_signature(
        self,
    ) -> None:
        for label, headers, scope_overrides in (
            ("malformed", {"wallet-signature": "not-base64!"}, None),
            (
                "duplicate",
                None,
                {
                    "headers": [
                        (b"wallet-signature", b"first"),
                        (b"Wallet-Signature", b"second"),
                    ],
                },
            ),
        ):
            service = AsyncMock()
            service.promo_free = True
            with self.subTest(label=label):
                response = await call_handler(
                    make_handler(service),
                    method="POST",
                    path="/x402/analyze/async",
                    headers=headers,
                    json_body={"symbols": ["AAPL"]},
                    scope_overrides=scope_overrides,
                )

            self.assertEqual(response.status, 401)
            self.assertEqual(
                response.json["errorCode"],
                "wallet_signature_invalid",
            )
            service.create_promotional_job.assert_not_awaited()

    async def test_promotional_price_is_zero_without_b402_lookup(self) -> None:
        service = AsyncMock()
        service.promo_free = True
        client = AsyncMock()
        client.payment_extras.side_effect = AssertionError(
            "promo must not call B402"
        )

        response = await call_handler(
            make_handler(service, b402_client=client),
            method="GET",
            path="/x402/price",
        )

        self.assertEqual(response.status, 200)
        self.assertTrue(response.json["promoFree"])
        self.assertFalse(response.json["paymentRequired"])
        self.assertEqual(response.json["price_u"], "0.0")
        self.assertEqual(response.json["price_wei"], "0")
        self.assertEqual(response.json["accepts"], [])
        self.assertEqual(response.json["supportedAssets"], SUPPORTED_ASSETS)
        self.assertIsNone(response.json["asset"])
        self.assertIsNone(response.json["payTo"])
        self.assertIsNone(response.json["signingScheme"])
        self.assertEqual(response.json["signingSchemes"], [])
        self.assertIsNone(response.json["facilitator"])
        self.assertEqual(
            response.json["walletAuthorization"]["header"],
            "Wallet-Signature",
        )
        self.assertEqual(
            response.json["walletAuthorization"]["scheme"],
            "eip712-wallet",
        )
        client.payment_extras.assert_not_awaited()

    async def test_promotional_create_receives_trusted_source_ip(self) -> None:
        service = AsyncMock()
        service.promo_free = True
        service.create_promotional_job.return_value = CreateJobResult(
            job_id=JOB_ID,
            job_token="token",
            status="queued",
            expires_at=EXPIRES_AT,
        )
        request = {"symbols": ["AAPL"]}
        wallet_header, wallet_address = promo_wallet_header(
            request,
            now=SIGNED_NOW,
        )

        with patch.object(handler_module.time, "time", return_value=SIGNED_NOW):
            response = await call_handler(
                make_handler(service),
                method="POST",
                path="/x402/analyze/async",
                headers={"wallet-signature": wallet_header},
                json_body=request,
                scope_overrides={"x402_source_ip": "198.51.100.8"},
            )

        self.assertEqual(response.status, 202)
        call = service.create_promotional_job.await_args
        self.assertEqual(call.args, (request,))
        self.assertEqual(call.kwargs["source_ip"], "198.51.100.8")
        self.assertEqual(call.kwargs["authorization"].address, wallet_address)
        service.create_job.assert_not_awaited()

    async def test_promotional_create_allows_expired_signature_for_service_recovery(
        self,
    ) -> None:
        service = AsyncMock()
        service.promo_free = True
        service.create_promotional_job.return_value = CreateJobResult(
            job_id=JOB_ID,
            job_token="token",
            status="queued",
            expires_at=EXPIRES_AT,
        )
        request = {"symbols": ["AAPL"]}
        wallet_header, _address = promo_wallet_header(
            request,
            now=SIGNED_NOW,
            expires_at=SIGNED_NOW - 1,
        )

        with patch.object(handler_module.time, "time", return_value=SIGNED_NOW):
            response = await call_handler(
                make_handler(service),
                method="POST",
                path="/x402/analyze/async",
                headers={"wallet-signature": wallet_header},
                json_body=request,
                scope_overrides={"x402_source_ip": "198.51.100.8"},
            )

        self.assertEqual(response.status, 202)
        service.create_promotional_job.assert_awaited_once()

    async def test_promotional_create_ignores_payment_header_when_wallet_signed(self) -> None:
        service = AsyncMock()
        service.promo_free = True
        service.create_promotional_job.return_value = CreateJobResult(
            job_id=JOB_ID,
            job_token="token",
            status="queued",
            expires_at=EXPIRES_AT,
        )
        client = AsyncMock()
        client.payment_extras.side_effect = AssertionError(
            "promo must not call B402"
        )
        request = {"symbols": ["AAPL"]}
        wallet_header, _wallet_address = promo_wallet_header(
            request,
            now=SIGNED_NOW,
        )

        with patch.object(handler_module.time, "time", return_value=SIGNED_NOW):
            response = await call_handler(
                make_handler(service, b402_client=client),
                method="POST",
                path="/x402/analyze/async",
                headers={
                    "payment-signature": "must-not-be-decoded",
                    "wallet-signature": wallet_header,
                },
                json_body=request,
                scope_overrides={"x402_source_ip": "198.51.100.8"},
            )

        self.assertEqual(response.status, 202)
        self.assertEqual(service.create_promotional_job.await_count, 1)
        service.create_job.assert_not_awaited()
        client.payment_extras.assert_not_awaited()

    async def test_promotional_create_without_source_ip_fails_closed(
        self,
    ) -> None:
        service = AsyncMock()
        service.promo_free = True
        service.create_promotional_job.side_effect = X402JobError(
            "promo_source_ip_required"
        )
        request = {"symbols": ["AAPL"]}
        wallet_header, _wallet_address = promo_wallet_header(
            request,
            now=SIGNED_NOW,
        )

        with patch.object(handler_module.time, "time", return_value=SIGNED_NOW):
            response = await call_handler(
                make_handler(service),
                method="POST",
                path="/x402/analyze/async",
                headers={"wallet-signature": wallet_header},
                json_body=request,
            )

        self.assertEqual(response.status, 503)
        self.assertEqual(
            response.json["errorCode"],
            "job_service_unavailable",
        )
        self.assertEqual(service.create_promotional_job.await_count, 1)
        self.assertIsNone(
            service.create_promotional_job.await_args.kwargs["source_ip"]
        )
        service.create_job.assert_not_awaited()

    async def test_promotional_rate_limit_maps_to_429_with_retry_after(
        self,
    ) -> None:
        service = AsyncMock()
        service.promo_free = True
        service.create_promotional_job.side_effect = X402JobError(
            "promo_rate_limited",
            retry_after_seconds=123,
        )
        request = {"symbols": ["AAPL"]}
        wallet_header, _wallet_address = promo_wallet_header(
            request,
            now=SIGNED_NOW,
        )

        with patch.object(handler_module.time, "time", return_value=SIGNED_NOW):
            response = await call_handler(
                make_handler(service),
                method="POST",
                path="/x402/analyze/async",
                headers={"wallet-signature": wallet_header},
                json_body=request,
                scope_overrides={"x402_source_ip": "198.51.100.8"},
            )

        self.assertEqual(response.status, 429)
        self.assertEqual(response.headers["retry-after"], "123")
        self.assertEqual(response.json["errorCode"], "promo_rate_limited")

    async def test_async_challenge_uses_only_v2_payment_required_header(
        self,
    ) -> None:
        service = AsyncMock()

        response = await call_handler(
            make_handler(service),
            method="POST",
            path="/x402/analyze/async",
            json_body={"symbols": ["AAPL"]},
        )

        self.assertEqual(response.status, 402)
        self.assert_private_no_store(response, token_authenticated=False)
        self.assertEqual(
            decode_header(response, "payment-required"),
            response.json["paymentRequired"],
        )
        self.assertEqual(
            [
                item["asset"]
                for item in response.json["paymentRequired"]["accepts"]
            ],
            [token.address for token in TOKENS],
        )
        self.assertEqual(
            [
                item["amount"]
                for item in response.json["paymentRequired"]["accepts"]
            ],
            [str(PAID_PRICE_WEI)] * len(TOKENS),
        )
        service.create_job.assert_not_awaited()

    async def test_async_create_accepts_payment_signature_and_emits_payment_response(
        self,
    ) -> None:
        service = AsyncMock()
        service.promo_free = False
        service.create_job.return_value = CreateJobResult(
            job_id=JOB_ID,
            job_token="job-token",
            status="queued",
            expires_at=123456,
            payment_response={
                "success": True,
                "transaction": "0xtx",
                "network": "eip155:56",
                "payer": "0x1111111111111111111111111111111111111111",
            },
        )
        response = await call_handler(
            make_handler(service),
            method="POST",
            path="/x402/analyze/async",
            headers={"payment-signature": "proof"},
            json_body={"symbols": ["AAPL"]},
        )
        service.create_job.assert_awaited_once_with("proof", {"symbols": ["AAPL"]})
        self.assertEqual(
            decode_header(response, "payment-response"),
            service.create_job.return_value.payment_response,
        )

    async def test_async_create_rejects_legacy_x_payment_as_unpaid(self) -> None:
        service = AsyncMock()
        service.promo_free = False
        response = await call_handler(
            make_handler(service),
            method="POST",
            path="/x402/analyze/async",
            headers={"x-payment": "proof"},
            json_body={"symbols": ["AAPL"]},
        )
        self.assertEqual(response.status, 402)
        service.create_job.assert_not_awaited()

    async def test_usd1_only_backend_keeps_challenge_and_price_available(self) -> None:
        client = AsyncMock()
        client.payment_extras.return_value = {
            USD1_TOKEN.symbol: USD1_SUPPORTED_EXTRA,
        }
        handler = make_handler(AsyncMock(), b402_client=client)

        challenge = await call_handler(
            handler,
            method="POST",
            path="/x402/analyze/async",
            json_body={"symbols": ["AAPL"]},
        )
        price = await call_handler(handler, method="GET", path="/x402/price")

        self.assertEqual(challenge.status, 402)
        self.assertEqual(
            [item["asset"] for item in challenge.json["paymentRequired"]["accepts"]],
            [USD1_TOKEN.address],
        )
        self.assertEqual(price.status, 200)
        self.assertEqual(
            [item["asset"] for item in price.json["accepts"]],
            [USD1_TOKEN.address],
        )
        self.assertEqual(price.json["asset"], USD1_TOKEN.address)

    async def test_usdt_only_backend_drives_legacy_price_fields(self) -> None:
        client = AsyncMock()
        client.payment_extras.return_value = {
            USDT_TOKEN.symbol: USDT_SUPPORTED_EXTRA,
        }
        handler = make_handler(AsyncMock(), b402_client=client)

        price = await call_handler(handler, method="GET", path="/x402/price")

        self.assertEqual(price.status, 200)
        self.assertEqual(
            [item["asset"] for item in price.json["accepts"]],
            [USDT_TOKEN.address],
        )
        self.assertEqual(price.json["asset"], USDT_TOKEN.address)
        self.assertEqual(price.json["price_wei"], str(PAID_PRICE_WEI))
        self.assertEqual(price.json["network"], "eip155:56")
        self.assertEqual(price.json["signingScheme"], "permit2-exact")
        self.assertEqual(price.json["signingSchemes"], ["permit2-exact"])

    async def test_missing_usdc_keeps_other_paid_accepts_in_registry_order(
        self,
    ) -> None:
        client = AsyncMock()
        client.payment_extras.return_value = {
            symbol: extra
            for symbol, extra in SUPPORTED_EXTRAS.items()
            if symbol != USDC_TOKEN.symbol
        }
        handler = make_handler(AsyncMock(), b402_client=client)

        price = await call_handler(handler, method="GET", path="/x402/price")

        self.assertEqual(price.status, 200)
        self.assertEqual(
            [item["asset"] for item in price.json["accepts"]],
            [U_TOKEN.address, USD1_TOKEN.address, USDT_TOKEN.address],
        )
        self.assertEqual(
            price.json["signingSchemes"],
            ["eip3009", "permit2-exact"],
        )

    async def test_empty_capability_intersection_returns_503(self) -> None:
        client = AsyncMock()
        client.payment_extras.return_value = {}
        handler = make_handler(AsyncMock(), b402_client=client)

        challenge = await call_handler(
            handler,
            method="POST",
            path="/x402/analyze/async",
            json_body={"symbols": ["AAPL"]},
        )
        price = await call_handler(handler, method="GET", path="/x402/price")

        self.assertEqual(challenge.status, 503)
        self.assertEqual(price.status, 503)

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
        b402_client.payment_extras.side_effect = RuntimeError(
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
            headers={"payment-signature": "proof"},
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
            headers={"payment-signature": "proof"},
            body_chunks=[b"[]"],
        )

        self.assertEqual(response.status, 400)
        self.assertEqual(response.json["errorCode"], "invalid_request")
        service.create_job.assert_not_awaited()

    async def test_async_create_rejects_non_utf8_payment_signature(self) -> None:
        service = AsyncMock()

        response = await call_handler(
            make_handler(service),
            method="POST",
            path="/x402/analyze/async",
            json_body={"symbols": ["AAPL"]},
            scope_overrides={
                "headers": [(b"payment-signature", b"\xff")],
            },
        )

        self.assertEqual(response.status, 402)
        self.assertEqual(response.json["errorCode"], "payment_rejected")
        service.create_job.assert_not_awaited()

    async def test_paid_and_free_handlers_reject_malformed_nested_proofs(
        self,
    ) -> None:
        paid_base = json.loads(base64.b64decode(signed_proof()))
        free_base = json.loads(
            base64.b64decode(signed_free_proof(x402_verify.B402_PAY_TO_ADDRESS))
        )

        def encode(value: object) -> str:
            return base64.b64encode(
                json.dumps(value, separators=(",", ":")).encode()
            ).decode()

        malformed_paid: list[tuple[str, str]] = []
        malformed_free: list[tuple[str, str]] = []
        for name, value in (
            ("list", []),
            ("scalar", 1),
        ):
            paid_payload = {**paid_base, "payload": value}
            paid_authorization = {
                **paid_base,
                "payload": {
                    **paid_base["payload"],
                    "authorization": value,
                },
            }
            free_payload = {**free_base, "payload": value}
            free_authorization = {
                **free_base,
                "payload": {
                    **free_base["payload"],
                    "authorization": value,
                },
            }
            malformed_paid.extend((
                (f"payload {name}", encode(paid_payload)),
                (f"authorization {name}", encode(paid_authorization)),
            ))
            malformed_free.extend((
                (f"payload {name}", encode(free_payload)),
                (f"authorization {name}", encode(free_authorization)),
            ))

        depth = 2_000
        recursive = base64.b64encode(
            b'{"payload":'
            + b"[" * depth
            + b"0"
            + b"]" * depth
            + b"}"
        ).decode()
        malformed_paid.append(("recursive", recursive))
        malformed_free.append(("recursive", recursive))

        for name, proof in malformed_paid:
            service = AsyncMock()
            service.promo_free = False

            async def validate_create(
                payment_header: str,
                _request: dict,
            ) -> CreateJobResult:
                payment, _reason = x402_verify.validate_payment_proof(
                    payment_header,
                    now=SIGNED_NOW,
                )
                if payment is None:
                    raise X402JobError("payment_rejected")
                raise AssertionError("malformed proof was accepted")

            service.create_job.side_effect = validate_create
            try:
                response = await call_handler(
                    make_handler(service),
                    method="POST",
                    path="/x402/analyze/async",
                    headers={"payment-signature": proof},
                    json_body={"symbols": ["AAPL"]},
                )
            except Exception as exc:
                self.fail(f"paid {name} raised {type(exc).__name__}")
            self.assertEqual(response.status, 402, name)
            self.assertEqual(
                response.json,
                {"errorCode": "payment_rejected"},
                name,
            )

        for name, proof in malformed_free:
            try:
                response = await call_handler(
                    X402Handler(
                        AsyncMock(),
                        free_work=free_report_work,
                        b402_client=AsyncMock(),
                    ),
                    method="POST",
                    path="/x402/free",
                    headers={"payment-signature": proof},
                    json_body={"symbol": "AAPL"},
                )
            except Exception as exc:
                self.fail(f"free {name} raised {type(exc).__name__}")
            self.assertEqual(response.status, 402, name)
            self.assertEqual(
                response.json,
                {
                    "error": "Free tier access denied",
                    "detail": "Payment-Signature is not valid base64 JSON",
                },
                name,
            )

    async def test_free_handler_rejects_malformed_numeric_proof(self) -> None:
        proof = json.loads(
            base64.b64decode(
                signed_free_proof(x402_verify.B402_PAY_TO_ADDRESS)
            )
        )
        proof["payload"]["authorization"]["validAfter"] = []
        encoded = base64.b64encode(
            json.dumps(proof, separators=(",", ":")).encode()
        ).decode()

        try:
            response = await call_handler(
                X402Handler(
                    AsyncMock(),
                    free_work=free_report_work,
                    b402_client=AsyncMock(),
                ),
                method="POST",
                path="/x402/free",
                headers={"payment-signature": encoded},
                json_body={"symbol": "AAPL"},
            )
        except Exception as exc:
            self.fail(f"free numeric field raised {type(exc).__name__}")
        self.assertEqual(response.status, 402)
        self.assertEqual(
            response.json,
            {
                "error": "Free tier access denied",
                "detail": "Payment-Signature is not valid base64 JSON",
            },
        )

    async def test_free_handler_rejects_a_thousand_digit_value(self) -> None:
        proof = json.loads(
            base64.b64decode(
                signed_free_proof(x402_verify.B402_PAY_TO_ADDRESS)
            )
        )
        proof["payload"]["authorization"]["value"] = "9" * 1_000
        encoded = base64.b64encode(
            json.dumps(proof, separators=(",", ":")).encode()
        ).decode()

        try:
            response = await call_handler(
                X402Handler(
                    AsyncMock(),
                    free_work=free_report_work,
                    b402_client=AsyncMock(),
                ),
                method="POST",
                path="/x402/free",
                headers={"payment-signature": encoded},
                json_body={"symbol": "AAPL"},
            )
        except Exception as exc:
            self.fail(f"thousand-digit free value raised {type(exc).__name__}")
        self.assertEqual(response.status, 402)
        self.assertEqual(
            response.json,
            {
                "error": "Free tier access denied",
                "detail": (
                    "free tier requires value=0; "
                    "use /x402/analyze/async for paid analysis"
                ),
            },
        )

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
                "headers": [(b"payment-signature", b"proof")],
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
        service.create_job.side_effect = X402JobError(
            "payment_rejected",
            payment_response={
                "success": False,
                "transaction": "",
                "network": "eip155:56",
                "payer": "0x1111111111111111111111111111111111111111",
                "errorReason": "payment_rejected",
            },
        )

        response = await call_handler(
            make_handler(service),
            method="POST",
            path="/x402/analyze/async",
            headers={"payment-signature": "proof"},
            json_body={"symbols": ["AAPL"]},
        )

        self.assertEqual(response.status, 402)
        self.assertEqual(response.json["errorCode"], "payment_rejected")
        self.assertFalse(decode_header(response, "payment-response")["success"])

    async def test_async_create_maps_invalid_request_to_400(self) -> None:
        service = AsyncMock()
        service.create_job.side_effect = X402JobError("invalid_request")

        response = await call_handler(
            make_handler(service),
            method="POST",
            path="/x402/analyze/async",
            headers={"payment-signature": "proof"},
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
            headers={"payment-signature": "proof"},
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

    async def test_rate_limited_view_includes_public_message(self) -> None:
        service = AsyncMock()
        service.get_job.return_value = JobView(
            job_id=JOB_ID,
            status="failed",
            expires_at=EXPIRES_AT,
            error_code="too_many_users",
            retryable=True,
        )

        response = await call_handler(
            make_handler(service),
            method="GET",
            path=f"/x402/jobs/{JOB_ID}",
            headers={"x-job-token": "token"},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.json["errorCode"], "too_many_users")
        self.assertEqual(
            response.json["error"],
            "Too many users now. Please try again later",
        )
        self.assertTrue(response.json["retryable"])

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
            headers={"payment-signature": "proof"},
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

    async def test_free_challenge_uses_trusted_public_resource_url(self) -> None:
        response = await call_handler(
            make_handler(AsyncMock()),
            method="GET",
            path="/x402/free",
            scope_overrides={
                "query_string": b"symbol=AAPL",
                "x402_public_base_url": "https://gateway.example/testnet",
            },
        )

        self.assertEqual(response.status, 402)
        self.assertEqual(
            response.json["paymentRequired"]["resource"],
            "https://gateway.example/testnet/x402/free",
        )

    async def test_free_post_returns_buffered_json_report(self) -> None:
        handler = X402Handler(
            AsyncMock(),
            free_work=free_report_work,
            b402_client=AsyncMock(),
        )
        with (
            patch(
                "stockanalyst.app.agent.x402_handler.verify_free_payment_proof",
                return_value=(True, "9 uses remaining today", "0x" + "1" * 40),
            ),
            patch(
                "stockanalyst.app.agent.x402_handler._payment_identity",
                return_value=("0x" + "1" * 40, "0x" + "2" * 64),
            ),
            patch(
                "stockanalyst.app.agent.x402_handler.report_competition_call",
                new=AsyncMock(),
            ),
        ):
            response = await call_handler(
                handler,
                method="POST",
                path="/x402/free",
                headers={"payment-signature": "proof"},
                json_body={"symbol": "AAPL"},
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.json, {
            "content": "# AAPL report",
            "format": "markdown",
        })

    async def test_free_post_returns_safe_error_when_quote_generation_fails(self) -> None:
        async def failed_work(symbol: str):
            yield "error", {"message": "private upstream failure"}

        handler = X402Handler(
            AsyncMock(),
            free_work=failed_work,
            b402_client=AsyncMock(),
        )
        with (
            patch(
                "stockanalyst.app.agent.x402_handler.verify_free_payment_proof",
                return_value=(True, "9 uses remaining today", "0x" + "1" * 40),
            ),
            patch(
                "stockanalyst.app.agent.x402_handler._payment_identity",
                return_value=("0x" + "1" * 40, "0x" + "2" * 64),
            ),
            patch(
                "stockanalyst.app.agent.x402_handler.report_competition_call",
                new=AsyncMock(),
            ),
        ):
            response = await call_handler(
                handler,
                method="POST",
                path="/x402/free",
                headers={"payment-signature": "proof"},
                json_body={"symbol": "AAPL"},
            )

        self.assertEqual(response.status, 503)
        self.assertEqual(response.json, {
            "errorCode": "free_quote_unavailable",
            "retryable": True,
        })
        self.assertNotIn(b"private upstream failure", response.body)


if __name__ == "__main__":
    unittest.main()
