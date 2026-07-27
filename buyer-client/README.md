# Stock Analysis Agent — Buyer Client

TypeScript buyer client for the [Stock Analysis Agent](../stockanalyst/README.md). Supports three tiers — pick the one that fits your use case:

| Tier | Command | Cost | Settlement | Report | Speed |
|------|---------|------|------------|--------|-------|
| **x402 Free** | `npm run x402:free` | 0 U | none | quick quote table | ~1s |
| **x402 Paid** (Binance Pay) | `npm run x402` | 1.0 U | Binance Pay facilitator | full analysis | 40–120s |
| **ERC-8183** (on-chain escrow) | `npm run dev` | 1.0 U | trustless escrow | full analysis | 5–15 min |

The free tier proves wallet identity via a 0-U EIP-712 signature and is rate-limited to 10 requests per wallet per 24 hours. Both paid tiers read the buyer's portfolio from a local **UOMP Memory Guard** and produce the same HTML + PDF report.

---

## Architecture

```
┌─────────────── LOCAL (buyer machine) ────────────────────────────┐
│                                                                    │
│  UOMP Memory Guard  (localhost:9374)                               │
│  ├─ portfolio:holdings  (AAPL 50sh @ $178, NVDA 20sh @ $412 …)    │
│  └─ profile:risk        (moderate / 12mo)                          │
│            │                                                       │
│            ▼                                                       │
│     buyer-client (Node.js)                                         │
│            │                                                       │
│    ┌───────┴──────────┐                                            │
│    │                  │                                            │
│  x402 flow      ERC-8183 flow                                      │
│    │                  │                                            │
│ 1 POST /x402/analyze  │  A2A negotiate → createJob → fund (5 txs)  │
│   X-Payment: <proof>  │  notify_funded → poll chain → settle        │
│    │                  │                                            │
│    └──── SSE stream ──┘                                            │
│                  │                                                 │
│           saveReport()                                             │
│           stock-analysis-<id>.html  +  .pdf                        │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
                          │
               ┌──────────┴──────────┐
               │ Local (bag dev)     │  Cloud (BNB Chain Platform)
               │ localhost:9000      │  bnbagent-api.bnbchain.world
               │ x402 + ERC-8183     │  ERC-8183 only (A2A + Cognito)
               └─────────────────────┘
```

---

## Prerequisites

