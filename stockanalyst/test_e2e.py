#!/usr/bin/env python3
"""
Full E2E buyer test for the stock analysis agent.

Flow:
  1. negotiate  — POST to agent A2A, get signed price quote
  2. buy        — create_job → register → set_budget → fund (on-chain U escrow)
  3. notify     — tell the agent the job is funded, trigger LLM work
  4. poll       — wait for SUBMITTED status on-chain
  5. fetch      — read the Markdown report from the deliverable URL
  6. settle     — approve the job, releasing escrow to the seller

Run from the stockanalyst/ directory:
  app/agent/.venv/bin/python test_e2e.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

# ── env loading ──────────────────────────────────────────────────────────────
# Must happen before any studio SDK import; WALLET_PASSWORD unlocks the keystore.
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".studio" / ".env.local", override=True)

# ── SDK imports (after env is loaded) ────────────────────────────────────────
from bnbagent_studio_core.erc8183 import (
    buy_workflow,
    fetch_workflow,
    settle_workflow,
)
from bnbagent_studio_core.erc8183.client import _reset_cache as _reset_8183_cache
from bnbagent_studio_core.erc8183.workflows import get_job_summary

import dataclasses
import httpx
import bnbagent
import bnbagent.config as _bnbagent_cfg
from bnbagent_studio_core.erc8183.negotiate import NegotiatedTerms

# BSC testnet MegaFuel paymaster is unreliable — it accepts transactions
# but they never confirm. Disable paymaster and use direct (self-pay) broadcast.
_orig_resolve = _bnbagent_cfg.resolve_network
def _resolve_no_paymaster(name, **kw):
    nc = _orig_resolve(name, **kw)
    return dataclasses.replace(nc, use_paymaster=False)
_bnbagent_cfg.resolve_network = _resolve_no_paymaster

# Clear cached clients so they pick up the patched resolve_network
_reset_8183_cache()

# Extend tx receipt timeout to 120s (direct broadcast on BSC testnet is fast)
bnbagent.set_default_receipt_timeout(120)

# ── config ───────────────────────────────────────────────────────────────────
AGENT_ENDPOINT = "http://localhost:9000"
PROVIDER = "0x1FF095E1C5Cf4bC72a3DC54be17B6cf85043Fb67"
NETWORK = "bsc-testnet"

SYMBOLS = ["AAPL", "NVDA"]
TASK = f"Comprehensive stock analysis for {', '.join(SYMBOLS)}"
DELIVERABLES = "Comprehensive stock analysis report in Markdown: price, fundamentals, RSI-14, MACD, Bollinger Bands, risk rating"
QUALITY = "Real market data via yfinance, all technical indicators computed, bilingual EN output"

POLL_INTERVAL = 15   # seconds between on-chain status checks
POLL_TIMEOUT  = 600  # seconds before giving up (10 min)


def a2a_negotiate(endpoint: str, task: str, deliverables: str, quality: str) -> NegotiatedTerms:
    """Negotiate via A2A JSON-RPC (agent uses message/send, not a /negotiate REST endpoint)."""
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {
            "message": {
                "role": "user", "messageId": "negotiate-01",
                "parts": [{"kind": "data", "data": {
                    "skill": "negotiate",
                    "task_description": task,
                    "terms": {
                        "deliverables": deliverables,
                        "quality_standards": quality,
                    },
                }}],
            }
        },
    }
    r = httpx.post(endpoint + "/", json=payload, timeout=30)
    r.raise_for_status()
    body = r.json()
    parts = (body.get("result") or {}).get("parts") or []
    envelope = parts[0].get("data") if parts else {}
    if not envelope:
        raise RuntimeError(f"Empty A2A negotiate response: {body}")
    resp = envelope.get("response") or {}
    if not resp.get("accepted"):
        raise RuntimeError(f"Negotiate rejected: {resp.get('reason')}")
    terms_data = resp.get("terms") or {}
    price_raw = int(terms_data["price"])
    return NegotiatedTerms(
        price_raw=price_raw,
        currency=str(terms_data["currency"]),
        quote_expires_at=resp.get("quote_expires_at"),
        estimated_completion_seconds=resp.get("estimated_completion_seconds"),
        response_envelope=envelope,  # the full data dict is the anchoring envelope
    )


def step(n: int, msg: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  Step {n}: {msg}")
    print(f"{'─'*60}")


def main() -> None:
    print("\n" + "═"*60)
    print("  Stock Analysis Agent — End-to-End Test")
    print(f"  Agent:    {AGENT_ENDPOINT}")
    print(f"  Provider: {PROVIDER}")
    print(f"  Network:  {NETWORK}")
    print("═"*60)

    # ── 1. Negotiate ─────────────────────────────────────────────────────────
    step(1, "Negotiate — get signed price quote from agent")
    terms = a2a_negotiate(AGENT_ENDPOINT, TASK, DELIVERABLES, QUALITY)
    price_u = Decimal(terms.price_raw) / Decimal(10**18)
    print(f"  ✓ Accepted   price={price_u} U")
    print(f"  ✓ Estimated  completion={terms.estimated_completion_seconds}s")
    print(f"  ✓ Quote hash {terms.response_envelope.get('negotiation_hash', 'n/a')[:20]}...")

    # ── 2. Buy (on-chain) ─────────────────────────────────────────────────────
    step(2, "Buy — create & fund job on BSC testnet")
    buy = buy_workflow(
        provider=PROVIDER,
        task=TASK,
        negotiate=False,
        negotiation_envelope=terms.response_envelope,
        budget_u=price_u,
        network=NETWORK,
    )
    print(f"  ✓ Job ID     {buy.job_id}")
    print(f"  ✓ create_tx  {buy.create_tx}")
    print(f"  ✓ fund_tx    {buy.fund_tx}")
    print(f"  ✓ budget     {buy.budget_u} U")

    # ── 3. notify_funded sweep ───────────────────────────────────────────────
    step(3, "notify_funded — request a funded-job sweep")
    payload = {
        "jsonrpc": "2.0", "id": 2, "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": "notify-sweep",
                "parts": [{
                    "kind": "data",
                    "data": {"skill": "notify_funded"},
                }],
            }
        },
    }
    r = httpx.post(AGENT_ENDPOINT + "/", json=payload, timeout=30)
    r.raise_for_status()
    notify_resp = r.json()
    parts = (notify_resp.get("result") or {}).get("parts") or []
    notify_data = parts[0].get("data") if parts else {}
    status_msg = notify_data.get("status", "?")
    print(f"  ✓ Agent ACK  status={status_msg}")
    if notify_data.get("note"):
        print(f"    note: {notify_data['note']}")

    # ── 4. Poll until SUBMITTED ───────────────────────────────────────────────
    step(4, f"Poll on-chain status for job {buy.job_id}")
    deadline = time.time() + POLL_TIMEOUT
    final_status = None
    while time.time() < deadline:
        summary = get_job_summary(buy.job_id, network=NETWORK)
        current = summary.get("status", "UNKNOWN")
        elapsed = int(time.time() - (deadline - POLL_TIMEOUT))
        print(f"  [{elapsed:>4}s] status={current}  deliverable_url={summary.get('deliverable_url') or 'pending'}")
        if current in ("SUBMITTED", "COMPLETED"):
            final_status = current
            break
        if current in ("CANCELLED", "DISPUTED", "EXPIRED"):
            print(f"  ✗ Job ended with terminal status: {current}")
            sys.exit(1)
        time.sleep(POLL_INTERVAL)
    else:
        print(f"  ✗ Timed out after {POLL_TIMEOUT}s waiting for SUBMITTED")
        sys.exit(1)

    print(f"  ✓ Job reached {final_status}")

    # ── 5. Fetch deliverable ──────────────────────────────────────────────────
    step(5, "Fetch deliverable — download the Markdown report")
    fetch = fetch_workflow(buy.job_id, network=NETWORK)
    print(f"  ✓ Deliverable URL: {fetch.metadata.get('deliverable_url', 'n/a')}")
    print()
    if fetch.response:
        print("┌─ REPORT (" + "─"*50 + "┐")
        for line in fetch.response.splitlines():
            print("│ " + line)
        print("└" + "─"*52 + "┘")
    else:
        raw_url = fetch.metadata.get("deliverable_url", "")
        print(f"  (report body not returned by SDK; fetch manually: {raw_url})")

    # ── 6. Settle ─────────────────────────────────────────────────────────────
    step(6, "Settle — approve job, release escrow to seller")
    settle_tx = settle_workflow(buy.job_id, action="approve", network=NETWORK)
    print(f"  ✓ settle_tx  {settle_tx}")

    print("\n" + "═"*60)
    print(f"  ✓ E2E test PASSED — job {buy.job_id} approved on-chain")
    print("═"*60 + "\n")


if __name__ == "__main__":
    main()
