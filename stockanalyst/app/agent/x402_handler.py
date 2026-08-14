"""x402 HTTP payment channel with durable asynchronous delivery.

Adds paid asynchronous job routes alongside the existing A2A server (pure ASGI
middleware — no extra framework or heavy deps):

  GET  /x402/price                → current price and asset information
  POST /x402/analyze/async        → settle payment and return a durable job
  GET  /x402/jobs/{jobId}         → authenticated job status and download URL
  POST /x402/jobs/{jobId}/resume  → authenticated recovery

Payment verification is FIXED CODE in x402_verify.py — never LLM-callable.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from typing import Any

import httpx

if __package__:
    from .b402_client import (
        B402Client,
        B402Config,
        B402ConfigurationError,
        B402IndeterminateError,
        B402RejectedError,
    )
    from .x402_job_service import (
        JobView,
        SettlementIndeterminate,
        X402JobError,
        X402JobService,
        is_valid_settlement_reference,
    )
    from .x402_settlement import SettlementOutcome
    from .x402_tokens import TOKENS, supported_assets
    from .x402_verify import (
        CHAIN_ID,
        PRICE_WEI,
        build_payment_challenge,
        build_payment_requirement,
        decode_payment_signature,
    )
else:
    from b402_client import (
        B402Client,
        B402Config,
        B402ConfigurationError,
        B402IndeterminateError,
        B402RejectedError,
    )
    from x402_job_service import (
        JobView,
        SettlementIndeterminate,
        X402JobError,
        X402JobService,
        is_valid_settlement_reference,
    )
    from x402_settlement import SettlementOutcome
    from x402_tokens import TOKENS, supported_assets
    from x402_verify import (
        CHAIN_ID,
        PRICE_WEI,
        build_payment_challenge,
        build_payment_requirement,
        decode_payment_signature,
    )

logger = logging.getLogger("seller-agent.x402")

_ASYNC_BODY_MAX_BYTES = 256 * 1024
_JOB_PATH_RE = re.compile(r"/x402/jobs/(x402_[0-9a-f]{32})(/resume)?\Z")


class BodyTooLarge(ValueError):
    pass


class RequestDisconnected(ValueError):
    pass


class InvalidHeaderValue(ValueError):
    pass


def _encode_payment_header(value: Mapping[str, Any]) -> bytes:
    body = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    return base64.b64encode(body)


# ── Settlement configuration (priority: B402 > generic facilitator > demo) ─────

# Binance B402 V2 authenticated facilitator (preferred).
try:
    _B402_CONFIG = B402Config.from_env()
except B402ConfigurationError:
    _B402_CONFIG = None
    logger.error(
        "x402: incomplete or invalid B402 V2 configuration; paid requests "
        "will fail closed"
    )
_B402_CLIENT = B402Client(_B402_CONFIG) if _B402_CONFIG is not None else None

# Generic x402 facilitator (fallback — no HMAC auth).
FACILITATOR_URL = os.environ.get("X402_FACILITATOR_URL", "").rstrip("/")

# Demo / local-dev mode — explicit opt-in required.
# NEVER set this in production — signatures verified but NO token transferred.
X402_DEMO_MODE = os.environ.get("X402_DEMO_MODE", "").strip().lower() in ("1", "true", "yes")

if _B402_CLIENT is not None:
    logger.info("x402: Binance B402 V2 RSA facilitator active")
elif FACILITATOR_URL:
    logger.info("x402: generic facilitator active")
elif X402_DEMO_MODE:
    logger.warning(
        "x402: DEMO MODE active (X402_DEMO_MODE=1) — EIP-712 signatures are verified "
        "but NO on-chain token transfer is executed. Never use this in production."
    )
else:
    logger.warning(
        "x402: SECURITY — no settlement backend configured. "
        "The paid /x402/analyze/async endpoint will REJECT all requests until one is set. "
        "For production: configure B402_CLIENT_ID, B402_ACCESS_TOKEN, "
        "B402_BASE_URL, and B402_PRIVATE_KEY. "
        "For local testing: export X402_DEMO_MODE=1."
    )


async def _settle_via_facilitator(
    proof_b64: str,
    mode: str,
) -> SettlementOutcome:
    """Execute on-chain settlement via configured backend.

    Priority: B402 V2 (RSA-SHA256) → generic facilitator → demo mode → fail closed.

    Returns a typed settlement outcome; unknown remote outcomes stay retryable.
    """
    # Decode proof (shared by all settlement paths)
    proof = decode_payment_signature(proof_b64)
    if proof is None:
        return SettlementOutcome(
            "rejected",
            reason="Payment-Signature is not valid base64 JSON",
        )

    accepted = proof.get("accepted")
    if not isinstance(accepted, dict):
        return SettlementOutcome(
            "rejected",
            reason="payment not settled: missing V2 accepted requirement",
        )
    extra = accepted.get("extra")
    if not isinstance(extra, dict):
        return SettlementOutcome(
            "rejected",
            reason="payment not settled: missing transfer method",
        )
    transfer_method = extra.get("assetTransferMethod")
    payload = {
        "x402Version": 2,
        "paymentPayload": proof,
        "paymentRequirements": accepted,
    }

    # ── 1. Binance B402 V2 (RSA-SHA256) ────────────────────────────────────────
    if _B402_CLIENT is not None:
        try:
            if mode == "verify-and-settle":
                outcome = await _B402_CLIENT.verify_and_settle(proof)
            elif mode == "settle-only":
                outcome = await _B402_CLIENT.settle_only(proof)
            else:
                return SettlementOutcome(
                    "rejected",
                    reason="payment not settled: invalid settlement mode",
                )
            if not isinstance(outcome, SettlementOutcome):
                raise SettlementIndeterminate()
            logger.info(
                "x402 facilitator outcome=%s backend=b402",
                outcome.status,
            )
            return outcome
        except B402RejectedError as exc:
            logger.warning("x402 facilitator outcome=rejected backend=b402")
            return SettlementOutcome("rejected", reason=str(exc))
        except B402IndeterminateError as exc:
            raise SettlementIndeterminate() from exc

    if transfer_method != "eip3009":
        return SettlementOutcome(
            "rejected",
            reason="payment not settled: transfer method requires B402",
        )

    # ── 2. Generic x402 facilitator (unauthenticated POST) ─────────────────────
    if FACILITATOR_URL:
        settled, detail = await _settle_generic(payload)
        if settled:
            return SettlementOutcome("settled", transaction=detail)
        return SettlementOutcome("rejected", reason=detail)

    # ── 3. Demo mode (local testing only) ──────────────────────────────────────
    if X402_DEMO_MODE:
        logger.warning(
            "x402: demo mode — EIP-712 sig OK but no on-chain transfer (X402_DEMO_MODE=1)"
        )
        return SettlementOutcome("settled", transaction="demo")

    # ── 4. Fail closed ─────────────────────────────────────────────────────────
    return SettlementOutcome(
        "rejected",
        reason=(
            "payment not settled: no settlement backend configured. "
            "Configure all four B402 V2 settings for production, or "
            "X402_DEMO_MODE=1 for local testing."
        ),
    )


async def _settle_generic(payload: dict) -> tuple[bool, str]:
    """POST to a generic x402 facilitator (no HMAC auth)."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{FACILITATOR_URL}/settle",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code >= 500:
                raise SettlementIndeterminate()
            data = resp.json()
        if not isinstance(data, dict):
            raise SettlementIndeterminate()
        success = data.get("success")
        if type(success) is not bool:
            raise SettlementIndeterminate()
        if success is True:
            txhash = data.get("transaction")
            if not is_valid_settlement_reference(txhash):
                raise SettlementIndeterminate()
            logger.info("x402 facilitator outcome=settled backend=generic")
            return True, txhash
        if "transaction" in data and data["transaction"] != "":
            raise SettlementIndeterminate()
        reason = str(data.get("errorReason") or data.get("error") or "facilitator rejected")
        logger.warning("x402 facilitator outcome=rejected backend=generic")
        return False, reason
    except SettlementIndeterminate:
        raise
    except Exception as exc:
        logger.warning("x402 facilitator outcome=indeterminate backend=generic")
        raise SettlementIndeterminate() from exc


