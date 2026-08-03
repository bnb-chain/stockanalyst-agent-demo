"""x402 HTTP payment channel with durable asynchronous delivery.

Adds paid asynchronous job routes and the free quick-quote route alongside the
existing A2A server (pure ASGI middleware — no extra framework or heavy deps):

  GET  /x402/price                → current price and asset information
  POST /x402/analyze/async        → settle payment and return a durable job
  GET  /x402/jobs/{jobId}         → authenticated job status and download URL
  POST /x402/jobs/{jobId}/resume  → authenticated recovery
  GET|POST /x402/free             → zero-price quick quote over SSE

Payment verification is FIXED CODE in x402_verify.py — never LLM-callable.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from collections.abc import AsyncGenerator, Callable
from urllib.parse import parse_qs

import httpx

from competition_reporting import report_competition_call
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
    )
from x402_verify import (
    CHAIN_ID,
    FREE_TIER_LIMIT,
    MIN_PRICE_WEI,
    PRICE_WEI,
    SELLER_WALLET,
    U_TOKEN_BSC_TESTNET,
    build_free_payment_challenge,
    build_payment_challenge,
    verify_free_payment_proof,
)

logger = logging.getLogger("seller-agent.x402")

_ASYNC_BODY_MAX_BYTES = 256 * 1024
_JOB_PATH_RE = re.compile(r"/x402/jobs/(x402_[0-9a-f]{32})(/resume)?\Z")


class BodyTooLarge(ValueError):
    pass


class RequestDisconnected(ValueError):
    pass


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
    logger.info("x402: generic facilitator active — %s", FACILITATOR_URL)
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


async def _settle_via_facilitator(proof_b64: str) -> tuple[bool, str]:
    """Execute on-chain settlement via configured backend.

    Priority: B402 V2 (RSA-SHA256) → generic facilitator → demo mode → fail closed.

    Returns (True, txHash) on success, (False, error_reason) on failure.
    """
    # Decode proof (shared by all settlement paths)
    try:
        raw   = base64.b64decode(proof_b64.strip())
        proof = json.loads(raw)
    except Exception as exc:
        return False, f"could not decode proof: {exc}"

    accepted = proof.get("accepted")
    if not isinstance(accepted, dict):
        return False, "payment not settled: missing V2 accepted requirement"
    payload = {
        "x402Version": 2,
        "paymentPayload": proof,
        "paymentRequirements": accepted,
    }

    # ── 1. Binance B402 V2 (RSA-SHA256) ────────────────────────────────────────
    if _B402_CLIENT is not None:
        try:
            transaction = await _B402_CLIENT.verify_and_settle(proof)
            logger.info("x402 B402 settled: txHash=%s", transaction)
            return True, transaction
        except B402RejectedError as exc:
            logger.warning("x402 B402 rejected: %s", exc)
            return False, str(exc)
        except B402IndeterminateError as exc:
            raise SettlementIndeterminate() from exc

    # ── 2. Generic x402 facilitator (unauthenticated POST) ─────────────────────
    if FACILITATOR_URL:
        return await _settle_generic(payload)

    # ── 3. Demo mode (local testing only) ──────────────────────────────────────
    if X402_DEMO_MODE:
        logger.warning(
            "x402: demo mode — EIP-712 sig OK but no on-chain transfer (X402_DEMO_MODE=1)"
        )
        return True, "demo"

    # ── 4. Fail closed ─────────────────────────────────────────────────────────
    return False, (
        "payment not settled: no settlement backend configured. "
        "Configure all four B402 V2 settings for production, or "
        "X402_DEMO_MODE=1 for local testing."
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
        if data.get("success"):
            txhash = str(data.get("transaction") or "")
            logger.info("x402 facilitator settled: txHash=%s", txhash)
            return True, txhash
        reason = str(data.get("errorReason") or data.get("error") or "facilitator rejected")
        logger.warning("x402 facilitator rejected: %s", reason)
        return False, reason
    except SettlementIndeterminate:
        raise
    except Exception as exc:
        logger.exception("x402 facilitator call failed")
        raise SettlementIndeterminate() from exc


class X402Handler:
    """ASGI middleware: intercepts /x402/* routes, forwards everything else.

    Mount as the outermost ASGI layer in main.py so it sits in front of the
    A2A server, the ERC-8183 local-storage route, and the JSON-RPC error-
    hardening middleware.

    Args:
        app: Inner ASGI application (A2A + existing routes).
        free_stream_work: Async generator used only by the free quick-quote
            endpoint.
        job_service: Durable paid-analysis service.
    """

    def __init__(
        self,
        app,
        *,
        free_stream_work: Callable[..., AsyncGenerator[tuple[str, dict], None]] | None = None,
        job_service: X402JobService | None = None,
        b402_client: B402Client | None = _B402_CLIENT,
    ) -> None:
        self._inner = app
        self._free_stream_work = free_stream_work
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
        elif path == "/x402/free" and method == "GET":
            await self._handle_free_challenge(scope, send)
        elif path == "/x402/free" and method == "POST":
            await self._handle_free(scope, receive, send)
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
                    "POST /x402/analyze/async  (+ X-Payment header)  → paid, asynchronous report",
                    "GET  /x402/jobs/{jobId}  (+ X-Job-Token header)",
                    "POST /x402/jobs/{jobId}/resume  (+ X-Job-Token header)",
                    "GET  /x402/free?symbol=AAPL",
                    "POST /x402/free     (+ X-Payment header)  → free, 0 U, quick quote",
                ]},
                extra_headers=async_headers,
            )

    # ── Route handlers ─────────────────────────────────────────────────────────

    async def _handle_price(self, scope, send) -> None:
        """GET /x402/price — price info without payment."""
        try:
            extra = await self._paid_payment_extra()
        except Exception:
            logger.warning("x402 payment configuration unavailable")
            await _send_payment_backend_unavailable(send)
            return
        challenge = build_payment_challenge(
            [],
            _public_resource(scope, "/x402/analyze/async"),
            extra,
        )
        accept    = (challenge.get("accepts") or [{}])[0]
        await _send_json(send, 200, {
            "x402Version":  2,
            "price_u":      "1.0",
            "price_wei":    accept.get("amount", str(PRICE_WEI)),
            "min_price_u":  "0.5",
            "min_price_wei": str(MIN_PRICE_WEI),
            "asset":        accept.get("asset"),
            "network":      accept.get("network"),
            "payTo":        accept.get("payTo"),
            "signingScheme": "eip3009",
            "facilitator": (
                "binance-b402-v2"
                if self._b402_client is not None
                else FACILITATOR_URL or "(demo mode — no on-chain settlement)"
            ),
        })

    async def _paid_payment_extra(self) -> dict:
        if self._b402_client is not None:
            return await self._b402_client.payment_extra(
                f"eip155:{CHAIN_ID}",
                os.environ.get("U_TOKEN_DOMAIN_NAME", "U"),
                os.environ.get("U_TOKEN_DOMAIN_VERSION", "1"),
            )
        if FACILITATOR_URL or X402_DEMO_MODE:
            return {
                "name": os.environ.get("U_TOKEN_DOMAIN_NAME", "U"),
                "version": os.environ.get("U_TOKEN_DOMAIN_VERSION", "1"),
                "assetTransferMethod": "eip3009",
                "signerAddress": SELLER_WALLET.lower(),
            }
        raise B402IndeterminateError("payment backend unavailable")

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

        payment_header = _header(scope, b"x-payment")
        if not payment_header:
            symbols = _parse_symbols(req.get("symbols") or "")
            try:
                extra = await self._paid_payment_extra()
                challenge = build_payment_challenge(
                    symbols,
                    _public_resource(scope, "/x402/analyze/async"),
                    extra,
                )
            except Exception:
                logger.warning("x402 payment configuration unavailable")
                await _send_payment_backend_unavailable(send)
                return
            challenge_json = json.dumps(challenge).encode()
            await _send_json(
                send,
                402,
                {
                    "error": "Payment Required",
                    "description": (
                        "Retry this request with a valid X-Payment header."
                    ),
                    "paymentRequired": challenge,
                },
                extra_headers=[
                    (b"x-payment-required", challenge_json),
                    *_async_response_headers(),
                ],
            )
            return

        try:
            result = await self._job_service.create_job(payment_header, req)
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
            extra_headers=[
                (b"location", status_url.encode()),
                (b"retry-after", b"10"),
                *_async_response_headers(),
            ],
        )

    async def _handle_job_get(self, scope, send, job_id: str) -> None:
        """GET /x402/jobs/{jobId} — return an authenticated job view."""
        token = _header(scope, b"x-job-token")
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
        token = _header(scope, b"x-job-token")
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

    async def _handle_free_challenge(self, scope, send) -> None:
        """GET /x402/free?symbol=AAPL — return 402 free tier challenge (0 U)."""
        qs = parse_qs((scope.get("query_string") or b"").decode())
        symbol = ((qs.get("symbol") or qs.get("symbols") or [""])[0]).strip().upper()
        host = _host(scope)
        challenge = build_free_payment_challenge(symbol, host)
        challenge_json = json.dumps(challenge).encode()
        await send({
            "type": "http.response.start",
            "status": 402,
            "headers": [
                (b"x-payment-required", challenge_json),
                (b"content-type", b"application/json"),
            ],
        })
        body = json.dumps({
            "error": "Payment Required",
            "description": (
                "Sign a 0-U EIP-712 authorization to prove your wallet identity, "
                f"then POST to /x402/free. Rate limit: {FREE_TIER_LIMIT}/24h per wallet."
            ),
            "paymentRequired": challenge,
        }).encode()
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def _handle_free(self, scope, receive, send) -> None:
        """POST /x402/free — verify 0-U EIP-712 proof + rate limit + stream quick quote."""
        if not self._free_stream_work:
            await _send_json(send, 501, {"error": "free tier not configured"})
            return

        chunks: list[bytes] = []
        while True:
            msg = await receive()
            if msg["type"] == "http.request":
                chunks.append(msg.get("body") or b"")
                if not msg.get("more_body"):
                    break

        try:
            req: dict[str, Any] = json.loads(b"".join(chunks)) if chunks else {}
        except json.JSONDecodeError:
            await _send_json(send, 400, {"error": "invalid JSON body"})
            return

        # Accept "symbol" (singular) or "symbols" (list/string, first element)
        symbol_raw = req.get("symbol") or ""
        if not symbol_raw:
            syms = _parse_symbols(req.get("symbols") or "")
            symbol_raw = syms[0] if syms else ""
        symbol = str(symbol_raw).strip().upper()
        if not symbol:
            await _send_json(send, 400, {
                "error": "symbol is required",
                "example": '{"symbol": "AAPL"}',
            })
            return

        headers_dict: dict[bytes, bytes] = dict(scope.get("headers") or [])
        payment_header = (headers_dict.get(b"x-payment") or b"").decode().strip()

        if not payment_header:
            host = _host(scope)
            challenge = build_free_payment_challenge(symbol, host)
            challenge_json = json.dumps(challenge).encode()
            await send({
                "type": "http.response.start",
                "status": 402,
                "headers": [
                    (b"x-payment-required", challenge_json),
                    (b"content-type", b"application/json"),
                ],
            })
            body = json.dumps({
                "error": "Payment Required",
                "description": "Retry with a valid X-Payment header (0 U EIP-712 signature).",
                "paymentRequired": challenge,
            }).encode()
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return

        ok, msg, from_addr = verify_free_payment_proof(payment_header)
        if not ok:
            logger.warning("x402 free tier rejected for %s: %s", symbol, msg)
            await _send_json(send, 402, {
                "error": "Free tier access denied",
                "detail": msg,
            })
            return

        try:
            proof_addr, nonce = _payment_identity(payment_header)
            if proof_addr != from_addr.lower():
                raise ValueError("verified free payment identity mismatch")
            await report_competition_call(
                event_id=f"b402-free:{CHAIN_ID}:{proof_addr}:{nonce}",
                address=proof_addr,
                called_at=int(time.time() * 1000),
            )
        except ValueError:
            logger.exception("x402 verified free payment identity extraction failed")
        except Exception:
            logger.exception("x402 free-tier competition accounting failed")

        logger.info("x402 free tier: streaming quote for %s (from=%s, %s)", symbol, from_addr, msg)
        await self._stream_free_sse(send, symbol, from_addr, msg)

    async def _stream_free_sse(
        self, send, symbol: str, from_addr: str, rate_msg: str,
    ) -> None:
        """Stream SSE events for the free quick-quote tier."""
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream; charset=utf-8"),
                (b"cache-control", b"no-cache"),
                (b"x-accel-buffering", b"no"),
                (b"transfer-encoding", b"chunked"),
            ],
        })

        async def _emit(event: str, data: dict) -> None:
            frame = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            await send({
                "type": "http.response.body",
                "body": frame.encode("utf-8"),
                "more_body": True,
            })

        try:
            await _emit("progress", {
                "stage": "starting",
                "symbol": symbol,
                "message": f"Fetching quote for {symbol}... ({rate_msg})",
            })
            async for event_name, data in self._free_stream_work(symbol):
                await _emit(event_name, data)
        except Exception as exc:
            logger.exception("x402 free SSE delivery failed for %s", symbol)
            await _emit("error", {"message": str(exc)})

        await send({"type": "http.response.body", "body": b"", "more_body": False})


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
            return value.decode("utf-8", errors="ignore").strip()
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


def _payment_identity(proof_header: str) -> tuple[str, str]:
    """Extract canonical wallet and nonce after proof verification succeeded."""
    try:
        proof = json.loads(base64.b64decode(proof_header.strip()))
        authorization = (proof.get("payload") or {}).get("authorization") or {}
        address = str(authorization["from"]).lower()
        address_bytes = bytes.fromhex(address.removeprefix("0x"))
        nonce_bytes = bytes.fromhex(
            str(authorization["nonce"]).removeprefix("0x").zfill(64)
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("verified payment proof has no valid identity") from exc
    if len(address_bytes) != 20 or len(nonce_bytes) != 32:
        raise ValueError("verified payment proof has no valid identity")
    return f"0x{address_bytes.hex()}", f"0x{nonce_bytes.hex()}"


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
