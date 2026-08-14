# Agent Demo — ERC-8183 + x402 + UOMP

[![ERC-8183](https://img.shields.io/badge/Protocol-ERC--8183-blue)](https://github.com/bnb-chain/BEPs)
[![x402](https://img.shields.io/badge/Payment-x402%20v2-orange)](buyer-client/src/x402-async.ts)
[![UOMP](https://img.shields.io/badge/Context-UOMP-purple)](https://github.com/0xaicrypto/uomp-core)
[![Network](https://img.shields.io/badge/Network-BSC%20Testnet-yellow)](https://testnet.bscscan.com)
[![Python](https://img.shields.io/badge/Seller-Python%203.12-3776AB?logo=python)](stockanalyst/)
[![TypeScript](https://img.shields.io/badge/Buyer-TypeScript-3178C6?logo=typescript)](buyer-client/)
[![License](https://img.shields.io/badge/License-Apache%202.0-green)](LICENSE)

End-to-end demo of a **personalized AI stock analyst** bought and paid for on BNB Chain.

The agent aggregates 5 independent data sources (yfinance, FRED macro, SEC EDGAR insider trades, Alpha Vantage AI sentiment, GNews headlines), computes 10 technical indicators (RSI, MACD, Bollinger, MA50/200 golden/death cross, ADX trend strength, OBV, ATR, VaR 95%), and writes a structured report with explicit bull/bear thesis, portfolio P&L vs your actual cost basis, and a hard recommendation with target price.

## BSC Mainnet x402 payments

Paid asynchronous analysis supports four fixed 18-decimal tokens at exactly
0.21 token per completed payment:

| Token | BSC address | Method | Price |
| --- | --- | --- | --- |
| U | `0xcE24439F2D9C6a2289F741120FE202248B666666` | `eip3009` | 0.21 U |
| USD1 | `0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d` | `eip3009` | 0.21 USD1 |
| USDC | `0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d` | `permit2-exact` | 0.21 USDC |
| USDT | `0x55d398326f99059fF775485246999027B3197955` | `permit2-exact` | 0.21 USDT |

B402 capabilities may be partial: the seller advertises only the supported
subset returned by the live facilitator. `extra.signerAddress` is facilitator
EOA metadata; it is not the Permit2 spender and is not part of `permit2-exact`
typed data. `extra.spenderAddress` is the live B402 proxy and the
`permit2-exact` typed-data spender; the ERC-20 approval target remains
canonical Permit2 `0x000000000022D473030F116dDEE9F6B43aC78BA3`.

From `buyer-client/`, inspect and explicitly manage a USDC or USDT allowance
with `npm run x402:allowance`, `npm run x402:approve`, and
`npm run x402:revoke`; pass the token after `--`, for example
`npm run x402:approve -- USDC`. Both approve and revoke require confirmation;
`--yes` is an explicit noninteractive bypass. Approval sets an exact 50-token
cap (resetting a differing nonzero allowance to zero first), while revoke sets
it to zero. A 50-token allowance covers 238 complete 0.21 payments
and leaves 0.02 token. `BSC_RPC_URL` is used only for USDC/USDT allowance
reads, approval/revoke, and paid preflight; U/USD1 and local signing do not use
it. `npm run x402:async` never approves or revokes: it only checks that a
Permit2 allowance is between 0.21 and 50 before a new paid flow.

In promotional mode, `paymentRequired=false` and the active `accepts=[]`;
`supportedAssets` may still list all four tokens as registry metadata and is
not an active payment requirement. Promo requires an identity-only
`Wallet-Signature`, verified locally for Competition attribution. It has no
token payment proof, B402 verify/settle, Permit2 preflight, RPC, or automatic
approval. Only a freshly created Permit2
reservation in the same request uses verify-and-settle. Every pre-existing
stale Permit2 reservation is recovered settle-only with the identical
persisted proof, regardless of `pendingSettlementReference` or deadline;
recovery does not call `/verify`. Recovery creates no new signature, nonce, or
approval. See
[the x402 API usage guide](docs/x402-api-usage.md) for the HTTP contract,
selection rules, and recovery details.

Three ways to pay — pick the one that fits your use case:

| Tier | Command | Cost | Settlement | Speed |
|------|---------|------|------------|-------|
| **Free** quick quote | `npm run x402:free` | 0 U | none (identity proof only) | ~1s |
| **Paid** full analysis via x402 | `npm run x402:async` | 0.21 U, USD1, USDC, or USDT | Binance Pay facilitator | async job |
| **Paid** full analysis via ERC-8183 | `npm run dev` | 0.21 U | on-chain escrow (trustless) | 5–15 min |

- **Seller** (`stockanalyst/`) — stock analysis agent deployed on BNB Chain platform; serves both x402 (durable private jobs, EIP-3009 or Permit2 exact) and ERC-8183 (on-chain escrow, A2A + Cognito) in parallel. LLM: **kimi-k2.6** with extended thinking.
- **Buyer** (`buyer-client/`) — TypeScript client that reads the user's portfolio and cost basis from a local UOMP Guard and supports all three payment tiers.

## Architecture

```
  LOCAL (buyer machine)                      CLOUD / CHAIN
  ──────────────────────                     ─────────────

  UOMP Guard (localhost:9374)
  ├─ portfolio: AAPL ×50, NVDA ×20
  └─ profile:  moderate / 12mo
        │
        │ [1] read context
        ▼
  buyer-client (Node.js)
        │
        ├─── x402 free tier ──────────────► public x402 API Gateway
        │    sign 0-U EIP-712 proof         └─ verify sig + rate limit (10/24h)
        │    POST /x402/free                   fetch_quote() — no LLM
        │◄── JSON: quick quote table ─────────                  (~1s)
        │
        ├─── x402 paid async ─────────────► public x402 API Gateway
        │    sign exact 0.21-token proof    └─ seller agent → Binance Pay facilitator
        │    POST /x402/analyze/async          kimi-k2.6 background analysis
        │◄── jobId + private token ────────
        │    poll status → presigned URL ──► private S3 report
        │
        ├─[2]─ A2A negotiate ─────────────► seller agent (BNB Chain Platform :9000)
        │      OAuth2 token                  └─ sign quote → 0.21 U
        │◄─────────────────────────────────── signed quote
        │
        ├─[3]─ createJob ─────────────────► BSC Testnet (chain 97)
        │      registerJob                   AgenticCommerce
        │      setBudget                     U token locked in escrow
        │      approve + fund
        │
        ├─ start relay (localhost:9444)
        │  Cloudflare Tunnel ──────────────► https://xxx.trycloudflare.com
        │                                          │
        ├─[4]─ notify_funded ────────────► seller agent
        │      EIP-712 signed context       ├─ kimi-k2.6 extended thinking (~5-15min)
        │                                   ├─[5]─ submit_result ─► BSC Testnet
        │                                   └─[6]─ POST report ───► Cloudflare Tunnel
        │                                                                 │
        │◄────────────────────────────────────────────────────────────────┘
        │
        ├─[5]─ poll getJob() ─────────────► BSC Testnet → SUBMITTED
        ├─[6]─ fetch report via tunnel URL
        └─[7]─ settle (after 24h) ────────► BSC Testnet (escrow released)
```

## Payment channel comparison

| | x402 Free | x402 Paid | ERC-8183 |
|---|---|---|---|
| Cost | 0 U | 0.21 U, USD1, USDC, or USDT | 0.21 U |
| Signing | EIP-712 (0-U identity proof) | EIP-3009 or Permit2 exact | EIP-191 quote + on-chain txs |
| On-chain settlement | none | Binance Pay facilitator | escrow contract (trustless) |
| Report | quick quote table | full analysis | full analysis |
| LLM | none (yfinance only) | kimi-k2.6 | kimi-k2.6 |
| Time | ~1s | asynchronous | 5–15 min |
| Rate limit | 10/24h per wallet | none | none |

## Quick start

```bash
# Terminal 1 — start the agent locally
cd stockanalyst/app/agent
OPENAI_API_KEY=<kimi-key> WALLET_PASSWORD=<pw> python main.py

# Terminal 2 — seed UOMP portfolio context
node guard-mock.mjs

# Terminal 3 — buyer (pick one)
cd buyer-client
SYMBOL=AAPL npm run x402:free    # free: 0 U, quick quote, ~1s
npm run x402:async               # paid: exact 0.21 selected token, durable async analysis
npm run dev                      # paid: 0.21 U, full analysis, ERC-8183 trustless
```

## ERC-8183 E2E test flow

| Step | Who | Action |
|------|-----|--------|
| 1 | Buyer | Read UOMP Guard → AAPL/NVDA holdings + risk profile |
| 2 | Buyer→Seller | A2A negotiate (OAuth2) → signed quote 0.21 U |
| 3 | Buyer→Chain | createJob → registerJob → setBudget → approve → fund |
| 4 | Buyer→Seller | `notify_funded` with EIP-712 authorization from the job-client wallet |
| 5 | Seller | kimi-k2.6 extended thinking + report (~5–15 min) → submit_result → POST to tunnel |
| 6 | Buyer | Poll chain → SUBMITTED → fetch report from local relay |
| 7 | Buyer→Chain | settle (after 24h dispute window) |

## Authenticated delivery notification

Named `notify_funded` calls are authenticated separately from OAuth2. The buyer
client serializes the complete delivery context once (gateway URL and token,
portfolio, and risk profile), then its existing job-creation wallet signs that
exact string as EIP-712 typed data. The request carries the decimal `job_id`
and that authorization envelope; the seller recovers the signer against its own
chain/Commerce domain and requires it to be the on-chain job client. The
TypeScript [`notifyFunded`](buyer-client/src/negotiate.ts) helper is the supported
way to make this request.

For remote delivery, the production default accepts public HTTPS
`*.trycloudflare.com` relay origins. Local loopback development is intentionally
off by default: set `ALLOW_PRIVATE_DELIVERY_GATEWAY=true` only for an HTTP
loopback relay. Deployments that use another approved public relay origin must
configure `DELIVERY_GATEWAY_ALLOWED_HOSTS` (a comma-separated exact host or
`.suffix` allowlist). These controls do not replace the buyer signature.

## Setup

### 1. Deploy the seller

```bash
cd stockanalyst/app/agent

export WALLET_PASSWORD='<your-keystore-password>'
bag deploy agent
```

Record `agent_id` from `studio.toml [deploy.platform]` and create an OAuth2 client in the platform console.

The deployed agent exposes two ports:
- `:9000` — A2A (ERC-8183, requires Cognito Bearer token)
- `:9001` — x402 (public, `Payment-Signature` auth only) — enabled by `X402_PORT=9001` env var

The public x402 V2 exchange is `402 PAYMENT-REQUIRED: base64(PaymentRequired)`,
followed by a retry carrying `PAYMENT-SIGNATURE: base64(PaymentPayload)`, then
`202 PAYMENT-RESPONSE: base64(SettlementResponse)`.

### 2. Configure the buyer

```bash
brew install cloudflare/cloudflare/cloudflared  # macOS; required for ERC-8183 remote

cd buyer-client
npm install
cp .env.example .env
```

Edit `buyer-client/.env`:

```env
KEYSTORE_PATH=../stockanalyst/.studio/wallets/<address>.json
WALLET_PASSWORD=<your-keystore-password>

# ERC-8183 (cloud seller, requires OAuth2)
AGENT_ENDPOINT=https://bnbagent-api.bnbchain.world/v1/rt/<agent_id>/a2a
AGENT_CLIENT_ID=<client_id>
AGENT_CLIENT_SECRET=<client_secret>
PROVIDER_ADDRESS=<seller wallet address>

# x402 — defaults to localhost:9000; deployed value is the public mainnet gateway
X402_ENDPOINT=https://stock-agent.bnbchain.org
# Append /x402/price, /x402/analyze/async, or private job paths to this base.
# It contains neither /mainnet nor a trailing /x402. The old execute-api endpoint remains enabled during certificate/DNS/custom-domain validation and is disabled only after successful final cutover verification.

UOMP_GUARD_URL=http://127.0.0.1:9374
UOMP_GUARD_TOKEN=demo-guard-token
```

Buyer wallet needs: ≥ 0.01 tBNB (gas) + ≥ 0.21 U (for ERC-8183); paid x402 accepts exactly 0.21 U, USD1, USDC, or USDT when its live capability is available. The EIP-3009 subset remains 0.21 U or 0.21 USD1.

### 3. Run

**Terminal 1 — UOMP Guard mock:**
```bash
node guard-mock.mjs
```

**Terminal 2 — buyer (pick one):**
```bash
cd buyer-client

SYMBOL=AAPL npm run x402:free   # free: 0 U, quick quote, ~1s, no LLM
npm run x402:async              # paid: exact 0.21 selected token, durable async analysis
npm run dev                      # paid: 0.21 U, full analysis, ERC-8183 trustless (~5–15min)
```

After the 24-hour ERC-8183 dispute window:
```bash
cd stockanalyst/app/agent && bag erc8183 settle <job_id>
```

## Repository Structure

```
agent-demo/
├── guard-mock.mjs          UOMP Guard mock (portfolio + risk profile)
├── stockanalyst/           Seller agent
│   ├── app/agent/
│   │   ├── main.py         A2A entrypoint + x402 dual-port mode
│   │   ├── x402_handler.py x402 routes: /price  /analyze/async  /jobs  /free
│   │   ├── x402_verify.py  EIP-712 EIP-3009 verification (FIXED code, never LLM)
│   │   ├── seller_core.py  ERC-8183 negotiate / notify_funded / fulfill
│   │   ├── signing.py      Deterministic signing (never LLM tools)
│   │   ├── analysis.py     yfinance data engine + technical indicators
│   │   └── tools.py        LLM-callable read-only tools
│   └── README.md
└── buyer-client/           TypeScript buyer client
    ├── src/
    │   ├── x402free.ts     Free tier buyer  (npm run x402:free)
    │   ├── x402-async.ts   Paid x402 buyer  (npm run x402:async)
    │   ├── index.ts        ERC-8183 buyer   (npm run dev)
    │   ├── uomp.ts         UOMP memory layer
    │   ├── gateway.ts      Cloudflare Tunnel relay
    │   ├── negotiate.ts    A2A + OAuth2
    │   └── erc8183.ts      On-chain buyer ops
    └── README.md
```

## BSC Testnet Contracts (chain 97)

| Contract | Address |
|----------|---------|
| AgenticCommerce | `0xa206c0517b6371c6638cd9e4a42cc9f02a33b0de` |
| EvaluatorRouter | `0xd7d36d66d2f1b608a0f943f722d27e3744f66f25` |
| OptimisticPolicy | `0x4f4678d4439fec812ac7674bb3efb4c8f5fb78a6` |
| U Token (ERC-20) | `0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565` |

| Resource | Link |
|----------|------|
| tBNB faucet (gas) | https://testnet.bnbchain.org/faucet-smart |
| U token faucet | https://united-coin-u.github.io/u-faucet/ |
| BSC testnet explorer | https://testnet.bscscan.com |