class X402Handler:
    """ASGI middleware: intercepts /x402/* routes, forwards everything else.

    Mount as the outermost ASGI layer in main.py so it sits in front of the
    A2A server, the ERC-8183 local-storage route, and the JSON-RPC error-
    hardening middleware.

    Args:
        app: Inner ASGI application (A2A + existing routes).
        job_service: Durable paid-analysis service.
    """

    def __init__(
        self,
        app,
        *,
        job_service: X402JobService | None = None,
        b402_client: B402Client | None = _B402_CLIENT,
    ) -> None:
        self._inner = app
        self._job_service = job_service
        self._b402_client = b402_client

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/x402"):
            await self._inner(scope, receive, send)
            return

        raw_path = scope["path"]
        path = raw_path.rstrip("/")
        method = (scope.get("method") or "GET").upper()
        job_match = _JOB_PATH_RE.fullmatch(raw_path)

        if path == "/x402/price":
            await self._handle_price(scope, send)
        elif raw_path == "/x402/analyze/async" and method == "POST":
            if self._job_service is None:
                await _send_json(
                    send,
                    404,
                    {"error": "not found"},
                    extra_headers=_async_response_headers(),
                )
            else:
                await self._handle_async_analyze(scope, receive, send)
        elif (
            job_match
            and method == "GET"
            and job_match.group(2) is None
        ):
            if self._job_service is None:
                await _send_json(
                    send,
                    404,
                    {"error": "not found"},
                    extra_headers=_async_response_headers(
                        token_authenticated=True
                    ),
                )
            else:
                await self._handle_job_get(
                    scope,
                    send,
                    job_match.group(1),
                )
        elif (
            job_match
            and method == "POST"
            and job_match.group(2) == "/resume"
        ):
            if self._job_service is None:
                await _send_json(
                    send,
                    404,
                    {"error": "not found"},
                    extra_headers=_async_response_headers(
                        token_authenticated=True
                    ),
                )
            else:
                await self._handle_job_resume(
                    scope,
                    send,
                    job_match.group(1),
                )
        else:
            async_headers = None
            if raw_path == "/x402/analyze/async":
                async_headers = _async_response_headers()
            elif raw_path.startswith("/x402/jobs/"):
                async_headers = _async_response_headers(
                    token_authenticated=True
                )
            await _send_json(
                send,
                404,
                {"error": "not found", "x402_routes": [
                    "GET  /x402/price",
                    "POST /x402/analyze/async  (+ Payment-Signature header)  → paid, asynchronous report",
                    "GET  /x402/jobs/{jobId}  (+ X-Job-Token header)",
                    "POST /x402/jobs/{jobId}/resume  (+ X-Job-Token header)",
                ]},
                extra_headers=async_headers,
            )

    # ── Route handlers ─────────────────────────────────────────────────────────

    async def _handle_price(self, scope, send) -> None:
        """GET /x402/price — price info without payment."""
        try:
            requirements = await self._paid_requirements()
        except Exception:
            logger.warning("x402 payment configuration unavailable")
            await _send_payment_backend_unavailable(send)
            return
        challenge = build_payment_challenge(
            [],
            _public_resource(scope, "/x402/analyze/async"),
            requirements,
        )
        accepts = challenge.get("accepts") or []
        accept = accepts[0]
        schemes = list(dict.fromkeys(
            item["extra"]["assetTransferMethod"] for item in accepts
        ))
        await _send_json(send, 200, {
            "x402Version":  2,
            "paymentRequired": True,
            "price_u":      "0.1",
            "price_wei":    accept.get("amount", str(PRICE_WEI)),
            "asset":        accept.get("asset"),
            "network":      accept.get("network"),
            "payTo":        accept.get("payTo"),
            "accepts":      accepts,
            "supportedAssets": supported_assets(),
            "signingScheme": schemes[0],
            "signingSchemes": schemes,
            "facilitator": (
                "binance-b402-v2"
                if self._b402_client is not None
                else FACILITATOR_URL or "(demo mode — no on-chain settlement)"
            ),
        })

    async def _paid_requirements(self) -> list[dict[str, Any]]:
        if self._b402_client is None:
            raise B402IndeterminateError("payment backend unavailable")
        extras = await self._b402_client.payment_extras(
            f"eip155:{CHAIN_ID}",
            TOKENS,
        )
        requirements = [
            build_payment_requirement(token, extras[token.symbol])
            for token in TOKENS
            if token.symbol in extras
        ]
        if not requirements:
            raise B402RejectedError(
                "no configured payment token is supported"
            )
        return requirements

    async def _handle_async_analyze(self, scope, receive, send) -> None:
        """POST /x402/analyze/async — settle and return a durable job handle."""
        try:
            req = await _read_json_body(receive)
        except BodyTooLarge:
            await _send_json(
                send,
                413,
                {"errorCode": "request_too_large"},
                extra_headers=_async_response_headers(),
            )
            return
        except RequestDisconnected:
            return
        except (json.JSONDecodeError, UnicodeDecodeError):
            await _send_json(
                send,
                400,
                {"errorCode": "invalid_request"},
                extra_headers=_async_response_headers(),
            )
            return
        if not isinstance(req, dict):
            await _send_json(
                send,
                400,
                {"errorCode": "invalid_request"},
                extra_headers=_async_response_headers(),
            )
            return

        try:
            payment_header = _header(scope, b"payment-signature")
        except InvalidHeaderValue:
            await _send_job_error(
                send,
                X402JobError("payment_rejected"),
                token_authenticated=False,
            )
            return
        if not payment_header:
            symbols = _parse_symbols(req.get("symbols") or "")
            try:
                requirements = await self._paid_requirements()
                challenge = build_payment_challenge(
                    symbols,
                    _public_resource(scope, "/x402/analyze/async"),
                    requirements,
                )
            except Exception:
                logger.warning("x402 payment configuration unavailable")
                await _send_payment_backend_unavailable(send)
                return
            await _send_json(
                send,
                402,
                {
                    "error": "Payment Required",
                    "description": (
                        "Retry this request with a valid Payment-Signature header."
                    ),
                    "paymentRequired": challenge,
                },
                extra_headers=[
                    (b"payment-required", _encode_payment_header(challenge)),
                    *_async_response_headers(),
                ],
            )
            return

        try:
            result = await self._job_service.create_job(
                payment_header,
                req,
            )
        except X402JobError as exc:
            await _send_job_error(send, exc, token_authenticated=False)
            return
        except Exception as exc:
            logger.warning(
                "x402 asynchronous job service unavailable dependency=%s",
                type(exc).__name__,
            )
            await _send_service_unavailable(
                send,
                token_authenticated=False,
            )
            return

        status_url = f"/x402/jobs/{result.job_id}"
        response_headers = [
            (b"location", status_url.encode()),
            (b"retry-after", b"10"),
        ]
        if result.payment_response is not None:
            response_headers.append(
                (b"payment-response", _encode_payment_header(result.payment_response))
            )
        await _send_json(
            send,
            202,
            {
                "jobId": result.job_id,
                "jobToken": result.job_token,
                "status": result.status,
                "statusUrl": status_url,
                "expiresAt": result.expires_at,
            },
            extra_headers=[*response_headers, *_async_response_headers()],
        )

    async def _handle_job_get(self, scope, send, job_id: str) -> None:
        """GET /x402/jobs/{jobId} — return an authenticated job view."""
        try:
            token = _header(scope, b"x-job-token")
        except InvalidHeaderValue:
            await _send_job_error(
                send,
                X402JobError("job_not_found"),
                token_authenticated=True,
            )
            return
        try:
            view = await self._job_service.get_job(job_id, token)
        except X402JobError as exc:
            await _send_job_error(send, exc, token_authenticated=True)
            return
        except Exception as exc:
            logger.warning(
                "x402 asynchronous job service unavailable dependency=%s",
                type(exc).__name__,
            )
            await _send_service_unavailable(
                send,
                token_authenticated=True,
            )
            return

        headers = _async_response_headers(
            token_authenticated=True,
            extra_headers=(
                [(b"retry-after", b"10")]
                if view.status in {"queued", "running"}
                else None
            ),
        )
        await _send_json(
            send,
            200,
            _job_view_body(view),
            extra_headers=headers,
        )

    async def _handle_job_resume(self, scope, send, job_id: str) -> None:
        """POST /x402/jobs/{jobId}/resume — resume eligible failed work."""
        try:
            token = _header(scope, b"x-job-token")
        except InvalidHeaderValue:
            await _send_job_error(
                send,
                X402JobError("job_not_found"),
                token_authenticated=True,
            )
            return
        try:
            view = await self._job_service.resume_job(job_id, token)
        except X402JobError as exc:
            await _send_job_error(send, exc, token_authenticated=True)
            return
        except Exception as exc:
            logger.warning(
                "x402 asynchronous job service unavailable dependency=%s",
                type(exc).__name__,
            )
            await _send_service_unavailable(
                send,
                token_authenticated=True,
            )
            return

        await _send_json(
            send,
            202,
            _job_view_body(view),
            extra_headers=_async_response_headers(
                token_authenticated=True,
                extra_headers=[(b"retry-after", b"10")],
            ),
        )

