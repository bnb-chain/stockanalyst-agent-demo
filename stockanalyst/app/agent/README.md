# Stock Analyst seller runtime

This runtime serves paid asynchronous x402 jobs at
`https://stock-agent.bnbchain.org`: `GET /x402/price`,
`POST /x402/analyze/async`, `GET /x402/jobs/{jobId}`, and
`POST /x402/jobs/{jobId}/resume`.

- [Mainnet integration quickstart](../../../docs/x402-mainnet-quickstart.md)
- [Payment wire and security contract](../../../docs/x402-api-usage.md)

## x402 payment contract

Every accepted analysis costs exactly `100000000000000000` atomic units (0.1
of the selected 18-decimal token).

| Token | BSC address | Method | Price |
| --- | --- | --- | --- |
| U | `0xcE24439F2D9C6a2289F741120FE202248B666666` | `eip3009` | 0.1 U |
| USD1 | `0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d` | `eip3009` | 0.1 USD1 |
| USDC | `0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d` | `permit2-exact` | 0.1 USDC |
| USDT | `0x55d398326f99059fF775485246999027B3197955` | `permit2-exact` | 0.1 USDT |

B402 capabilities may be partial. `extra.signerAddress` is facilitator EOA
metadata; it is not the Permit2 spender and is not part of `permit2-exact`
typed data. `extra.spenderAddress` is the live B402 proxy and the
`permit2-exact` typed-data spender; the ERC-20 approval target remains
canonical Permit2 `0x000000000022D473030F116dDEE9F6B43aC78BA3`.

`BSC_RPC_URL` is used only for USDC/USDT allowance reads, approval/revoke, and
paid preflight. The buyer commands `npm run x402:allowance`,
`npm run x402:approve`, and `npm run x402:revoke` require explicit selection;
both approve and revoke require confirmation and `--yes` is an explicit
noninteractive bypass. `npm run x402:async` never approves or revokes. Only a
freshly created Permit2 reservation in the same request uses
verify-and-settle. Every pre-existing stale Permit2 reservation is recovered
settle-only with the identical persisted proof, regardless of
`pendingSettlementReference` or deadline; recovery does not call `/verify`.

## Admission

Verified wallet identity is admitted before B402 verification or settlement.
Admission allows 30 accepted new jobs per rolling hour for each payer wallet.
Explicit payment rejection releases a new reservation; the 31st new job gets
HTTP 429 with `Retry-After`. An exact retry does not consume another slot or
settle twice. Payment is settled before analysis and does not guarantee a
successful report; a later analysis failure does not automatically refund the
settlement, while a retryable failure resumes without another payment.
Competition delivery starts from durable settled/queued state, uses a stable
hashed `eventId`, and is asynchronous best-effort delivery that the receiver
must deduplicate.

ERC-8183 is a separate escrow channel, not x402; its fixed price remains 0.1
U and its behavior is unchanged.

## Key files

| File | Purpose |
|------|---------|
| `main.py` | Entrypoint: A2A on `:9000` + x402 on `:9000` (local) / `:9001` (platform) |
| `x402_handler.py` | x402 routes: paid price, async create/query/resume, and settlement |
| `x402_verify.py` | Fixed-code EIP-712 EIP-3009 and Permit2 exact proof verification |
| `x402_job_service.py` | Durable paid job admission, settlement, execution, and recovery |
| `x402_job_store.py` | Private durable job and idempotency storage |
| `seller_core.py` | ERC-8183 seller logic — negotiate / notify_funded / fulfill |
| `signing.py` | Deterministic ERC-8183 signing — quote / submit / settle (never LLM tools) |
| `analysis.py` | yfinance data engine + RSI/MACD/Bollinger computation |
| `tools.py` | LLM-callable read-only tools (`get_stock_quote`, `get_technical_signals`, …) |
| `studio.toml` | Agent config (wallet, LLM, pricing, x402 dual-port, storage) |

## Run locally

```bash
# From the app/agent directory
python main.py                     # single-port: paid x402 + A2A on :9000

# With env:
OPENAI_API_KEY=<kimi-key> WALLET_PASSWORD=<pw> python main.py
```

Deployed platform uses `X402_PORT=9001` to serve paid x402 on a separate
public port without the Cognito-protected A2A gateway.
