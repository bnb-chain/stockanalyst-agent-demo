# app/agent — Layer A (Agent / Sole Signer)

The valuable agent and **sole key-holder/signer** for the stockanalyst seller. See the [project README](../../README.md) for full documentation.

## BSC Mainnet x402 contract

| Token | BSC address | Method | Price |
| --- | --- | --- | --- |
| U | `0xcE24439F2D9C6a2289F741120FE202248B666666` | `eip3009` | 0.21 U |
| USD1 | `0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d` | `eip3009` | 0.21 USD1 |
| USDC | `0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d` | `permit2-exact` | 0.21 USDC |
| USDT | `0x55d398326f99059fF775485246999027B3197955` | `permit2-exact` | 0.21 USDT |

B402 capabilities may be partial; requirements expose only the live supported
subset. `extra.signerAddress` is facilitator EOA metadata; it is not the
Permit2 spender and is not part of `permit2-exact` typed data.
`extra.spenderAddress` is the live B402 proxy and the `permit2-exact`
typed-data spender; the ERC-20 approval target remains canonical Permit2
`0x000000000022D473030F116dDEE9F6B43aC78BA3`.
Buyer allowance operations are explicit: `npm run x402:allowance`,
`npm run x402:approve`, and `npm run x402:revoke` accept only USDC or USDT.
Both approve and revoke require confirmation; `--yes` is an explicit
noninteractive bypass. Approval caps allowance at exactly 50 tokens. A
50-token allowance covers 238 complete 0.21 payments and leaves 0.02 token.
`BSC_RPC_URL` is used only for USDC/USDT allowance reads, approval/revoke, and
paid preflight; U/USD1 and local signing do not use it. `npm run x402:async`
never approves or revokes.

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
approval. See the
[x402 API usage guide](../../../docs/x402-api-usage.md).

## Key files

| File | Purpose |
|------|---------|
| `main.py` | Entrypoint: A2A on `:9000` + x402 on `:9000` (local) / `:9001` (platform) |
| `x402_handler.py` | x402 routes: price, async create/query/resume, and free quick quote |
| `x402_verify.py` | EIP-712 EIP-3009 verification + free-tier rate limiting (FIXED code, never LLM) |
| `seller_core.py` | ERC-8183 seller logic — negotiate / notify_funded / fulfill |
| `signing.py` | Deterministic signing — quote / submit / settle (never LLM tools) |
| `analysis.py` | yfinance data engine + RSI/MACD/Bollinger computation |
| `tools.py` | LLM-callable read-only tools (`get_stock_quote`, `get_technical_signals`, …) |
| `studio.toml` | Agent config (wallet, LLM, pricing, x402 dual-port, storage) |

## Payment channels served

| Route | Auth | Cost | LLM |
|-------|------|------|-----|
| `GET /x402/free` | none | 0-U signing challenge | no |
| `POST /x402/free` | 0-U EIP-712, 10/24h per wallet | free JSON quote | no |
| `POST /x402/analyze/async` | EIP-3009 or Permit2 exact | 0.21 selected token per analysis | kimi-k2.6 |
| A2A `notify_funded` | ERC-8183 + Cognito Bearer | 0.21 U (escrow) | kimi-k2.6 |

## Run locally

```bash
# From the app/agent directory
python main.py                     # single-port: x402 + A2A on :9000

# With env:
OPENAI_API_KEY=<kimi-key> WALLET_PASSWORD=<pw> python main.py
```

Deployed platform uses `X402_PORT=9001` to run x402 on a separate public port (no Cognito gateway).
