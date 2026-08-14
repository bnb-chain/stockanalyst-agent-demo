# Stock Analysis Agent — Buyer Client

TypeScript buyer client for the [Stock Analysis Agent](../stockanalyst/README.md). Supports these flows:

## Paid x402 contract

x402 payments use **BSC Mainnet (chain ID 56)**. The EIP-3009 domain names are United Stables for U and World Liberty Financial USD for USD1. Every accepted analysis costs exactly `100000000000000000` atomic units (0.1 of the selected 18-decimal token).

| Token | BSC address | Method | Price |
| --- | --- | --- | --- |
| U | `0xcE24439F2D9C6a2289F741120FE202248B666666` | `eip3009` | 0.1 U |
| USD1 | `0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d` | `eip3009` | 0.1 USD1 |
| USDC | `0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d` | `permit2-exact` | 0.1 USDC |
| USDT | `0x55d398326f99059fF775485246999027B3197955` | `permit2-exact` | 0.1 USDT |

B402 capabilities may be partial; choose one live supported requirement. U and USD1 use EIP-3009; USDC and USDT use `permit2-exact`. `extra.signerAddress` is facilitator EOA metadata; it is not the Permit2 spender and is not part of `permit2-exact` typed data. `extra.spenderAddress` is the live B402 proxy and the `permit2-exact` typed-data spender; the ERC-20 approval target remains canonical Permit2 `0x000000000022D473030F116dDEE9F6B43aC78BA3`.

`BSC_RPC_URL` is used only for USDC/USDT allowance reads, approval/revoke, and paid preflight. Use `npm run x402:allowance`, `npm run x402:approve`, and `npm run x402:revoke`; both approve and revoke require confirmation and `--yes` is an explicit noninteractive bypass. `npm run x402:async` never approves or revokes. Only a freshly created Permit2 reservation in the same request uses verify-and-settle. Every pre-existing stale Permit2 reservation is recovered settle-only with the identical persisted proof, regardless of `pendingSettlementReference` or deadline; recovery does not call `/verify`.

After local cryptographic payment-proof verification identifies the wallet, admission allows 30 accepted new jobs per rolling hour. The 31st request returns HTTP 429 and `Retry-After` before B402 verification or settlement. An exact retry does not consume another slot or settle twice. Competition reporting occurs once after terminal settlement or queued state using `settledAt`.

ERC-8183 is a separate on-chain escrow flow, not x402; its fixed price remains 0.21 U and its settlement and delivery behavior are unchanged.

Both full-analysis channels read the buyer's portfolio from a local **UOMP Memory Guard** and produce the same HTML + PDF report.

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
│    ┌────────────┴───────────┐                                      │
│    │                        │                                      │
│  x402 async           ERC-8183 flow                                │
│    │                        │                                      │
│  POST /x402/analyze/async   │  A2A negotiate → createJob → fund    │
│  receive jobId + token      │  notify_funded → poll chain → settle │
│  poll → private download    │                                      │
│    └────────────┬───────────┘                                      │
│                 │                                                  │
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
- For x402 paid async: **U, USD1, USDC, or USDT on BSC Mainnet**, provided the
  token appears in the live capability subset. U/USD1 use EIP-3009;
  USDC/USDT require a separately managed Permit2 allowance.
