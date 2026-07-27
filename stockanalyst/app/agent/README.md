# app/agent — Layer A (Agent / Sole Signer)

The valuable agent and **sole key-holder/signer** for the stockanalyst seller. See the [project README](../../README.md) for full documentation.

## Key files

| File | Purpose |
|------|---------|
| `main.py` | Entrypoint: A2A on `:9000` + x402 on `:9000` (local) / `:9001` (platform) |
| `x402_handler.py` | x402 routes: `GET /x402/price`, `GET|POST /x402/analyze`, `GET|POST /x402/free` |
| `x402_verify.py` | EIP-712 EIP-3009 verification + free-tier rate limiting (FIXED code, never LLM) |
| `seller_core.py` | ERC-8183 seller logic — negotiate / notify_funded / fulfill |
| `signing.py` | Deterministic signing — quote / submit / settle (never LLM tools) |
| `analysis.py` | yfinance data engine + RSI/MACD/Bollinger computation |
| `tools.py` | LLM-callable read-only tools (`get_stock_quote`, `get_technical_signals`, …) |
| `studio.toml` | Agent config (wallet, LLM, pricing, x402 dual-port, storage) |

## Payment channels served

| Route | Auth | Cost | LLM |
|-------|------|------|-----|
| `POST /x402/free` | 0-U EIP-712, 10/24h per wallet | free | no |
| `POST /x402/analyze` | 1-U EIP-712 EIP-3009 | 1.0 U | kimi-k2.6 |
| A2A `notify_funded` | ERC-8183 + Cognito Bearer | 1.0 U (escrow) | kimi-k2.6 |

## Run locally

```bash
# From the app/agent directory
python main.py                     # single-port: x402 + A2A on :9000

# With env:
OPENAI_API_KEY=<kimi-key> WALLET_PASSWORD=<pw> python main.py
```

Deployed platform uses `X402_PORT=9001` to run x402 on a separate public port (no Cognito gateway).