- **Node.js 18+** (for native `fetch` + `ReadableStream`)
- **tBNB** in your wallet (gas) — [BSC Testnet Faucet](https://testnet.bnbchain.org/faucet-smart)
- **U token** in your wallet (payment) — [U Faucet](https://united-coin-u.github.io/u-faucet/)
- **UOMP Guard** running locally on port 9374
- **Agent** running locally on port 9000 (for x402) or deployed on platform (for ERC-8183)

---

## Setup

```bash
cd buyer-client
npm install
cp .env.example .env
# Edit .env — see variable reference below
```

### Environment variables

```bash
# .env

# ── Wallet ────────────────────────────────────────────────────────
KEYSTORE_PATH=../stockanalyst/.studio/wallets/<address>.json
WALLET_PASSWORD=your_wallet_password

# ── ERC-8183 (cloud seller, requires OAuth2) ──────────────────────
AGENT_ENDPOINT=https://bnbagent-api.bnbchain.world/v1/rt/<runtime-id>/a2a
AGENT_CLIENT_ID=<cognito-client-id>        # from `bag deploy provision-cognito`
AGENT_CLIENT_SECRET=<cognito-secret>
PROVIDER_ADDRESS=0x1FF095E1C5Cf4bC72a3DC54be17B6cf85043Fb67

# ── x402 (local agent, no auth needed) ───────────────────────────
X402_ENDPOINT=http://localhost:9000        # default — set only to override

# ── UOMP Memory Guard ─────────────────────────────────────────────
UOMP_GUARD_URL=http://127.0.0.1:9374
UOMP_GUARD_TOKEN=your_guard_jwt_token
```

> **Note:** `X402_ENDPOINT` and `AGENT_ENDPOINT` are separate.
> `AGENT_ENDPOINT` is the deployed A2A path (requires Cognito auth) used by `npm run dev`.
> `X402_ENDPOINT` is the bare local agent URL used by `npm run x402` — it defaults to
> `http://localhost:9000` so you only need to add it to `.env` if you change the port.

---

## Quick start — x402 free tier (0 U, ~1s)

No LLM involved — the free tier calls `yfinance` directly and returns a markdown price table. Rate-limited to 10 requests per wallet per 24 hours.

```bash
# Terminal 1 — start agent
cd ../stockanalyst/app/agent
python main.py    # or: bag dev --agent-only

# Terminal 2 — free quote
cd buyer-client
SYMBOL=AAPL npm run x402:free
# or pass the symbol as an argument:
npm run x402:free NVDA
```

Expected output:
```
════════════════════════════════════════════════════════════
  x402 Free Tier — Quick Quote
  Wallet:   0x1FF0…Fb67
  Symbol:   AAPL
  Payment:  0 U (wallet identity proof only)
  Limit:    10 requests / 24 h per wallet
════════════════════════════════════════════════════════════

  ✓ EIP-712 proof signed (value = 0 U)
  →  Fetching market data for AAPL...
  ✓ Report received

│ ## AAPL — Apple Inc.  |  Quick Quote  2026-07-24
│
│ | Metric         | Value                       |
│ |----------------|-----------------------------|
│ | Price          | USD 321.66                  |
│ | Change         | -1.30%                      |
│ | Market Cap     | 4.72T USD                   |
│ | PE (TTM)       | 38.9x                       |
│ | Forward PE     | 33.4x                       |
│ | Analyst Target | USD 318.25 (-1.1% upside)   |
│ | Consensus      | Buy                         |
│ | Beta           | 1.10                        |
│ | 52W Range      | USD 201.50 – USD 334.99     |
│
│ > Full analysis → Paid tier (1.0 U) via POST /x402/analyze

  ✓ FREE TIER COMPLETE — 0 U · 1 signature · ~1s
```

## Quick start — x402 paid tier (1.0 U, SSE stream)

One EIP-712 EIP-3009 signature, one HTTP POST, SSE stream back. No gas, no polling.

**Terminal 1 — start the agent locally:**
```bash
cd ../stockanalyst/app/agent
python main.py
```

**Terminal 2 — seed the UOMP Guard, then run the buyer:**
```bash
cd ..
node guard-mock.mjs      # seed portfolio + risk profile into Guard

cd buyer-client
npm run x402
```

Expected output:
```
Decrypting keystore...

────────────────────────────────────────────────────────────
  Step 1: Load UOMP user context (portfolio + risk profile)
────────────────────────────────────────────────────────────
  ✓ Symbols:  AAPL, NVDA
  ✓ Holdings: 2 positions
  ✓ Risk:     moderate / 12mo

════════════════════════════════════════════════════════════
  Stock Analysis Agent — x402 Buyer
  x402 endpoint: http://localhost:9000
  Buyer:         0x1FF0…Fb67
  Symbols:       AAPL, NVDA
  Payment:       x402 v2 / EIP-712 EIP-3009 (Binance Pay facilitator)
  Delivery:      SSE stream (no polling needed)
════════════════════════════════════════════════════════════

────────────────────────────────────────────────────────────
  Step 2: Sign x402 v2 payment authorization (EIP-712 EIP-3009)
────────────────────────────────────────────────────────────
  ✓ EIP-712 proof signed (TransferWithAuthorization)
  ✓ Paying:   1.0 U → 0x1ff0…fb67
  ✓ Valid for: 10 minutes

────────────────────────────────────────────────────────────
  Step 3: POST /x402/analyze → streaming SSE report
────────────────────────────────────────────────────────────
  ⟳  get_stock_quote
  ⟳  get_technical_signals
  ⟳  get_options_sentiment
  ⟳  get_insider_activity
  ⟳  get_news_sentiment
  ⟳  get_stock_quote          ← second symbol
  ·  Generating analysis.....
  ✎  Rendering report...
  ✓ Report received (21 840 chars)

────────────────────────────────────────────────────────────
  Step 4: Save report as HTML + PDF
────────────────────────────────────────────────────────────
  ✓ HTML  stock-analysis-x402-xxxxxx.html
  ✓ PDF   stock-analysis-x402-xxxxxx.pdf
```

---

## ERC-8183 flow (cloud seller, on-chain escrow)

The full trustless flow against the deployed agent on BNB Chain Platform.

**Requires:**
- `AGENT_ENDPOINT`, `AGENT_CLIENT_ID`, `AGENT_CLIENT_SECRET` in `.env`
- `cloudflared` installed (used for the UOMP report relay)

```bash
brew install cloudflare/cloudflare/cloudflared   # first time only
```

**Terminal 1 — UOMP Guard + relay:**
```bash
node guard-mock.mjs
```

**Terminal 2 — ERC-8183 buyer:**
```bash
cd buyer-client
npm run dev
```

The 7-step flow:

| Step | What happens | Approx. time |
|------|-------------|------|
| 1 | Load UOMP portfolio context from Guard | instant |
| 2 | A2A negotiate — OAuth2 token + signed price quote | ~2s |
| 3 | `createJob → registerJob → setBudget → approve → fund` (5 txs) | ~30–60s |
| 4 | `notify_funded` — seller starts LLM analysis | instant |
| 5 | Poll chain until `SUBMITTED` | 40–120s |
| 6 | Fetch report via UOMP tunnel | instant |
| 7 | `settle` (or run manually after 24h dispute window) | ~10s |

After the 24-hour dispute window, settle manually:
```bash
cd ../stockanalyst
bag erc8183 settle <job_id>
```

---

## Source files

```
src/
├── x402free.ts     — free tier buyer    (npm run x402:free)  — 0 U, ~1s, no LLM
├── x402.ts         — paid x402 buyer   (npm run x402)       — 1 U, SSE, EIP-712
├── index.ts        — ERC-8183 buyer    (npm run dev)        — 1 U, on-chain escrow
├── erc8183.ts      — on-chain job lifecycle: createJob → fund → settle
├── negotiate.ts    — A2A JSON-RPC negotiate with OAuth2 support
├── uomp.ts         — UOMP Guard HTTP client + buildTaskFromMemory()
├── gateway.ts      — Cloudflare Tunnel relay (UOMP delivery for ERC-8183)
├── pdf-report.ts   — HTML + PDF report generation via Puppeteer
└── abi/            — Solidity ABIs: Commerce, Router, Policy, ERC-20
```

---

## Payment channels explained

### x402 free tier — wallet identity proof (0 U)

```
Buyer                              Agent (localhost:9000)
  │                                        │
  │  POST /x402/free                       │
  │  {"symbol": "AAPL"}                    │
  │  X-Payment: base64(0-U proof)  ───────▶│
  │                                        │  verify_free_payment_proof()
  │                                        │  value must = 0, rate limit 10/24h
  │                                        │  fetch_quote("AAPL")  — no LLM
  │◀── event: progress ────────────────────│
  │◀── event: report   ────────────────────│  markdown price table
  │◀── event: done     ────────────────────│
```

### x402 paid tier — Binance Pay facilitator (1.0 U)

```
Buyer                              Agent (localhost:9000)
  │                                        │
  │  POST /x402/analyze                    │
  │  {"symbols": ["AAPL","NVDA"], ...}     │
  │  X-Payment: base64(1-U proof)  ───────▶│
  │                                        │  verify_payment_proof()  ← fixed code
  │                                        │  Binance Pay facilitator → on-chain tx
  │◀── event: progress ────────────────────│  per tool-call progress
  │◀── event: progress (thinking·····) ───│  SSE heartbeat (keeps connection alive)
  │◀── event: report   ────────────────────│  full markdown report
  │◀── event: done     ────────────────────│
```

Both tiers use **x402 v2 / EIP-712 EIP-3009 (TransferWithAuthorization)**. The client signs structured typed data; no `eth_sign` / `personal_sign` involved:

```typescript
// ethers v6 signTypedData — domain matches the U token contract on BSC Testnet
const sig = await wallet.signTypedData(
  { name: "U", version: "1", chainId: 97,
    verifyingContract: "0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565" },
  { TransferWithAuthorization: [
      { name: "from",        type: "address" },
      { name: "to",          type: "address" },
      { name: "value",       type: "uint256" },  // 0 (free) or 1e18 (paid)
      { name: "validAfter",  type: "uint256" },
      { name: "validBefore", type: "uint256" },
      { name: "nonce",       type: "bytes32" },
    ]},
  { from, to, value: BigInt(priceWei), validAfter: 0n,
    validBefore: BigInt(now + 600), nonce }
);

// x402 v2 wire format
const proof = {
  x402Version: 2, scheme: "exact", network: "eip155:97",
  payload: { signature: sig, authorization: { from, to, value, validAfter, validBefore, nonce } },
};
// X-Payment header: Buffer.from(JSON.stringify(proof)).toString("base64")
```

The agent verifies: EIP-712 signature recovers to `from`, `to` == seller wallet, `value` ≥ 0.5 U (paid) or == 0 (free), not expired, nonce not reused.

**curl example:**
```bash
# Get price / challenge
curl http://localhost:9000/x402/price
curl "http://localhost:9000/x402/free?symbol=AAPL"
curl "http://localhost:9000/x402/analyze?symbols=AAPL,NVDA"

# Stream analysis (generate proof with the script in x402_verify.py docstring)
curl -N -X POST http://localhost:9000/x402/analyze \
  -H "Content-Type: application/json" \
  -H "X-Payment: <base64-proof>" \
  -d '{"symbols": ["AAPL", "NVDA"]}'

# Free quick quote
curl -N -X POST http://localhost:9000/x402/free \
  -H "Content-Type: application/json" \
  -H "X-Payment: <base64-0u-proof>" \
  -d '{"symbol": "AAPL"}'
```

### ERC-8183 — on-chain trustless escrow

```
Buyer                      BSC Testnet contracts         Agent (cloud)
  │                               │                           │
  ├── createJob ─────────────────▶│                           │
  ├── registerJob ───────────────▶│                           │
  ├── setBudget  ────────────────▶│                           │
  ├── approve (U token) ─────────▶│                           │
  ├── fund (lock 1 U in escrow) ─▶│                           │
  │                               │                           │
  ├── notify_funded ──────────────┼──────────────────────────▶│
  │                               │      LLM analysis (40–120s)│
  │                               │◀─── submit_result ─────────┤
  │◀── poll SUBMITTED ────────────┤                           │
  │                               │                           │
  ├── settle ─────────────────────▶│  (releases U to seller)   │
```

---

## Building your own buyer client

To call the agent from your own code, here are the minimal integration points:

### x402 (simplest — any language/framework)

1. **GET** `http://<agent>:9000/x402/price` → read `payTo`, `maxAmountRequired`, `network`
2. Sign the canonical message with the buyer's wallet (EIP-191 personal_sign)
3. **POST** `http://<agent>:9000/x402/analyze` with `X-Payment: <base64-proof>` and `{"symbols": [...]}` body
4. Consume the `text/event-stream` response, parse `event:` / `data:` pairs

### ERC-8183 (TypeScript SDK)

Reuse the classes in `src/erc8183.ts` and `src/negotiate.ts`:

```typescript
import { ERC8183Buyer, CONTRACTS } from "./src/erc8183.js";
import { negotiate, notifyFunded } from "./src/negotiate.js";
import { GuardUserMemory, buildTaskFromMemory } from "./src/uomp.js";

const wallet  = await Wallet.fromEncryptedJson(keystoreJson, password);
const buyer   = new ERC8183Buyer(wallet);
const memory  = new GuardUserMemory();
const { symbols, task, deliverables, quality, portfolio, riskProfile } =
  await buildTaskFromMemory(memory);

const envelope = await negotiate(agentEndpoint, task, deliverables, quality);
const priceU   = Number(BigInt(envelope.response.terms.price)) / 1e18;

const buy = await buyer.buy({
  provider:    PROVIDER_ADDRESS,
  description: JSON.stringify(envelope),
  budgetU:     String(priceU),
});

await notifyFunded(agentEndpoint, buy.jobId, { portfolio, riskProfile });
await buyer.pollUntilSubmitted(buy.jobId);
const url = await buyer.getDeliverableUrl(buy.jobId, buy.fundTxBlock);
```

---

## BSC Testnet contract addresses

| Contract | Address |
|---------|---------|
| AgenticCommerce | `0xa206c0517b6371c6638cd9e4a42cc9f02a33b0de` |
| EvaluatorRouter | `0xd7d36d66d2f1b608a0f943f722d27e3744f66f25` |
| OptimisticPolicy | `0x4f4678d4439fec812ac7674bb3efb4c8f5fb78a6` |
| U Token (ERC-20) | `0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565` |

Chain ID: **97** (BSC Testnet)

---

## UOMP — user-owned portfolio context

The buyer client reads portfolio context from a local **UOMP Memory Guard** at `localhost:9374`. The Guard stores data on the buyer's machine only; the agent sees context exactly as passed by the buyer on each request.

Data seeded for this demo (`guard-mock.mjs`):

```jsonc
// tag: portfolio:holdings
[
  { "symbol": "AAPL", "shares": 50, "avgCost": 178.30, "currency": "USD" },
  { "symbol": "NVDA", "shares": 20, "avgCost": 412.50, "currency": "USD" }
]

// tag: profile:risk
{
  "tolerance": "moderate",
  "horizonMonths": 12,
  "preferredIndicators": ["RSI-14", "MACD", "Bollinger Bands", "MA50/200", "ADX"]
}
```

The agent uses this to personalize the report: real P&L vs cost basis, risk-adjusted stop-loss levels, and position-specific rebalancing recommendations.

---

## Resources

| Resource | Link |
|---------|------|
| tBNB faucet (gas) | https://testnet.bnbchain.org/faucet-smart |
| U token faucet | https://united-coin-u.github.io/u-faucet/ |
| BSC Testnet Explorer | https://testnet.bscscan.com |
| Agent source | [../stockanalyst/app/agent/](../stockanalyst/app/agent/) |
| ERC-8183 spec | https://github.com/bnb-chain/BEPs |
| UOMP protocol | https://github.com/0xaicrypto/uomp-core |