# ── Helpers ────────────────────────────────────────────────────────────────────

async def _read_json_body(
    receive,
    *,
    max_bytes: int = _ASYNC_BODY_MAX_BYTES,
) -> Any:
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise RequestDisconnected
        if message["type"] != "http.request":
            continue
        chunk = message.get("body") or b""
        total += len(chunk)
        if total > max_bytes:
            raise BodyTooLarge
        chunks.append(chunk)
        if not message.get("more_body"):
            break
    return json.loads(b"".join(chunks)) if chunks else {}


def _header(scope, name: bytes) -> str:
    expected = name.lower()
    for header_name, value in scope.get("headers") or []:
        if header_name.lower() == expected:
            try:
                return value.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise InvalidHeaderValue("invalid request header") from exc
    return ""


def _job_view_body(view: JobView) -> dict[str, Any]:
    body: dict[str, Any] = {
        "jobId": view.job_id,
        "status": view.status,
        "expiresAt": view.expires_at,
    }
    if view.error_code is not None:
        body["errorCode"] = view.error_code
        body["retryable"] = bool(view.retryable)
        if view.error_code == "too_many_users":
            body["error"] = "Too many users now. Please try again later"
    if view.download_url is not None:
        body["downloadUrl"] = view.download_url
        body["downloadUrlExpiresAt"] = view.download_url_expires_at
    return body


