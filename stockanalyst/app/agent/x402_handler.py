"""x402 HTTP payment channel with SSE streaming delivery.

Adds three routes alongside the existing A2A server (pure ASGI middleware —
no extra framework, no new heavy deps):

  GET  /x402/price              → 200 JSON: current price + asset info (no payment)
  GET  /x402/analyze?symbols=.. → 402 JSON + X-Payment-Required header (challenge)
  POST /x402/analyze            → with X-Payment header → 200 SSE stream

Request body (POST, JSON):
  {
    "symbols": ["AAPL", "NVDA"],          # required — list or comma-string
    "analysis_type": "comprehensive"       # optional (default: "comprehensive")
  }

SSE event stream format (one event per line-pair):
  event: progress
  data: {"stage": "collecting", "tool": "get_stock_quote", "message": "Running get_stock_quote..."}

  event: progress
  data: {"stage": "generating", "message": "Rendering final report..."}

  event: report
  data: {"content": "# EQUITY RESEARCH...", "format": "markdown"}

  event: error
  data: {"message": "..."}

  event: done
  data: {}

Payment verification is FIXED CODE in x402_verify.py — never LLM-callable.
The stream_work callable is the ADK runner generator from main.py (also not LLM-priced;
it runs fixed code to collect data then calls the LLM for text only).

For dual-channel operation: ERC-8183 (trustless escrow, on-chain settlement) runs
alongside x402 (simpler Binance Pay flow, off-chain, lower friction for quick buys).
Both channels share the same LLM analysis pipeline.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
import uuid
from typing import Any, AsyncGenerator, Callable

import httpx

from x402_verify import (
    CHAIN_ID, MIN_PRICE_WEI, PRICE_WEI, SELLER_WALLET, U_TOKEN_BSC_TESTNET,
    FREE_TIER_LIMIT,
    build_payment_challenge, verify_payment_proof,
    build_free_payment_challenge, verify_free_payment_proof,
)

logger = logging.getLogger("seller-agent.x402")

# ── Settlement configuration (priority: B402 > generic facilitator > demo) ─────

# Binance Pay B402 authenticated facilitator (preferred).
# Apply for credentials at https://forms.gle/aUQvxUETfGMzyTky5
# After approval: bag env set B402_CLIENT_ID <clientId>
#                 bag env set B402_SECRET <secret>
B402_CLIENT_ID = os.environ.get("B402_CLIENT_ID", "").strip()
B402_SECRET    = os.environ.get("B402_SECRET", "").strip()
B402_BASE_URL  = os.environ.get("B402_BASE_URL", "https://bpay.binance.com").rstrip("/")

# Generic x402 facilitator (fallback — no HMAC auth).
FACILITATOR_URL = os.environ.get("X402_FACILITATOR_URL", "").rstrip("/")

# Demo / local-dev mode — explicit opt-in required.
# NEVER set this in production — signatures verified but NO token transferred.
X402_DEMO_MODE = os.environ.get("X402_DEMO_MODE", "").strip().lower() in ("1", "true", "yes")

if B402_CLIENT_ID and B402_SECRET:
    logger.info(
        "x402: B402 HMAC-SHA512 facilitator active — client_id=%s base_url=%s",
        B402_CLIENT_ID, B402_BASE_URL,
    )
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
        "The paid /x402/analyze endpoint will REJECT all requests until one is set. "
        "For production:    export B402_CLIENT_ID=<id> B402_SECRET=<secret>. "
        "For local testing: export X402_DEMO_MODE=1."
    )


def _b402_headers(payload_dict: dict) -> dict[str, str]:
    """Build Binance Pay HMAC-SHA512 authentication headers for B402 settle."""
    timestamp = int(time.time() * 1000)
    nonce = uuid.uuid4().hex[:32]
    payload_to_sign = f"{timestamp}\n{nonce}\n{json.dumps(payload_dict)}\n"
    signature = hmac.new(
        B402_SECRET.encode("utf-8"),
        payload_to_sign.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest().upper()
    return {
        "BinancePay-Timestamp": str(timestamp),
        "BinancePay-Nonce": nonce,
        "BinancePay-Certificate-SN": B402_CLIENT_ID,
        "BinancePay-Signature": signature,
        "Content-Type": "application/json",
    }


async def _settle_via_facilitator(proof_b64: str) -> tuple[bool, str]:
    """Execute on-chain settlement via configured backend.

    Priority: B402 (HMAC-SHA512) → generic facilitator → demo mode → fail closed.

    Returns (True, txHash) on success, (False, error_reason) on failure.
    """
    # Decode proof (shared by all settlement paths)
    try:
        raw   = base64.b64decode(proof_b64.strip())
        proof = json.loads(raw)
    except Exception as exc:
        return False, f"could not decode proof: {exc}"

    payload = {
        "x402Version": 2,
        "paymentPayload": proof,
        "paymentRequirements": {
            "scheme":            "exact",
            "network":           f"eip155:{CHAIN_ID}",
            "maxAmountRequired": str(PRICE_WEI),
            "asset":             U_TOKEN_BSC_TESTNET,
            "payTo":             SELLER_WALLET.lower(),
            "maxTimeoutSeconds": 60,
        },
    }

    # ── 1. Binance Pay B402 (HMAC-SHA512) ──────────────────────────────────────
    if B402_CLIENT_ID and B402_SECRET:
        return await _settle_b402(payload)

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
        "Set B402_CLIENT_ID + B402_SECRET for production, or X402_DEMO_MODE=1 for local testing."
    )


async def _settle_b402(payload: dict) -> tuple[bool, str]:
    """POST to Binance Pay B402 /papi/v2/b402/settle with HMAC-SHA512 auth."""
    url = f"{B402_BASE_URL}/papi/v2/b402/settle"
    try:
        headers = _b402_headers(payload)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            data = resp.json()
        if data.get("status") == "SUCCESS" and data.get("code") == "000000":
            txhash = str((data.get("data") or {}).get("transactionHash") or "")
            logger.info("x402 B402 settled: txHash=%s", txhash)
            return True, txhash
        reason = str(
            (data.get("data") or {}).get("errorMessage")
            or data.get("errorMessage")
            or data.get("msg")
            or f"B402 rejected (code={data.get('code')})"
        )
        logger.warning("x402 B402 rejected: %s | response=%s", reason, data)
        return False, reason
    except Exception as exc:
        logger.exception("x402 B402 call failed")
        return False, str(exc)


async def _settle_generic(payload: dict) -> tuple[bool, str]:
    """POST to a generic x402 facilitator (no HMAC auth)."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{FACILITATOR_URL}/settle",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            data = resp.json()
        if data.get("success"):
            txhash = str(data.get("transaction") or "")
            logger.info("x402 facilitator settled: txHash=%s", txhash)
            return True, txhash
        reason = str(data.get("errorReason") or data.get("error") or "facilitator rejected")
        logger.warning("x402 facilitator rejected: %s", reason)
        return False, reason
    except Exception as exc:
        logger.exception("x402 facilitator call failed")
        return False, str(exc)


