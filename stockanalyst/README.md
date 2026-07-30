# Stock Analysis Agent

[![ERC-8183](https://img.shields.io/badge/Protocol-ERC--8183-blue)](https://github.com/bnb-chain/BEPs)
[![UOMP](https://img.shields.io/badge/Context-UOMP-purple)](https://github.com/0xaicrypto/uomp-core)
[![Network](https://img.shields.io/badge/Network-BSC%20Testnet-yellow)](https://testnet.bscscan.com)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python)](https://www.python.org)

> [中文](#中文) | English

---

## Why a Blockchain-Settled Stock Analyst?

Traditional financial research has a structural problem: the analyst gets paid regardless of whether the work is useful. This agent flips that model — payment is locked in a smart contract and only released after the agent submits a verifiable deliverable on-chain. The buyer can dispute within 24 hours. The incentive for quality is built into the settlement mechanism.

Beyond payment, the agent is personalized: it reads the buyer's actual portfolio cost basis and risk profile from a locally-controlled UOMP Memory Guard and calculates real P&L figures — not generic market commentary.

---

## 1. Analysis Engine

### Data Sources (5 independent feeds)

The agent aggregates data from five sources before writing a single word of analysis. Each source covers a dimension that the others cannot:

| Source | What it provides | Key |
|--------|-----------------|-----|
| **yfinance** | Real-time price, fundamentals (PE/PB/PEG/beta), 1-year daily OHLCV, options chain | None |
| **FRED** | Fed funds rate, 10Y treasury yield, CPI YoY, unemployment rate | `FRED_API_KEY` |
| **SEC EDGAR** | Form 4 insider trades (past 90 days) | None (public) |
| **Alpha Vantage** | AI-scored news sentiment per ticker (–1 bearish → +1 bullish) | `ALPHA_VANTAGE_API_KEY` |
| **GNews** | Top 5 recent headlines by company name | `GNEWS_API_KEY` |

If an API key is absent, that data block is skipped — the agent continues with what it has. SEC EDGAR and yfinance always run.

### Technical Indicators

All computed from 1 year of daily price history:

| Indicator | Signal |
|-----------|--------|
| **RSI-14** | Overbought (>70) / oversold (<30) |
| **Weekly RSI** | Longer-cycle momentum (filters daily noise) |
| **MACD** | Crossover direction + histogram momentum |
| **Bollinger Bands** | Position within bands (0 = lower, 1 = upper) |
| **MA50 / MA200** | Price vs moving averages + golden/death cross detection |
| **ADX** | Trend strength (>25 = strong trend) + directional bias (+DI vs –DI) |
| **OBV** | Volume confirms or diverges from price direction |
| **ATR-14** | Daily volatility in % of price — used for position sizing |
| **VaR 95%** | Maximum daily loss on 95% of trading days (historical) |
| **CVaR 95%** | Average loss on the worst 5% of days (expected shortfall) |

Options sentiment (no API key required):
- **Put/Call ratio** (volume and open interest) — PCR > 1.2 = bearish hedging; < 0.7 = bullish
- **Implied volatility** weighted by open interest across the nearest expiration

### Two-Stage Analysis Logic

The agent uses a disciplined two-stage workflow to prevent hallucination:

```
Stage 1 — Data Collection (tool calls, no text output)
  For EACH symbol (explicit per-symbol checklist in the prompt):
    get_stock_quote()        → price, PE, PB, PEG, beta, analyst target
    get_technical_signals()  → all 10 indicators above
    get_options_sentiment()  → put/call ratio, implied vol
    get_insider_activity()   → SEC Form 4 filing count (90 days)
    get_news_sentiment()     → Alpha Vantage score + GNews headlines
  Once:
    get_macro_context()      → FRED rates + VIX

Stage 2 — Report Writing (structured Markdown, no new fetches)
  Only uses numbers returned by Stage 1 tools.
  Never fabricates a price, RSI value, or sentiment score.
  Must produce a complete analysis block for EVERY symbol — enforced by
  an explicit per-symbol report outline and MANDATORY RULE #6.
```

This separation is enforced in the agent instruction: the LLM is told to collect all data first, then write. Each metric in the report maps to a specific tool call result.

### Extended Thinking

The agent runs on **Kimi K2.6** (`kimi-k2.6`, 256k context window) via `api.moonshot.cn`. This model supports extended thinking: the LLM reasons through the analysis before writing, which improves recommendation quality and consistency.

- Thinking content is **filtered from the output** — only the final formatted report reaches the buyer
- Analysis for a multi-stock portfolio typically takes **5–15 minutes** depending on the number of symbols
- The delivery timeout is set to **1800s (30 minutes)** to accommodate deep analysis sessions
- The 256k context window comfortably fits tool call results for 10+ symbols plus the full report

### Report Structure

Every report is a structured Markdown document with these sections:

**Executive Summary** — macro backdrop (VIX regime, rate environment) and overall portfolio stance.

**Per-stock analysis** (one section per symbol):
- **Fundamental**: price, 52W range, market cap, PE/forward PE/PEG vs sector, analyst target + upside%, revenue growth, gross margins, beta, short float
- **Technical**: RSI interpretation (with weekly RSI for confirmation), MACD signal, Bollinger position, MA50/MA200 status and cross detection, ADX trend strength, OBV divergence, ATR daily risk%, VaR 95%
- **Catalysts & Risk**: 3 bull thesis points with expected timing, 3 bear risks, insider activity signal, options PCR, news sentiment score and top headline
- **Portfolio Position**: (only if the buyer holds this stock) avg cost, shares, current P&L%, and whether the technical setup favors adding / holding / trimming
- **Recommendation**: explicit BUY / HOLD / SELL with target price, time horizon, and 1–5 star risk rating with rationale

**Portfolio Summary** — sector concentration, cross-stock correlation, aggregate risk stance.

### Why This Approach Works for Investment Analysis

1. **Multi-source corroboration**: A bullish RSI reading means more when insider buying is also elevated and news sentiment is positive. The agent can connect these dots; a single-source tool cannot.

2. **Cost-basis personalization**: Generic "AAPL looks cheap at 28x" commentary is useless if you bought at $195. The agent computes your actual unrealized P&L and frames the recommendation around your entry price.

3. **Macro context**: The same stock analysis reads differently when the Fed is at 5% vs 2%, or when VIX is at 30 vs 15. The FRED integration ensures the LLM writes against real rate and inflation data, not its training-time assumptions.

4. **Insider signal filtering**: Form 4 filings are one of the few legally disclosed leading indicators. The SEC EDGAR integration surfaces clustering (5+ filings = "high activity") that often precedes corporate events.

5. **Structured output enforcement**: The two-stage instruction and strict section headers force the LLM to commit to an explicit recommendation and target price — not hedge with "it depends."

---

## 2. Protocol Architecture

### Two Protocols, One Flow

| Protocol | Role | What it handles |
|----------|------|-----------------|
| **ERC-8183** | Commerce | On-chain job creation, escrow, payment settlement |
| **UOMP** | User context + delivery | Portfolio data from local Guard, deliverable relay via Cloudflare Tunnel |

### ERC-8183: Trustless Payment Settlement

The buyer's 1.0 U payment is locked in a smart contract escrow. The agent receives it only after submitting a verifiable deliverable URL on-chain and the 24-hour dispute window passes without a challenge.

```
Buyer                        Chain (BSC Testnet)              Seller Agent
  │                               │                               │
  ├── negotiate (A2A) ────────────────────────────────────────── │
  │◄─ signed quote (1.0 U) ───────────────────────────────────── │
  │                               │                               │
  ├── createJob ─────────────────►│                               │
  ├── registerJob ───────────────►│                               │
  ├── setBudget + fund ──────────►│ U token locked in escrow      │
  │                               │                               │
  ├── notify_funded (A2A) ────────────────────────────────────── │
  │◄─ ACK "accepted" (instant) ───────────────────────────────── │
  │                               │    background: LLM analysis   │
  │                               │◄── submit_result(report_url)  │
  │                               │                               │
  ├── poll getJob() ─────────────►│                               │
  │◄─ SUBMITTED ─────────────────│                               │
  │                               │                               │
  ├── fetch report via UOMP relay ────────────────────────────── │
  │◄─ Markdown report ────────────────────────────────────────── │
  │                               │                               │
  └── settle ────────────────────►│ escrow released to seller     │
```

### UOMP: Personalized Context + Reverse Delivery

UOMP solves two problems ERC-8183 alone cannot:

**User context**: The buyer's portfolio (symbols, shares, avg cost) and risk profile (tolerance, horizon, preferred indicators) are stored in their own Memory Guard on localhost. Before negotiating, the buyer client reads this data and builds a personalized task description. For a named `notify_funded` call, it serializes the delivery and portfolio context once and signs that exact string; the seller receives only the validated structured context from the EIP-712 authorization envelope.

**Reverse delivery**: The seller runs in the cloud with no inbound URL the buyer can poll. The buyer starts a local relay on `:9444`, exposes it via Cloudflare Tunnel (`https://xxx.trycloudflare.com`), and includes the tunnel URL and token in the signed `notify_funded` context. The seller uploads the report there; the buyer fetches it from its own relay.

---

## 3. System Architecture

```
┌─────────────────── LOCAL (buyer machine) ───────────────────────────────┐
│                                                                           │
│  UOMP Guard mock (localhost:9374)                                         │
│   portfolio:holdings  — AAPL ×50 @ $185, NVDA ×20 @ $420                 │
│   profile:risk        — moderate / 12mo horizon                           │
│          ──[1. read]──►  buyer-client (Node.js)                           │
│                                 │                                         │
│                ┌────────────────┼────────────────────┐                   │
│                │                │                    │                   │
│           [2. negotiate]  [3. on-chain ops]    [relay :9444]             │
│           OAuth2 token    createJob             Cloudflare Tunnel ────────┼──┐
│                           registerJob                                     │  │
│                           setBudget + fund ─────────────────────────────┼──┼──►BSC Testnet
│                                                                           │  │
└───────────────────────────────────────────────────────────────────────────┘  │
                         │ [4. notify_funded]                                   │
                         │   EIP-712 signed gateway + portfolio context         │
                         ▼                                                       │
┌──────────────────────────────────────────────────────────────────────────┐   │
│              BNB Chain Platform (cloud seller)                            │   │
│                                                                           │   │
│  Stage 1: collect data (6 tool categories, 5 sources)                    │   │
│    yfinance · FRED · SEC EDGAR · Alpha Vantage · GNews                   │   │
│                                                                           │   │
│  Stage 2: write structured report                                         │   │
│    fundamentals · 10 technical indicators · insider · sentiment           │   │
│    portfolio P&L · bull/bear thesis · recommendation + target             │   │
│                                                                           │   │
│  submit_result ───────────────────────────────────────────────────────────┼───┼──►BSC Testnet
│  POST report ──────────────────────────────────────────────────────────── ┼───┘
│    → Cloudflare Tunnel → buyer relay → displayed inline                   │
└───────────────────────────────────────────────────────────────────────────┘
```

### BSC Testnet Contract Addresses

| Contract | Address |
|----------|---------|
| AgenticCommerce | `0xa206c0517b6371c6638cd9e4a42cc9f02a33b0de` |
| EvaluatorRouter | `0xd7d36d66d2f1b608a0f943f722d27e3744f66f25` |
| OptimisticPolicy | `0x4f4678d4439fec812ac7674bb3efb4c8f5fb78a6` |
| U Token (ERC-20) | `0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565` |

> **MegaFuel Paymaster**: The BSC testnet public paymaster accepts but never confirms transactions. The buyer client disables it; gas is paid directly (~3 gwei).

---

## 4. E2E Testing

### Public x402 gateway endpoint

For the deployed testnet x402 gateway, buyers configure the API Gateway base
URL rather than the raw AgentCore invocation URL:

```dotenv
X402_ENDPOINT=https://<api-id>.execute-api.us-east-1.amazonaws.com/testnet
X402_SELLER_WALLET=0xd10BdDC20E4DC42A1a19a9653e994991e25b8153
```

Only four asynchronous paid routes are public through that gateway:
`GET /x402/price`, `POST /x402/analyze/async`,
`GET /x402/jobs/{jobId}`, and `POST /x402/jobs/{jobId}/resume`. The gateway
uses its Lambda `live` alias and trusted API Gateway request context; it does
not expose the AgentCore invocation URL. See
[`docs/x402-lambda-gateway.md`](../docs/x402-lambda-gateway.md) for operation
and rollback procedures.

### Prerequisites

- Node.js 18+, [cloudflared](https://github.com/cloudflare/cloudflared) installed
- Buyer wallet: ≥ 0.01 tBNB (gas) + ≥ 1.0 U (escrow)
- Seller deployed to BNB Chain platform (see below)

### Deploy the seller

```bash
cd stockanalyst/app/agent

# Single-quote the password — prevents bash from expanding $x inside it
export WALLET_PASSWORD='<your-keystore-password>'

bag deploy agent --force-deploy-broken-storage
```

After deploy, `studio.toml` records `[deploy.platform]` with `agent_id` and `invoke_url`. Create an OAuth2 client from the platform console to get `client_id` and `client_secret`.

### Configure private storage for asynchronous x402 jobs

The asynchronous endpoint stores job state and personalized reports in S3.
At the job's `expiresAt` timestamp, seven days after creation, authenticated
polling stops and the API no longer issues or renews download URLs. A presigned
URL minted before `expiresAt` remains independently usable until its own
expiration, for at most 30 minutes. S3 Lifecycle separately marks each object
eligible for expiration seven days after that object's `LastModified`
timestamp; actual physical deletion is asynchronous and scheduled by AWS. This
supported runbook requires a dedicated private x402 job bucket that has never
had S3 Versioning enabled.

Set operator-local variables without committing their values:

```bash
export AWS_REGION='us-east-1'
export X402_JOB_S3_BUCKET='replace-with-private-bucket-name'
export AGENTCORE_RUNTIME_ROLE_NAME='replace-with-runtime-role-name'
export X402_JOB_TOKEN_SECRET="$(openssl rand -hex 32)"
```

The last command places only the generator expression in shell history. Its
result is 64 hexadecimal characters representing 32 random bytes of entropy.
Do not echo the value or run these commands with shell tracing enabled.

Block every form of public access:

```bash
aws s3api put-public-access-block \
  --bucket "$X402_JOB_S3_BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

Verify that Versioning has never been enabled:

```bash
aws s3api get-bucket-versioning --bucket "$X402_JOB_S3_BUCKET"
```

Continue only when the response has no `Status` field (normally `{}`). Both
`Enabled` and `Suspended` are rejected: suspension does not remove existing
versions. With the rule below, a versioned current object may first become
noncurrent after seven days and then remain for roughly another seven days;
delete markers can also remain. That does not meet this runbook's retention
intent.

Create the Lifecycle file through `mktemp`, which places it outside the
repository, with exactly:

```bash
export X402_JOB_LIFECYCLE_FILE="$(mktemp "${TMPDIR:-/tmp}/x402-job-lifecycle.json.XXXXXX")"
cat > "$X402_JOB_LIFECYCLE_FILE" <<'JSON'
{
  "Rules": [{
    "ID": "ExpireX402JobsAfterSevenDays",
    "Status": "Enabled",
    "Filter": { "Prefix": "x402-jobs/" },
    "Expiration": { "Days": 7 },
    "NoncurrentVersionExpiration": { "NoncurrentDays": 7 }
  }]
}
JSON
```

`put-bucket-lifecycle-configuration` replaces the bucket's complete Lifecycle
ruleset. For the required dedicated bucket, first confirm that no unrelated
rules exist, then apply and verify the seven-day eligibility rule:

```bash
aws s3api get-bucket-lifecycle-configuration \
  --bucket "$X402_JOB_S3_BUCKET"

aws s3api put-bucket-lifecycle-configuration \
  --bucket "$X402_JOB_S3_BUCKET" \
  --lifecycle-configuration "file://$X402_JOB_LIFECYCLE_FILE"

aws s3api get-public-access-block --bucket "$X402_JOB_S3_BUCKET"
aws s3api get-bucket-lifecycle-configuration --bucket "$X402_JOB_S3_BUCKET"
```

`NoSuchLifecycleConfiguration` from the first command is the expected result
for a new dedicated bucket.

Whether the apply/verification commands succeed or fail, remove the temporary
file and variable:

```bash
rm -f -- "$X402_JOB_LIFECYCLE_FILE"
unset X402_JOB_LIFECYCLE_FILE
```

Bucket reuse is unsupported by these commands. Any exception requires a
separately reviewed custom bucket policy, CloudFront exposure analysis,
Versioning-aware Lifecycle design, and non-destructive ruleset update.

Grant the AgentCore runtime role only object read/write access to this prefix.
Create its policy file outside the repository:

```bash
export X402_JOB_IAM_POLICY_FILE="$(mktemp "${TMPDIR:-/tmp}/x402-job-iam-policy.json.XXXXXX")"
cat > "$X402_JOB_IAM_POLICY_FILE" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "X402PrivateJobs",
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:PutObject"],
    "Resource": "arn:aws:s3:::${X402_JOB_S3_BUCKET}/x402-jobs/*"
  }]
}
JSON
```

Attach and inspect the scoped inline policy:

```bash
aws iam put-role-policy \
  --role-name "$AGENTCORE_RUNTIME_ROLE_NAME" \
  --policy-name X402PrivateJobs \
  --policy-document "file://$X402_JOB_IAM_POLICY_FILE"

aws iam get-role-policy \
  --role-name "$AGENTCORE_RUNTIME_ROLE_NAME" \
  --policy-name X402PrivateJobs
```

Whether the IAM commands succeed or fail, remove the temporary file and
variable:

```bash
rm -f -- "$X402_JOB_IAM_POLICY_FILE"
unset X402_JOB_IAM_POLICY_FILE
```

Do not grant `s3:*`, bucket-wide object access, public-read ACLs, or wallet
permissions. Install the runtime settings through `bag`; do not put the token
secret in source, `studio.toml`, logs, or shell history:

```bash
cd stockanalyst/app/agent
bag env set X402_JOB_S3_BUCKET "$X402_JOB_S3_BUCKET"
bag env set X402_JOB_S3_PREFIX x402-jobs
bag env set X402_JOB_TOKEN_SECRET "$X402_JOB_TOKEN_SECRET"
bag env set X402_ASYNC_ACCEPT_NEW_JOBS 1
bag deploy agent --force-deploy-broken-storage
bag deploy info
```

This repository's existing deployment procedure uses the explicit `agent`
target and `--force-deploy-broken-storage` storage workaround; use that
repository-verified form for rollout and rollback.

After deployment, verify async create, authenticated polling, report download,
and resume. A successful job must return only a
private presigned URL valid for at most 30 minutes. Confirm that polling at
`expiresAt` returns expiry and cannot issue or renew a URL, while a URL minted
before `expiresAt` remains independently usable only until that URL's own
expiration. Confirm logs contain stable job IDs and error codes, but no payment
proof, job token, portfolio, S3 key, or presigned URL.

To pause or roll back creation without cutting off access to already-paid jobs:

```bash
cd stockanalyst/app/agent
bag env set X402_ASYNC_ACCEPT_NEW_JOBS 0
bag deploy agent --force-deploy-broken-storage
```

Keep the bucket, IAM prefix access, and `X402_JOB_TOKEN_SECRET` available
through the latest outstanding job's `expiresAt` plus 30 minutes. Polling and
URL issuance stop at `expiresAt`; the additional window preserves only
presigned URLs minted beforehand until their independent expiration. Do not
delete those resources as part of the creation rollback. Lifecycle deletion may
occur later because AWS expiration processing is asynchronous.

### Configure optional API keys

Add to `stockanalyst/.studio/.env.local` for richer analysis (agent works without them):

```env
FRED_API_KEY=...            # fred.stlouisfed.org — free, instant approval
ALPHA_VANTAGE_API_KEY=...   # alphavantage.co — free, 25 req/day
GNEWS_API_KEY=...           # gnews.io — free, 100 req/day
```

SEC EDGAR and yfinance (price, technicals, options) require no key and always run.

### Install buyer dependencies

```bash
brew install cloudflare/cloudflare/cloudflared   # macOS

cd buyer-client
npm install
cp .env.example .env
```

### Configure `buyer-client/.env`

```env
KEYSTORE_PATH=../stockanalyst/.studio/wallets/<address>.json
WALLET_PASSWORD=<your-keystore-password>
AGENT_ENDPOINT=https://bnbagent-api.bnbchain.world/v1/rt/<agent_id>/a2a
AGENT_CLIENT_ID=<client_id from platform console>
AGENT_CLIENT_SECRET=<client_secret from platform console>
PROVIDER_ADDRESS=<seller wallet address>
UOMP_GUARD_URL=http://127.0.0.1:9374
UOMP_GUARD_TOKEN=demo-guard-token
```

### Run

**Terminal 1 — UOMP Guard mock** (serves portfolio + risk profile):

```bash
cd agent-demo   # repo root
node guard-mock.mjs
```

**Terminal 2 — buyer client:**

```bash
cd buyer-client
npm run dev
```

### E2E Steps

| Step | Action | Detail |
|------|--------|--------|
| 1 | Load UOMP context | Guard → AAPL ×50 @ $185, NVDA ×20 @ $420, risk=moderate |
| 2 | `negotiate` | OAuth2 token → A2A → signed quote 1.0 U |
| 3 | On-chain buy | createJob → registerJob → setBudget → approve → fund |
| 4 | `notify_funded` | Job-client EIP-712 authorization over exact gateway + portfolio context → seller ACK |
| 5 | Seller works | Stage 1: data collection → Stage 2: kimi-k2.6 extended thinking + report (~5–15 min) |
| 6 | Fetch report | Tunnel URL → local relay → report displayed inline |
| 7 | Settle | After 24h dispute window: `bag erc8183 settle <job_id>` |

### Authenticated `notify_funded` protocol

The TypeScript buyer client uses `notifyFunded(AGENT_ENDPOINT, wallet, jobId,
options)` rather than a raw named-job payload. It serializes the complete
context (delivery gateway URL/token, portfolio, and risk profile) once and
signs that exact string as EIP-712 typed data. The seller supplies the chain
and Commerce contract domain values, recovers the signature, and requires the
signer to be the on-chain job client. The authorization envelope is the only
source of named-job delivery and personalization fields.

Production gateways must be public HTTPS `*.trycloudflare.com` origins by
default. To test an HTTP loopback relay locally, explicitly set
`ALLOW_PRIVATE_DELIVERY_GATEWAY=true` on the seller. For another approved
public delivery origin, configure `DELIVERY_GATEWAY_ALLOWED_HOSTS` as a
comma-separated exact hostname or `.suffix` allowlist; do not broaden it beyond
the relay origins you operate.

---

## Pricing

| Stocks | Price |
|--------|-------|
| Any count | **1.0 U** (testnet) |
| Floor / Ceiling | 0.5 U – 5.0 U |

Currency: U token on BSC testnet (`0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565`)

---

## BSC Testnet Resources

| Resource | Link |
|----------|------|
| tBNB faucet (gas) | https://testnet.bnbchain.org/faucet-smart |
| U token faucet (escrow) | https://united-coin-u.github.io/u-faucet/ |
| BSC testnet explorer | https://testnet.bscscan.com |

---

## CI

[CI Workflow](.github/workflows/ci.yml) runs on every push:
- `ruff check` — zero tolerance
- yfinance real data test (AAPL RSI / MACD / Bollinger / MA / ADX)
- LLM tool registration check (15 tools)

---

<a name="中文"></a>

# 中文

## 为什么要用区块链结算的股票分析 Agent？

传统金融研报有一个结构性问题：分析师不管报告有没有用，都能收钱。这个 Agent 反转了这个模型——付款锁在智能合约里，Agent 把可验证的交付物 URL 提交到链上之后，买家才能在 24 小时争议窗口之后释放资金。质量激励直接内嵌在结算机制里。

除了支付，这个 Agent 还是个性化的：它从买家本地控制的 UOMP Memory Guard 读取真实持仓成本和风险偏好，计算真实浮盈浮亏，而不是泛泛的市场评论。

---

## 1. 分析引擎

### 数据源（5 个独立数据流）

Agent 在写任何分析之前，会先聚合来自五个数据源的数据。每个数据源覆盖其他数据源无法替代的维度：

| 数据源 | 提供内容 | 是否需要 Key |
|--------|---------|------------|
| **yfinance** | 实时价格、基本面（PE/PB/PEG/Beta）、1年日线 OHLCV、期权链 | 无需 |
| **FRED** | 联储基准利率、10年国债收益率、CPI 同比、失业率 | `FRED_API_KEY` |
| **SEC EDGAR** | Form 4 内幕交易（90天内） | 无需（公开数据） |
| **Alpha Vantage** | AI 新闻情绪评分（–1 看空 → +1 看多） | `ALPHA_VANTAGE_API_KEY` |
| **GNews** | 按公司名搜索的最新 5 条头条 | `GNEWS_API_KEY` |

如果某个 Key 未配置，对应数据块跳过，Agent 继续用现有数据完成报告。SEC EDGAR 和 yfinance 始终可用。

### 技术指标

全部基于 1 年日线数据计算：

| 指标 | 信号含义 |
|------|---------|
| **RSI-14** | 超买（>70）/ 超卖（<30） |
| **周线 RSI** | 更长周期动量，过滤日内噪音 |
| **MACD** | 交叉方向 + 柱状图动量 |
| **布林带** | 价格在通道中的位置（0=下轨，1=上轨） |
| **MA50 / MA200** | 价格与均线关系 + 金叉/死叉检测 |
| **ADX** | 趋势强度（>25=强趋势）+ 方向判断（+DI vs –DI） |
| **OBV** | 成交量是否确认价格方向（背离 = 警告信号） |
| **ATR-14** | 每日波动率（占价格%）—— 仓位管理依据 |
| **VaR 95%** | 95% 置信度下的单日最大亏损（历史法） |
| **CVaR 95%** | 最差 5% 交易日的平均亏损（期望损失） |

期权情绪（无需 Key）：
- **Put/Call 比率**（成交量和持仓量）—— PCR > 1.2 = 看空对冲为主；< 0.7 = 看多
- **隐含波动率** 按持仓量加权平均（最近到期日）

### 两阶段分析逻辑

Agent 采用严格的两阶段流程，防止幻觉：

```
第一阶段：数据采集（调用工具，不输出文字）
  每个股票：
    get_stock_quote()        → 价格、PE、PB、PEG、Beta、分析师目标价
    get_technical_signals()  → 以上 10 个技术指标
    get_options_sentiment()  → Put/Call 比率、隐含波动率
    get_insider_activity()   → SEC Form 4 申报数量（90天）
    get_news_sentiment()     → Alpha Vantage 情绪分 + GNews 头条
  执行一次：
    get_macro_context()      → FRED 利率数据 + VIX

第二阶段：报告撰写（结构化 Markdown，不再发起新的数据请求）
  仅使用第一阶段工具返回的数字
  不虚构任何价格、RSI 值或情绪分
```

这个分离由 Agent 的 instruction 强制执行：LLM 被明确要求先收集所有数据，再开始撰写。报告中每个数字都对应一次具体的工具调用结果。

### 扩展思维模式

Agent 使用 **Kimi K2.6**（`kimi-k2.6`，256k 上下文窗口）通过 `api.moonshot.cn` 提供分析。该模型支持扩展思维：在撰写报告之前，LLM 会对分析进行深度推理，从而显著提升建议质量和一致性。

- 思维过程内容会被**过滤掉**——只有最终格式化的报告会发给买家（通过 ADK `Part.thought` 过滤器实现）
- 多股票组合分析通常需要 **5–15 分钟**，具体取决于股票数量
- 交付超时设置为 **1800 秒（30 分钟）**，以适应思维模式下的深度分析
- 256k 上下文窗口可轻松容纳 10 个以上股票的工具调用结果和完整报告

### 报告结构

每份报告都是包含以下章节的结构化 Markdown：

**执行摘要** —— 宏观背景（VIX 状态、利率环境）和整体持仓判断。

**每只股票分析**（每个股票代码一节）：
- **基本面**：价格、52周区间、市值、PE/远期PE/PEG、分析师目标价及上涨空间、营收增长、毛利率、Beta、做空比例
- **技术面**：RSI 解读（结合周线 RSI 确认）、MACD 信号、布林带位置、MA50/MA200 状态及金叉/死叉检测、ADX 趋势强度、OBV 背离、ATR 日波动风险%、VaR 95%
- **催化剂与风险**：3个看多逻辑（含预期时间节点）、3个看空风险、内幕交易信号、期权 PCR、新闻情绪评分及头条
- **持仓分析**（仅当买家持有该股票时）：平均成本、持股数量、当前浮盈浮亏%、技术面是否支持加仓/持有/减仓
- **投资建议**：明确的 买入/持有/卖出，含目标价、时间周期、1–5星风险评级及理由

**持仓汇总** —— 板块集中度、跨股票相关性、整体风险态势。

### 为什么这种方式适合投资分析

1. **多源交叉验证**：RSI 看多信号，叠加内幕人士在买入、新闻情绪正面，说服力远大于单一信号。Agent 能把这些维度关联起来。

2. **成本个性化**：泛泛的"苹果 28 倍 PE 便宜"毫无意义，如果你是 195 美元进的。Agent 用你的真实成本计算实际浮盈浮亏，在此基础上给出建议。

3. **宏观背景**：联储利率 5% 和 2% 的环境下，同一只股票的分析结论截然不同；VIX 30 和 VIX 15 的市场情绪也完全不同。FRED 集成确保 LLM 基于真实利率和通胀数据写作，而不是依赖训练时的历史假设。

4. **内幕交易信号过滤**：Form 4 是极少数合法披露的领先指标之一。SEC EDGAR 集成会识别集中申报（90天内 5+ 次 = "高活跃度"），这通常早于公司重大事件。

5. **强制结构化输出**：两阶段 instruction 和严格的章节标题迫使 LLM 给出明确的建议和目标价——不允许"具体情况具体分析"的模糊表述。

---

## 2. 协议架构

### 两个协议，一个流程

| 协议 | 角色 | 负责内容 |
|------|------|---------|
| **ERC-8183** | 商业协议 | 链上 Job 创建、资金托管、付款结算 |
| **UOMP** | 用户上下文 + 交付 | 本地 Guard 持仓数据、Cloudflare Tunnel 反向报告传递 |

### ERC-8183：无需信任的付款结算

买家的 1.0 U 付款锁在智能合约托管中。Agent 只有在将可验证的交付物 URL 提交到链上、且 24 小时争议窗口期过去后，才能收款。

```
买家                         链上合约（BSC 测试网）              卖家 Agent
  │                               │                               │
  ├── negotiate（A2A）────────────────────────────────────────── │
  │◄─ 签名报价（1.0 U）──────────────────────────────────────── │
  │                               │                               │
  ├── createJob ─────────────────►│                               │
  ├── registerJob ───────────────►│                               │
  ├── setBudget + fund ──────────►│  U token 锁入托管合约         │
  │                               │                               │
  ├── notify_funded（A2A）────────────────────────────────────── │
  │◄─ ACK "accepted"（立即）──────────────────────────────────── │
  │                               │  后台：LLM 两阶段分析         │
  │                               │◄ submit_result(report_url)   │
  │                               │                               │
  ├── 轮询 getJob()──────────────►│                               │
  │◄─ SUBMITTED ─────────────────│                               │
  │                               │                               │
  ├── 通过 UOMP 中继获取报告 ──────────────────────────────────── │
  │◄─ Markdown 报告 ──────────────────────────────────────────── │
  │                               │                               │
  └── settle ────────────────────►│  托管款释放给卖家              │
```

### UOMP：个性化上下文 + 反向交付

**用户上下文**：买家的持仓（代码、股数、平均成本）和风险偏好（容忍度、时间跨度、偏好指标）存放在本地控制的 Memory Guard 中。协商前，买家客户端读取这些数据并构建个性化任务描述。针对带 job ID 的 `notify_funded` 调用，客户端会将交付和持仓上下文序列化一次，并对完全相同的字符串签名；卖家只从 EIP-712 授权信封中接收并验证结构化上下文。

**反向交付**：卖家在云端运行，没有买家可以主动拉取报告的入站 URL。买家在本地启动 `:9444` 中继，通过 Cloudflare Tunnel 暴露（`https://xxx.trycloudflare.com`），并将 Tunnel URL 和 token 放入已签名的 `notify_funded` 上下文。卖家把报告 POST 到那里，买家从自己的本地中继读取。

---

## 3. 系统架构

```
┌─────────────────── 本地（买家机器）──────────────────────────────────────┐
│                                                                           │
│  UOMP Guard mock（localhost:9374）                                        │
│   portfolio:holdings — AAPL ×50 @ $185、NVDA ×20 @ $420                  │
│   profile:risk       — moderate / 12个月                                  │
│          ──[1. 读取]──►  buyer-client（Node.js）                          │
│                                 │                                         │
│                ┌────────────────┼────────────────────┐                   │
│                │                │                    │                   │
│           [2. 协商]      [3. 链上操作]          [中继 :9444]             │
│           OAuth2 Token   createJob               Cloudflare Tunnel ───────┼──┐
│                          registerJob                                      │  │
│                          setBudget + fund ───────────────────────────────┼──┼──►BSC 测试网
│                                                                           │  │
└───────────────────────────────────────────────────────────────────────────┘  │
                         │ [4. notify_funded]                                   │
                         │   EIP-712 签名的网关 + 持仓上下文                     │
                         ▼                                                       │
┌──────────────────────────────────────────────────────────────────────────┐   │
│              BNB Chain 平台（云端卖家）                                    │   │
│                                                                           │   │
│  第一阶段：数据采集（6类工具，5个数据源）                                   │   │
│    yfinance · FRED · SEC EDGAR · Alpha Vantage · GNews                   │   │
│                                                                           │   │
│  第二阶段：结构化报告撰写                                                   │   │
│    基本面 · 10个技术指标 · 内幕交易 · 情绪分 · 持仓浮盈浮亏 · 建议+目标价   │   │
│                                                                           │   │
│  submit_result ────────────────────────────────────────────────────────── ┼───┼──►BSC 测试网
│  POST 报告 ─────────────────────────────────────────────────────────────── ┼───┘
│    → Cloudflare Tunnel → 买家本地中继 → 内联显示                           │
└───────────────────────────────────────────────────────────────────────────┘
```

### BSC 测试网合约地址

| 合约 | 地址 |
|------|------|
| AgenticCommerce | `0xa206c0517b6371c6638cd9e4a42cc9f02a33b0de` |
| EvaluatorRouter | `0xd7d36d66d2f1b608a0f943f722d27e3744f66f25` |
| OptimisticPolicy | `0x4f4678d4439fec812ac7674bb3efb4c8f5fb78a6` |
| U Token（ERC-20） | `0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565` |

> **MegaFuel Paymaster**：BSC 测试网公共 paymaster 接受交易但不确认。买家客户端已禁用，直接支付 Gas（约 3 gwei）。

---

## 4. 端到端测试

### 前置条件

- Node.js 18+，已安装 [cloudflared](https://github.com/cloudflare/cloudflared)
- 买家钱包：≥ 0.01 tBNB（Gas）+ ≥ 1.0 U（托管）
- 卖家已部署到 BNB Chain 平台（见下文）

### 部署卖家

```bash
cd stockanalyst/app/agent

# 必须用单引号 — 防止 bash 展开密码中的特殊字符（如 $r 会被静默截断）
export WALLET_PASSWORD='<你的 keystore 密码>'

bag deploy agent --force-deploy-broken-storage
```

部署完成后，`studio.toml` 记录 `[deploy.platform]`，包含 `agent_id` 和 `invoke_url`。从平台控制台创建 OAuth2 客户端，获取 `client_id` 和 `client_secret`。

### 为异步 x402 任务配置私有存储

异步接口把任务状态和个性化报告保存在 S3。任务创建 7 天到达 `expiresAt` 后，
鉴权轮询停止，API 不再签发或续签下载 URL。`expiresAt` 之前已经签发的 presigned
URL 仍可独立使用到其自身过期时间，最长 30 分钟。S3 Lifecycle 则按各对象的
`LastModified` 时间计算，满 7 天后仅将对象标记为可过期；实际物理删除由 AWS
异步调度。本操作手册只支持一个从未启用 S3 Versioning 的独立私有 x402 任务
bucket。

先在操作者本地设置变量，不要提交真实值：

```bash
export AWS_REGION='us-east-1'
export X402_JOB_S3_BUCKET='replace-with-private-bucket-name'
export AGENTCORE_RUNTIME_ROLE_NAME='replace-with-runtime-role-name'
export X402_JOB_TOKEN_SECRET="$(openssl rand -hex 32)"
```

最后一条命令写入 shell 历史的只有生成表达式；结果是 64 个十六进制字符，包含
32 个随机字节的熵。不要输出该值，也不要在开启 shell tracing 时执行这些命令。

彻底阻止公开访问：

```bash
aws s3api put-public-access-block \
  --bucket "$X402_JOB_S3_BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

确认该 bucket 从未启用 Versioning：

```bash
aws s3api get-bucket-versioning --bucket "$X402_JOB_S3_BUCKET"
```

只有响应没有 `Status` 字段（通常为 `{}`）时才可继续。`Enabled` 和 `Suspended`
都不符合要求：暂停 Versioning 不会删除历史版本。使用下方规则时，启用版本控制
的当前对象可能在 7 天后才转成 noncurrent，再保留约 7 天，且 delete marker 也
可能继续存在，因此不符合本操作手册的保留策略目标。

通过 `mktemp` 在仓库外创建 Lifecycle 文件，内容必须为：

```bash
export X402_JOB_LIFECYCLE_FILE="$(mktemp "${TMPDIR:-/tmp}/x402-job-lifecycle.json.XXXXXX")"
cat > "$X402_JOB_LIFECYCLE_FILE" <<'JSON'
{
  "Rules": [{
    "ID": "ExpireX402JobsAfterSevenDays",
    "Status": "Enabled",
    "Filter": { "Prefix": "x402-jobs/" },
    "Expiration": { "Days": 7 },
    "NoncurrentVersionExpiration": { "NoncurrentDays": 7 }
  }]
}
JSON
```

`put-bucket-lifecycle-configuration` 会替换 bucket 的完整 Lifecycle 规则集。
对于要求使用的独立 bucket，先确认不存在无关规则，再应用并核验 7 天可过期
规则：

```bash
aws s3api get-bucket-lifecycle-configuration \
  --bucket "$X402_JOB_S3_BUCKET"

aws s3api put-bucket-lifecycle-configuration \
  --bucket "$X402_JOB_S3_BUCKET" \
  --lifecycle-configuration "file://$X402_JOB_LIFECYCLE_FILE"

aws s3api get-public-access-block --bucket "$X402_JOB_S3_BUCKET"
aws s3api get-bucket-lifecycle-configuration --bucket "$X402_JOB_S3_BUCKET"
```

对于新建的独立 bucket，第一条命令返回 `NoSuchLifecycleConfiguration` 是预期
结果。

无论应用和核验命令成功还是失败，都应删除临时文件和变量：

```bash
rm -f -- "$X402_JOB_LIFECYCLE_FILE"
unset X402_JOB_LIFECYCLE_FILE
```

这些命令不支持复用 bucket。任何例外都必须单独评审自定义 bucket policy、
CloudFront 暴露面、Versioning 感知的 Lifecycle 设计和不会覆盖现有规则的更新
流程。

AgentCore runtime role 只能获得该前缀下对象的读写权限。在仓库外创建策略
文件：

```bash
export X402_JOB_IAM_POLICY_FILE="$(mktemp "${TMPDIR:-/tmp}/x402-job-iam-policy.json.XXXXXX")"
cat > "$X402_JOB_IAM_POLICY_FILE" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "X402PrivateJobs",
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:PutObject"],
    "Resource": "arn:aws:s3:::${X402_JOB_S3_BUCKET}/x402-jobs/*"
  }]
}
JSON
```

绑定并检查最小权限策略：

```bash
aws iam put-role-policy \
  --role-name "$AGENTCORE_RUNTIME_ROLE_NAME" \
  --policy-name X402PrivateJobs \
  --policy-document "file://$X402_JOB_IAM_POLICY_FILE"

aws iam get-role-policy \
  --role-name "$AGENTCORE_RUNTIME_ROLE_NAME" \
  --policy-name X402PrivateJobs
```

无论 IAM 命令成功还是失败，都应删除临时文件和变量：

```bash
rm -f -- "$X402_JOB_IAM_POLICY_FILE"
unset X402_JOB_IAM_POLICY_FILE
```

禁止授予 `s3:*`、bucket 全范围对象访问、public-read ACL 或任何钱包权限。运行时
配置只能通过 `bag` 注入；不得把 token secret 写入源码、`studio.toml`、日志或
shell 历史：

```bash
cd stockanalyst/app/agent
bag env set X402_JOB_S3_BUCKET "$X402_JOB_S3_BUCKET"
bag env set X402_JOB_S3_PREFIX x402-jobs
bag env set X402_JOB_TOKEN_SECRET "$X402_JOB_TOKEN_SECRET"
bag env set X402_ASYNC_ACCEPT_NEW_JOBS 1
bag deploy agent --force-deploy-broken-storage
bag deploy info
```

本仓库现有部署流程使用显式 `agent` target 和
`--force-deploy-broken-storage` 存储兼容参数；上线和回滚都应使用这一经过仓库
验证的形式。

部署后验证异步创建、鉴权轮询、报告下载和恢复流程。成功任务
只能返回最长有效 30 分钟的私有 presigned URL。确认轮询在 `expiresAt` 时返回
过期且不能签发或续签 URL；`expiresAt` 前已经签发的 URL 只能独立使用到该 URL
自身的过期时间。检查日志只能出现稳定 job ID 和错误码，不得包含付款凭证、job
token、持仓、S3 key 或 presigned URL。

暂停或回滚新任务创建时，必须保留已付款任务的访问能力：

```bash
cd stockanalyst/app/agent
bag env set X402_ASYNC_ACCEPT_NEW_JOBS 0
bag deploy agent --force-deploy-broken-storage
```

至少保留 bucket、IAM 前缀权限和 `X402_JOB_TOKEN_SECRET` 到所有任务中最晚的
`expiresAt` 再加 30 分钟。轮询和 URL 签发在 `expiresAt` 停止；额外窗口只用于
保证此前签发的 presigned URL 可使用到其独立过期时间。创建入口回滚时不得删除
这些资源。由于 AWS 异步处理过期，Lifecycle 的物理删除可能更晚发生。

### 配置可选 API Key

在 `stockanalyst/.studio/.env.local` 中添加（不配置时 Agent 仍可运行，但分析维度减少）：

```env
FRED_API_KEY=...            # fred.stlouisfed.org — 免费，秒批
ALPHA_VANTAGE_API_KEY=...   # alphavantage.co — 免费，25 次/天
GNEWS_API_KEY=...           # gnews.io — 免费，100 次/天
```

### 安装买家依赖

```bash
brew install cloudflare/cloudflare/cloudflared   # macOS

cd buyer-client
npm install
cp .env.example .env
```

### 配置 `buyer-client/.env`

```env
KEYSTORE_PATH=../stockanalyst/.studio/wallets/<address>.json
WALLET_PASSWORD=<你的 keystore 密码>
AGENT_ENDPOINT=https://bnbagent-api.bnbchain.world/v1/rt/<agent_id>/a2a
AGENT_CLIENT_ID=<平台控制台的 client_id>
AGENT_CLIENT_SECRET=<平台控制台的 client_secret>
PROVIDER_ADDRESS=<卖家钱包地址>
UOMP_GUARD_URL=http://127.0.0.1:9374
UOMP_GUARD_TOKEN=demo-guard-token
```

### 运行

**终端 1 — UOMP Guard mock**（提供持仓和风险偏好数据）：

```bash
cd agent-demo   # 仓库根目录
node guard-mock.mjs
```

**终端 2 — 买家客户端：**

```bash
cd buyer-client
npm run dev
```

### 端到端步骤

| 步骤 | 操作 | 说明 |
|------|------|------|
| 1 | 加载 UOMP 上下文 | Guard → AAPL ×50 @ $185、NVDA ×20 @ $420，risk=moderate |
| 2 | `negotiate`（A2A） | OAuth2 Token → A2A → 签名报价 1.0 U |
| 3 | 链上买入 | createJob → registerJob → setBudget → approve → fund |
| 4 | `notify_funded` | job client 对精确网关 + 持仓上下文的 EIP-712 授权 → 卖家 ACK |
| 5 | 卖家分析 | 第一阶段：5源数据采集 → 第二阶段：结构化报告撰写 |
| 6 | 获取报告 | Tunnel URL → 本地中继 → 报告内联显示 |
| 7 | 结算 | 24h 争议窗口后：`bag erc8183 settle <job_id>` |

### 已认证的 `notify_funded` 协议

TypeScript 买家客户端通过 `notifyFunded(AGENT_ENDPOINT, wallet, jobId,
options)` 发起带 job ID 的通知，而不是手工构造原始请求。它会将完整上下文
（交付网关 URL/token、持仓和风险偏好）序列化一次，并对完全相同的字符串进行
EIP-712 签名。卖家使用自身配置的 chain 和 Commerce contract 域值恢复签名，且
仅在签名者是链上 job client 时接受。授权信封是带 job ID 的交付和个性化字段的唯一来源。

生产环境默认只接受公开 HTTPS `*.trycloudflare.com` 网关。若仅用于本地 HTTP
loopback 中继测试，请在卖家端显式设置 `ALLOW_PRIVATE_DELIVERY_GATEWAY=true`。
如果使用其他已批准的公开交付域名，请将
`DELIVERY_GATEWAY_ALLOWED_HOSTS` 配置为逗号分隔的精确主机名或 `.suffix`
允许列表，并保持范围尽可能窄。

---

## 定价

| 分析数量 | 价格 |
|----------|------|
| 任意股票数 | **1.0 U**（测试网） |
| 价格区间 | 0.5 U – 5.0 U |

货币：BSC 测试网 U token（`0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565`）

---

## 测试网资源

| 资源 | 链接 |
|------|------|
| tBNB 水龙头（Gas） | https://testnet.bnbchain.org/faucet-smart |
| U token 水龙头（托管） | https://united-coin-u.github.io/u-faucet/ |
| BSC 测试网浏览器 | https://testnet.bscscan.com |

---

## CI 自动测试

[CI Workflow](.github/workflows/ci.yml) 在每次 push 时运行：
- `ruff check` — 零容忍
- yfinance 真实数据测试（AAPL RSI / MACD / 布林带 / MA / ADX）
- LLM 工具注册验证（15 个工具）