def _async_response_headers(
    *,
    token_authenticated: bool = False,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> list[tuple[bytes, bytes]]:
    headers = [(b"cache-control", b"private, no-store")]
    if token_authenticated:
        headers.append((b"vary", b"X-Job-Token"))
    if extra_headers:
        headers.extend(extra_headers)
    return headers


async def _send_job_error(
    send,
    error: X402JobError,
    *,
    token_authenticated: bool,
) -> None:
    code = error.code
    headers = _async_response_headers(
        token_authenticated=token_authenticated
    )
    if error.payment_response is not None:
        headers.append(
            (b"payment-response", _encode_payment_header(error.payment_response))
        )
    if code == "job_not_found":
        await _send_json(
            send,
            404,
            {"errorCode": "job_not_found"},
            extra_headers=headers,
        )
    elif code == "job_expired":
        await _send_json(
            send,
            410,
            {"errorCode": "job_expired"},
            extra_headers=headers,
        )
    elif code in {"job_conflict", "attempts_exhausted"}:
        await _send_json(
            send,
            409,
            {"errorCode": code},
            extra_headers=headers,
        )
    elif code == "invalid_request":
        await _send_json(
            send,
            400,
            {"errorCode": "invalid_request"},
            extra_headers=headers,
        )
    elif code == "payment_rejected":
        await _send_json(
            send,
            402,
            {"errorCode": "payment_rejected"},
            extra_headers=headers,
        )
    elif code in {
        "async_jobs_paused",
        "job_state_unavailable",
        "payment_unavailable",
        "settlement_pending",
    }:
        await _send_json(
            send,
            503,
            {"errorCode": code, "retryable": True},
            extra_headers=headers,
        )
    else:
        await _send_service_unavailable(
            send,
            token_authenticated=token_authenticated,
        )


async def _send_service_unavailable(
    send,
    *,
    token_authenticated: bool,
) -> None:
    await _send_json(
        send,
        503,
        {
            "errorCode": "job_service_unavailable",
            "retryable": True,
        },
        extra_headers=_async_response_headers(
            token_authenticated=token_authenticated
        ),
    )


async def _send_payment_backend_unavailable(send) -> None:
    await _send_json(
        send,
        503,
        {
            "errorCode": "payment_backend_unavailable",
            "retryable": True,
        },
        extra_headers=_async_response_headers(),
    )


def _parse_symbols(raw) -> list[str]:
    """Accept a list, a comma-string, or a single string → upper-cased list."""
    if isinstance(raw, list):
        return [str(s).strip().upper() for s in raw if str(s).strip()]
    if isinstance(raw, str):
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    return []


def _host(scope) -> str:
    headers: dict[bytes, bytes] = dict(scope.get("headers") or [])
    return (headers.get(b"host") or b"localhost:9000").decode()


def _public_resource(scope: dict, path: str) -> str:
    trusted = str(scope.get("x402_public_base_url") or "").rstrip("/")
    if trusted:
        return f"{trusted}{path}"
    scheme = str(scope.get("scheme") or "http")
    return f"{scheme}://{_host(scope)}{path}"


async def _send_json(
    send,
    status: int,
    data: dict,
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(data, ensure_ascii=False).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": headers,
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})