class X402Handler:
    """ASGI middleware: intercepts /x402/* routes, forwards everything else.

    Mount as the outermost ASGI layer in main.py so it sits in front of the
    A2A server, the ERC-8183 local-storage route, and the JSON-RPC error-
    hardening middleware.

    Args:
        app: Inner ASGI application (A2A + existing routes).
        stream_work: Async generator factory — called as
            ``stream_work(prompt, session_id, symbols)`` and yields
            ``(event_name: str, data: dict)`` tuples. Defined in main.py
            (has access to the ADK runner + report parser/renderer); never
            imported here to avoid circular imports.
        generator: Agent identity string logged in SSE delivery metadata.
    """

    def __init__(
        self,
        app,
        *,
        stream_work: Callable[..., AsyncGenerator[tuple[str, dict], None]],
        generator: str,
        free_stream_work: Callable[..., AsyncGenerator[tuple[str, dict], None]] | None = None,
    ) -> None:
        self._inner = app
        self._stream_work = stream_work
        self._generator = generator
        self._free_stream_work = free_stream_work

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/x402"):
            await self._inner(scope, receive, send)
            return

        path = scope["path"].rstrip("/")
        method = (scope.get("method") or "GET").upper()

        if path == "/x402/price":
            await self._handle_price(scope, send)
        elif path == "/x402/analyze" and method == "GET":
            await self._handle_challenge(scope, send)
        elif path == "/x402/analyze" and method == "POST":
            await self._handle_analyze(scope, receive, send)
        elif path == "/x402/free" and method == "GET":
            await self._handle_free_challenge(scope, send)
        elif path == "/x402/free" and method == "POST":
            await self._handle_free(scope, receive, send)
        else:
            await _send_json(send, 404, {"error": "not found", "x402_routes": [
                "GET  /x402/price",
                "GET  /x402/analyze?symbols=AAPL,NVDA",
                "POST /x402/analyze  (+ X-Payment header)  → paid, 1.0 U, full report",
                "GET  /x402/free?symbol=AAPL",
                "POST /x402/free     (+ X-Payment header)  → free, 0 U, quick quote",
            ]})

    # ── Route handlers ─────────────────────────────────────────────────────────

    async def _handle_price(self, scope, send) -> None:
        """GET /x402/price — price info without payment."""
        challenge = build_payment_challenge([], "")
        accept    = (challenge.get("accepts") or [{}])[0]
        await _send_json(send, 200, {
            "x402Version":  2,
            "price_u":      "1.0",
            "price_wei":    accept.get("maxAmountRequired", str(PRICE_WEI)),
            "min_price_u":  "0.5",
            "min_price_wei": str(MIN_PRICE_WEI),
            "asset":        accept.get("asset"),
            "network":      accept.get("network"),
            "payTo":        accept.get("payTo"),
            "signingScheme": "eip3009",
            "facilitator":  FACILITATOR_URL or "(demo mode — no on-chain settlement)",
        })

    async def _handle_challenge(self, scope, send) -> None:
        """GET /x402/analyze?symbols=... — return 402 payment challenge."""
        qs = urllib.parse.parse_qs((scope.get("query_string") or b"").decode())
        symbols_raw = (qs.get("symbols") or [""])[0]
        symbols = _parse_symbols(symbols_raw)
        host = _host(scope)
        challenge = build_payment_challenge(symbols, host)
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
            "description": "Include a valid X-Payment header to access this resource.",
            "paymentRequired": challenge,
        }).encode()
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def _handle_analyze(self, scope, receive, send) -> None:
        """POST /x402/analyze — verify payment, stream SSE report."""
        # Read request body
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

        # Parse symbols
        symbols = _parse_symbols(req.get("symbols") or "")
        if not symbols:
            await _send_json(send, 400, {
                "error": "symbols is required",
                "example": '{"symbols": ["AAPL", "NVDA"]}',
            })
            return

        headers_dict: dict[bytes, bytes] = dict(scope.get("headers") or [])
        payment_header = (headers_dict.get(b"x-payment") or b"").decode().strip()

        # No payment header → return 402 challenge
        if not payment_header:
            host = _host(scope)
            challenge = build_payment_challenge(symbols, host)
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
                "description": "Retry this request with a valid X-Payment header.",
                "paymentRequired": challenge,
            }).encode()
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return

        # Step 1: verify EIP-712 signature locally (fast, no I/O)
        ok, err = verify_payment_proof(payment_header)
        if not ok:
            logger.warning("x402 payment rejected for %s: %s", symbols, err)
            await _send_json(send, 402, {
                "error": "Payment verification failed",
                "detail": err,
            })
            return

        # Step 2: settle via Binance Pay facilitator (on-chain transfer)
        ok, txhash_or_err = await _settle_via_facilitator(payment_header)
        if not ok:
            logger.warning("x402 facilitator settlement failed for %s: %s", symbols, txhash_or_err)
            await _send_json(send, 402, {
                "error": "Payment settlement failed",
                "detail": txhash_or_err,
            })
            return

        logger.info("x402 payment verified — streaming analysis for %s", symbols)
        analysis_type = str(req.get("analysis_type") or "comprehensive")
        portfolio = req.get("portfolio") or []
        risk_profile = req.get("risk_profile") or {}
        await self._stream_sse(send, symbols, analysis_type, portfolio=portfolio, risk_profile=risk_profile)

    async def _stream_sse(
        self, send, symbols: list[str], analysis_type: str,
        portfolio: list | None = None, risk_profile: dict | None = None,
    ) -> None:
        """Stream SSE events: progress → report → done."""
        from seller_core import _build_stock_analysis_prompt

        # Unique session id per delivery (avoids ADK session collision with ERC-8183 jobs)
        session_id = f"x402-{hashlib.sha256(f'{symbols}:{time.time()}'.encode()).hexdigest()[:12]}"

        # Build prompt (same pipeline as ERC-8183, including UOMP portfolio context)
        task_json = json.dumps({
            "task": f"Analyze {', '.join(symbols)}",
            "terms": {"symbols": symbols, "analysis_type": analysis_type},
        })
        prompt, effective_symbols = _build_stock_analysis_prompt(
            task_json,
            portfolio=portfolio or [],
            risk_profile=risk_profile or {},
        )

        # Start SSE response
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
                "symbols": effective_symbols or symbols,
                "message": f"Starting analysis for {', '.join(effective_symbols or symbols)}...",
            })

            async for event_name, data in self._stream_work(
                prompt, session_id, effective_symbols or symbols
            ):
                await _emit(event_name, data)

        except Exception as exc:
            logger.exception("x402 SSE delivery failed for %s", symbols)
            await _emit("error", {"message": str(exc)})

        # End stream
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _handle_free_challenge(self, scope, send) -> None:
        """GET /x402/free?symbol=AAPL — return 402 free tier challenge (0 U)."""
        qs = urllib.parse.parse_qs((scope.get("query_string") or b"").decode())
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


async def _send_json(send, status: int, data: dict) -> None:
    body = json.dumps(data, ensure_ascii=False).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})