- For ERC-8183 only: **tBNB** for gas and the testnet U token —
  [BSC Testnet Faucet](https://testnet.bnbchain.org/faucet-smart)
- **UOMP Guard** running locally on port 9374
- **Agent** running locally on port 9000 (for x402) or deployed on platform (for ERC-8183)

---

## Authenticated `notify_funded`

For a named job, do not construct a raw notification payload. Use the signing
helper with the same wallet that created the on-chain job:

```ts
const status = await notifyFunded(AGENT_ENDPOINT, wallet, jobId, {
  gatewayUrl: relay.publicUrl,
  gatewayToken: relay.token,
  portfolio,
  riskProfile,
});
```

The helper serializes the complete context exactly once and creates an EIP-712
authorization for the decimal job ID. The seller uses its configured chain and
Commerce contract as the domain, recovers the signer, and accepts the context
only when that signer is the on-chain job client. Gateway, token, portfolio,
and risk-profile fields therefore must not be sent outside the signed context.

## Authenticated payload delivery

The `deliverable_url` written on-chain is a public locator, not an access
credential. The gateway token stays in the signed off-chain `notify_funded`
context. The seller uses it for `POST /v1/payload/upload`, and the buyer and
seller use it as a Bearer token for `GET` and `HEAD` payload requests. Payload
IDs or on-chain URLs alone do not authorize a read, and credentials are never
placed in the URL, query string, fragment, or on-chain metadata.

The buyer attaches its token only to a canonical
`/v1/payload/pay_<32 lowercase hex>` URL whose origin exactly matches its
public or local relay, and rejects redirects for authenticated downloads.
Cross-origin and noncanonical URLs are fetched without relay credentials so a
seller-controlled locator cannot disclose the token.

The in-memory relay limits each upload to 2 MiB, all stored plus in-flight data
to 16 MiB, and stored plus active payload slots to 32. It returns `413 Payload
Too Large` for a per-upload overflow and `507 Insufficient Storage` when the
aggregate byte or slot limit is exhausted. Existing payloads are not evicted.

Production accepts public HTTPS `*.trycloudflare.com` relay origins by default.
For a local HTTP loopback relay only, set
`ALLOW_PRIVATE_DELIVERY_GATEWAY=true` on the seller. To use an alternative
approved public origin, set the seller's `DELIVERY_GATEWAY_ALLOWED_HOSTS` to a
comma-separated exact hostname or `.suffix` allowlist. Keep this allowlist as
narrow as possible.

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
# AWS AgentCore only: exact Cognito token endpoint and resource-server scope
# AGENT_TOKEN_URL=https://<domain>.auth.<region>.amazoncognito.com/oauth2/token
# AGENT_OAUTH_SCOPE=<resource-server-identifier>/<scope>
# AGENT_SESSION_ID=<stable-session-id-at-least-33-characters>
# Required when the seller delivers through AWS S3/CloudFront rather than the
# buyer-hosted relay. The existing mode name remains `ipfs` for compatibility.
DELIVERY_MODE=ipfs
PROVIDER_ADDRESS=0x1FF095E1C5Cf4bC72a3DC54be17B6cf85043Fb67

# ── x402 (BSC Mainnet; local agent may use localhost) ─────────────
X402_ENDPOINT=https://stock-agent.bnbchain.org
X402_SELLER_WALLET=0xd10BdDC20E4DC42A1a19a9653e994991e25b8153
# Optional strict token selector: U (default), USD1, USDC, or USDT.
X402_PAYMENT_TOKEN=U
# Optional async-client polling deadline; default is 30 minutes.
X402_POLL_TIMEOUT_MS=1800000

# ── UOMP Memory Guard ─────────────────────────────────────────────
UOMP_GUARD_URL=http://127.0.0.1:9374
UOMP_GUARD_TOKEN=your_guard_jwt_token
```

> **Note:** `X402_ENDPOINT` and `AGENT_ENDPOINT` are separate.
> `AGENT_ENDPOINT` is the deployed A2A path (requires Cognito auth) used by `npm run dev`.
> `X402_ENDPOINT` is the public base URL for the deployed mainnet x402 gateway,
> never the raw AgentCore invocation URL. Append `/x402/price`,
> `/x402/analyze/async`, or private job paths; the base contains neither
> `/mainnet` nor a trailing `/x402`. The old execute-api endpoint remains enabled during certificate/DNS/custom-domain validation and is disabled only after successful final cutover verification. Local development may instead use `http://localhost:9000`.

### AWS AgentCore runtime

For a self-hosted AWS AgentCore seller, set `AGENT_ENDPOINT` to the raw runtime
invocation URL (not a platform `/a2a` URL):

```text
https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<agent-runtime-arn>/invocations?qualifier=DEFAULT
```

Its Cognito issuer is separate from that runtime URL, so set these overrides
exactly as issued by Cognito and its resource server:

```dotenv
AGENT_TOKEN_URL=https://<domain>.auth.<region>.amazoncognito.com/oauth2/token
AGENT_OAUTH_SCOPE=<resource-server-identifier>/<scope>
# Optional; must contain at least 33 characters. If omitted, the buyer creates
# one stable value for its process.
AGENT_SESSION_ID=<stable-session-id-at-least-33-characters>
# Disable the buyer relay so the seller's S3/CloudFront URL is fetched directly.
# `ipfs` is the existing external-delivery mode name and is intentionally retained.
DELIVERY_MODE=ipfs
```

For an AgentCore runtime, the buyer sends the same session value in
`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` on both `negotiate` and
`notify_funded`. It does not log OAuth credentials, access tokens, or session
values. Leave the three AgentCore overrides unset for the existing
managed-platform and local flows. Set `DELIVERY_MODE=ipfs` whenever the seller
uses the AWS S3/CloudFront delivery path.

---

## Quick start — x402 paid async

This flow completes payment quickly, stores a private job receipt, and polls
while the report runs in the background. The challenge advertises the partial
live subset of U, USD1, USDC, and USDT in registry order. The CLI selects U by
default; integrators can pass any exact registered symbol as the preferred-token
argument to `fetchPaymentChallenge`.

```bash
cd buyer-client
npm run x402:async
```

The receipt is stored at `.agent-data/x402-job-receipt.json` with owner-only
permissions (`0600`). It contains only the job ID, private job token, status
path, and expiry. If the process or network is interrupted, run the same command
again to continue the existing job without another payment. The receipt is
deleted only after the Markdown has been downloaded and the HTML report has
been saved successfully.

Before the first payment POST, the CLI atomically stores the exact signed proof
and request in `.agent-data/x402-pending-create.json` at mode `0600`. Ambiguous
network or service failures retry that same proof with bounded backoff, including
after a process restart, so the client does not create a second payment
authorization. The pending record is removed only after the four-field job
receipt has been durably written and an authenticated job-status read shows
that the server has left `settling` (or the job has expired). If settlement
ownership is lost, the client retains and resubmits that exact proof after the
stale-settlement interval; it never signs a replacement proof for the existing
job. Before persisting the receipt, the client durably binds the pending proof,
request, and job ID with a versioned HMAC-SHA256 keyed by the private job token.
Restart verifies that complete binding in constant time before any recovery or
cleanup request. Cleanup first atomically quarantines the pending file and
deletes it only after its full identity and binding match, so a concurrently
replaced file is never overwritten or removed.

Only one async CLI may run at a time. `.agent-data/x402-async.lock` is acquired
before signing or submitting payment and concurrent invocations fail fast.
Normal completion, `SIGINT`, and `SIGTERM` remove the lock only when it is still
owned by that process. `SIGKILL`, a runtime crash, or a host failure can leave a
stale lock because no cleanup handler can run. In that case, first verify that
no async client process is running, then manually remove
`.agent-data/x402-async.lock`; the pending payment proof is already stored
separately for safe recovery.

The report stays in private AWS S3 storage. Its presigned download URL is valid
for 30 minutes and is refreshed automatically when necessary; the URL and job
token are never printed. Downloads require HTTPS, a recognized AWS S3 hostname,
and no redirects. The client rejects report bodies larger than 2 MiB.

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
├── x402-async.ts   — durable paid buyer (npm run x402:async) — private job polling
├── x402-async-client.ts — typed async create/poll/resume/download client
├── x402-payment.ts — shared side-effect-free EIP-3009 proof builder
├── index.ts        — ERC-8183 buyer    (npm run dev)        — 0.21 U, on-chain escrow
├── erc8183.ts      — on-chain job lifecycle: createJob → fund → settle
├── negotiate.ts    — A2A JSON-RPC negotiate with OAuth2 support
├── uomp.ts         — UOMP Guard HTTP client + buildTaskFromMemory()
├── gateway.ts      — Cloudflare Tunnel relay (UOMP delivery for ERC-8183)
├── pdf-report.ts   — HTML + PDF report generation via Puppeteer
└── abi/            — Solidity ABIs: Commerce, Router, Policy, ERC-20
```

---

## Payment channels explained

### x402 async — paid analysis

```
Buyer                              Agent (localhost:9000)
  │                                        │
  │  POST /x402/analyze/async              │
  │  {"symbols": ["AAPL","NVDA"], ...}     │
  │  (without PAYMENT-SIGNATURE) ─────────▶│
  │◀── 402 live supported subset + resource│  amount 100000000000000000
  │  sign exact returned requirement       │
  │  PAYMENT-SIGNATURE: base64(selected proof) ▶ validate → B402 verify/settle
  │◀── 202 jobId + private jobToken ───────│
  │  GET /x402/jobs/{jobId} ──────────────▶│  background analysis
  │◀── queued / running / succeeded ───────│
  │  GET private presigned S3 URL ────────▶│  full markdown report
```

The current paid flow uses **x402 v2** on **BSC Mainnet (chain ID 56)**.
U/USD1 use EIP-712 EIP-3009 `TransferWithAuthorization`; USDC/USDT use the
exact Permit2 proof described above. The retained legacy free client is U-only.
Paid requirements may select these exact tokens:

| Symbol | Method | Domain name/version | Decimals | Exact paid amount | Mainnet contract |
|--------|--------|---------------------|----------|-------------------|------------------|
| U | `eip3009` | United Stables / 1 | 18 | `100000000000000000` (`0.1 × 10^18`) | `0xcE24439F2D9C6a2289F741120FE202248B666666` |
| USD1 | `eip3009` | World Liberty Financial USD / 1 | 18 | `100000000000000000` (`0.1 × 10^18`) | `0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d` |
| USDC | `permit2-exact` | USD Coin / 2 | 18 | `100000000000000000` (`0.1 × 10^18`) | `0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d` |
| USDT | `permit2-exact` | Tether USD / 1 | 18 | `100000000000000000` (`0.1 × 10^18`) | `0x55d398326f99059fF775485246999027B3197955` |

Set `X402_PAYMENT_TOKEN` to exactly `U`, `USD1`, `USDC`, or `USDT`; the default
is U and any other spelling is rejected. The seller's fixed registry controls
these assets. Legacy `X402_TOKEN_ADDRESS` and U-domain environment settings are
compatibility inputs only and cannot replace or override the registry.

Every paid create first verifies the wallet signature locally, then applies the durable per-wallet rolling-hour admission limit before B402 verification or settlement. A wallet receives at most 30 accepted new jobs per rolling hour across replicas and restarts; exact retry reuses the existing reservation and does not settle twice.

For the U/USD1 EIP-3009 method, the client signs the following structured typed
data; no `eth_sign` / `personal_sign` is involved. Permit2 uses the separate
exact returned requirement described above:

```typescript
// ethers v6 signTypedData — use the explicitly selected mainnet requirement
const token = PAYMENT_TOKENS[selectedToken]; // "U" or "USD1"
const sig = await wallet.signTypedData(
  { name: token.name, version: token.version, chainId: 56,
    verifyingContract: token.asset },
  { TransferWithAuthorization: [
      { name: "from",        type: "address" },
      { name: "to",          type: "address" },
      { name: "value",       type: "uint256" },  // exactly 100000000000000000
      { name: "validAfter",  type: "uint256" },
      { name: "validBefore", type: "uint256" },
      { name: "nonce",       type: "bytes32" },
    ]},
  { from, to, value: BigInt(priceWei), validAfter: 0n,
    validBefore: BigInt(now + 600), nonce }
);

// Paid B402 V2 wire format. `accepted` and `resource` are copied from the 402.
const proof = {
  x402Version: 2, resource, accepted,
  payload: { signature: sig, authorization: { from, to, value, validAfter, validBefore, nonce } },
};
// Payment-Signature header: Buffer.from(JSON.stringify(proof)).toString("base64")
```

In paid mode, the agent verifies that the EIP-712 signature recovers to `from`, the selected
asset/domain pair is registered, `to` is the seller wallet, the paid value is
exactly 0.1 token (`100000000000000000` atomic units), the proof is unexpired, and the nonce
has not been reused.

The V2 wire exchange is:

```text
402: PAYMENT-REQUIRED: base64(PaymentRequired)
retry: PAYMENT-SIGNATURE: base64(PaymentPayload)
202: PAYMENT-RESPONSE: base64(SettlementResponse)
```

Provider output is retried automatically twice after an empty result. If all
three attempts are empty, the settled job reports `analysis_empty_response`
with `retryable: true`. Resume that same job with its private job token; resume
does not send a payment signature and never verifies or settles payment again.

**curl example:**
```bash
# Get price / challenge
curl "$X402_ENDPOINT/x402/price"

# First request: receive HTTP 402 and the live supported requirements
curl -X POST "$X402_ENDPOINT/x402/analyze/async" \\
  -H "Content-Type: application/json" \\
  -d '{"symbols": ["AAPL", "NVDA"]}'

# Paid mode: repeat the request with the exact proof returned by the HTTP 402
curl -X POST "$X402_ENDPOINT/x402/analyze/async" \
  -H "Content-Type: application/json" \
  -H "Payment-Signature: <base64-proof>" \
  -d '{"symbols": ["AAPL", "NVDA"]}'

```

### ERC-8183 — on-chain trustless escrow

```
Buyer                      BSC Testnet contracts         Agent (cloud)
  │                               │                           │
  ├── createJob ─────────────────▶│                           │
  ├── registerJob ───────────────▶│                           │
  ├── setBudget  ────────────────▶│                           │
  ├── approve (U token) ─────────▶│                           │
  ├── ERC-8183 fund (lock 0.21 U in escrow) ▶│                    │
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

### x402 async (simplest — any language/framework)

1. **POST** `/x402/analyze/async` without `Payment-Signature` and require HTTP 402.
2. Validate every advertised `accepts[]` entry
   against the four-token mainnet registry, reject duplicates or unknown
   assets, and explicitly select one live-supported token. The paid amount must be exactly
   `100000000000000000` and
   include method-appropriate B402 metadata.
3. Copy the complete `resource`, selected requirement, and `extra`. Sign the
   EIP-3009 authorization for U/USD1 or the exact Permit2 authorization for
   USDC/USDT, then repeat the POST with the V2 proof in `Payment-Signature`.
4. Persist the returned private job receipt.
5. Poll the status path with `X-Job-Token`; resume when instructed.
6. Download the report from the returned private presigned URL.

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

## ERC-8183-only BSC Testnet contract addresses

| Contract | Address |
|---------|---------|
| AgenticCommerce | `0xa206c0517b6371c6638cd9e4a42cc9f02a33b0de` |
| EvaluatorRouter | `0xd7d36d66d2f1b608a0f943f722d27e3744f66f25` |
| OptimisticPolicy | `0x4f4678d4439fec812ac7674bb3efb4c8f5fb78a6` |
| U Token (ERC-20) | `0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565` |

Chain ID: **97** (BSC Testnet). These addresses belong to the ERC-8183 demo and
must not be used for x402, which uses the mainnet registry above.

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
| ERC-8183 tBNB faucet (gas) | https://testnet.bnbchain.org/faucet-smart |
| ERC-8183 testnet U token faucet | https://united-coin-u.github.io/u-faucet/ |
| ERC-8183 BSC Testnet Explorer | https://testnet.bscscan.com |
| x402 BSC Mainnet Explorer | https://bscscan.com |
| Agent source | [../stockanalyst/app/agent/](../stockanalyst/app/agent/) |
| ERC-8183 spec | https://github.com/bnb-chain/BEPs |
| UOMP protocol | https://github.com/0xaicrypto/uomp-core |
