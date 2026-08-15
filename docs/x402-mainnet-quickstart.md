# Stock Analyst x402 Mainnet 快速接入

本文面向需要调用 Stock Analyst 付费异步分析 API 的外部开发者。推荐直接使用仓库内的 TypeScript Buyer CLI；如果需要自行实现客户端，请参考文末的原始 HTTP 流程。

## 1. 服务信息

- Base URL：`https://stock-agent.bnbchain.org`
- 网络：BNB Smart Chain Mainnet，chain ID `56`
- 接口价格：每次成功受理的新任务收取所选 Token 的 `0.1`
- 原子金额：`100000000000000000`（四种 Token 均为 18 decimals）
- 钱包限流：每个付款钱包最多接受 `30` 个新任务/滚动 1 小时
- 支付协议：x402 V2
- 支付请求头：`Payment-Signature`

主要接口：

| Method | Path | 用途 |
| --- | --- | --- |
| `GET` | `/x402/price` | 查询实时价格和当前可用支付方式 |
| `POST` | `/x402/analyze/async` | 获取 402 challenge 或提交付款证明创建任务 |
| `GET` | `/x402/jobs/{jobId}` | 使用私有 Job Token 查询任务 |
| `POST` | `/x402/jobs/{jobId}/resume` | 恢复允许重试的分析任务 |

没有免费、Promo 或免签调用模式。不要使用旧的 `Wallet-Signature` 或 `X-Payment` 请求头。

## 2. 支持的 Token

| Token | BSC Mainnet 合约 | 签名方式 | Capability metadata |
| --- | --- | --- | --- |
| U | `0xcE24439F2D9C6a2289F741120FE202248B666666` | EIP-3009 | `United Stables / 1` |
| USD1 | `0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d` | EIP-3009 | `World Liberty Financial USD / 1` |
| USDC | `0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d` | Permit2 Exact | `USD Coin / 1` |
| USDT | `0x55d398326f99059fF775485246999027B3197955` | Permit2 Exact | `Tether USD / 1` |

先查询实时支付能力：

```bash
curl --fail-with-body --silent --show-error \
  https://stock-agent.bnbchain.org/x402/price | jq
```

响应中的两个字段含义不同：

- `supportedAssets` 是服务端注册的四种 Token 元数据。
- `accepts` 是当前服务端与 Binance B402 实时能力的交集，只有这里出现的 Token 才能用于本次支付。

客户端只需支持并选择 `accepts` 中的一个 Token，不要求四种 Token 同时可用，也不能自行构造一个当前未被返回的支付要求。

## 3. 使用官方 Buyer CLI

### 3.1 前置条件

- Node.js 24
- 一个 ethers-compatible 加密 keystore JSON 文件及其密码
- 钱包持有所选 Token，余额不少于 `0.1`
- 使用 USDC 或 USDT 时，需要 BSC Mainnet RPC、少量 BNB gas，以及预先设置的 Permit2 allowance

克隆仓库后安装依赖：

```bash
cd buyer-client
npm ci
cp .env.example .env
```

在 `buyer-client/.env` 中配置：

```dotenv
KEYSTORE_PATH=../stockanalyst/.studio/wallets/<buyer-wallet>.json
WALLET_PASSWORD=<keystore-password>

X402_ENDPOINT=https://stock-agent.bnbchain.org
X402_SELLER_WALLET=0x15958aad30b758dAbfbB9788Da69dfcd56e89078
X402_PAYMENT_TOKEN=U
X402_POLL_TIMEOUT_MS=1800000

UOMP_GUARD_URL=http://127.0.0.1:9374
UOMP_GUARD_TOKEN=demo-guard-token

# 仅 USDC/USDT 的 allowance 查询、approve、revoke 和付款前检查需要：
BSC_RPC_URL=https://<your-bsc-mainnet-rpc>
```

安全要求：

- `.env`、keystore 和密码不得提交到 Git。
- `X402_SELLER_WALLET` 必须与 `/x402/price` 返回的 `payTo` 完全一致。
- 不要在日志、工单或聊天中发送私钥、keystore、密码、付款证明或 Job Token。

### 3.2 启动本地示例上下文

Buyer CLI 会从 UOMP Guard 读取待分析的股票和风险偏好。打开第一个终端，在仓库根目录运行：

```bash
node guard-mock.mjs
```

示例 Guard 默认提供演示用投资组合；生产集成应替换成调用方自己的 UOMP Memory Guard。

### 3.3 选择 U 或 USD1

U 和 USD1 使用 EIP-3009，不需要 `BSC_RPC_URL` 或 ERC-20 approval。

```dotenv
X402_PAYMENT_TOKEN=U
```

或：

```dotenv
X402_PAYMENT_TOKEN=USD1
```

在第二个终端运行：

```bash
cd buyer-client
npm run x402:async
```

### 3.4 选择 USDC 或 USDT

USDC 和 USDT 使用 `permit2-exact`。ERC-20 approval 的目标必须是 canonical Permit2：

```text
0x000000000022D473030F116dDEE9F6B43aC78BA3
```

先在 `.env` 中设置 Token 和 BSC Mainnet RPC，然后查询 allowance：

```bash
cd buyer-client
npm run x402:allowance -- USDC
```

如果 allowance 不足，显式执行 approve：

```bash
npm run x402:approve -- USDC
```

该命令需要人工确认，并将 allowance 设置为最多 `50` USDC。USDT 的命令相同，只需将 `USDC` 改为 `USDT`。Buyer 的 `npm run x402:async` 不会自动 approve 或 revoke。

确认 allowance 后运行：

```bash
npm run x402:async
```

不再使用时可以显式撤销：

```bash
npm run x402:revoke -- USDC
```

## 4. Buyer CLI 的执行流程

CLI 自动完成以下流程：

1. 从 UOMP Guard 读取投资组合并构造分析请求。
2. 请求 `/x402/analyze/async`，收到 HTTP 402 和实时 `PAYMENT-REQUIRED`。
3. 严格校验网络、Token、金额、`payTo`、超时时间及 B402 metadata。
4. 对 U/USD1 生成 EIP-3009 授权，或对 USDC/USDT 生成 Permit2 Exact 授权。
5. 在首次携带付款证明的 POST 前，将完整待提交请求安全保存到本地。
6. 使用 `Payment-Signature` 重放完全相同的请求。
7. 收到 HTTP 202 后保存 `jobId`、私有 `jobToken` 和状态路径。
8. 轮询任务，下载 Markdown 报告，并在本地生成 HTML/PDF。

本地恢复状态位于 `buyer-client/.agent-data/`：

- `x402-pending-create.json`：已签名但尚未确认创建结果的请求
- `x402-job-receipt.json`：任务 ID、私有 Job Token、状态路径和过期时间
- `x402-async.lock`：防止同一目录并发运行两个 Buyer

这些文件使用 owner-only 权限。网络中断或进程退出后，直接重新运行 `npm run x402:async`；客户端会优先恢复原任务或重放同一份付款证明，不能通过删除 pending 文件来绕过恢复流程。

## 5. 原始 HTTP 流程

建议优先复用官方 Buyer 的签名和恢复实现。自行实现客户端时，流程如下。

### 5.1 获取付款要求

```bash
curl --include --request POST \
  https://stock-agent.bnbchain.org/x402/analyze/async \
  --header 'Content-Type: application/json' \
  --data '{"symbols":["AAPL","NVDA"],"analysis_type":"comprehensive"}'
```

服务返回 HTTP 402。支付要求同时出现在 JSON body 的 `paymentRequired` 和 `PAYMENT-REQUIRED` 响应头中。客户端必须验证两者一致，并从 `accepts` 中选择一个完整 requirement。

### 5.2 签名并重放

构造 x402 V2 proof，保留服务端返回的完整 `resource`、`accepted` 和 `accepted.extra`。随后将 proof JSON 做标准 Base64 编码，放入唯一的支付请求头：

```text
Payment-Signature: <base64-encoded-x402-v2-proof>
```

使用完全相同的请求 body 再次 POST：

```bash
curl --include --request POST \
  https://stock-agent.bnbchain.org/x402/analyze/async \
  --header 'Content-Type: application/json' \
  --header 'Payment-Signature: <base64-proof>' \
  --data '{"symbols":["AAPL","NVDA"],"analysis_type":"comprehensive"}'
```

成功时返回 HTTP 202，例如：

```json
{
  "jobId": "x402_<32 lowercase hex characters>",
  "jobToken": "<private token>",
  "status": "queued",
  "statusUrl": "/x402/jobs/x402_<32 lowercase hex characters>",
  "expiresAt": 1800000000000
}
```

`PAYMENT-RESPONSE` 响应头携带结算结果。不要记录或公开 `Payment-Signature`、`PAYMENT-RESPONSE`、`jobToken` 或最终的私有下载 URL。

### 5.3 查询任务

```bash
curl --fail-with-body --silent --show-error \
  https://stock-agent.bnbchain.org/x402/jobs/<jobId> \
  --header 'X-Job-Token: <jobToken>' | jq
```

常见状态为 `settling`、`queued`、`running`、`succeeded` 和 `failed`。成功响应包含短期有效的 `downloadUrl`；它属于私有 URL，不应转发或持久公开。

### 5.4 恢复可重试任务

仅当任务返回 `retryable: true` 时调用 resume：

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  https://stock-agent.bnbchain.org/x402/jobs/<jobId>/resume \
  --header 'X-Job-Token: <jobToken>' | jq
```

Resume 不需要，也不应再次发送 `Payment-Signature`；它不会再次执行支付验证或结算。已经永久失败、过期或不可恢复的任务可能返回 409 或 410。

完整 EIP-3009 和 Permit2 typed-data/wire 定义见 [x402 API 技术规范](./x402-api-usage.md)。

## 6. 常见错误

| HTTP | `errorCode` | 含义与处理 |
| --- | --- | --- |
| 400 | `invalid_request` | JSON 或请求字段不合法；修正请求后重新开始 |
| 402 | `payment_rejected` | Proof、金额、Token、签名、有效期或 capability 不匹配；重新获取 challenge，不要修改服务端 requirement |
| 429 | `wallet_rate_limited` | 付款钱包达到 30 次/滚动小时；遵循 `Retry-After`，不要立即重新签名 |
| 503 | `wallet_rate_limit_unavailable` | 钱包限流存储暂时不可用；保持本地恢复文件并稍后重试 |
| 503 | `payment_backend_unavailable` | B402 capability 服务暂时不可用；稍后重新获取 challenge |
| 503 | `settlement_pending` | 结算结果尚未确定；必须重放原 proof，不能签一个新 proof |
| 404 | `job_not_found` | Job ID 或 Job Token 无效；服务端不会区分两者 |
| 409 | `job_conflict` / `attempts_exhausted` | 当前状态不能恢复，或重试次数耗尽 |
| 410 | `job_expired` | 私有任务已过期 |

如果配置了 `X402_PAYMENT_TOKEN=USDC`，但实时 `accepts` 只有 U、USD1 和 USDT，客户端应返回 `payment_token_unavailable` 并列出实际可用 Token；不能伪造 USDC requirement。

## 7. 接入检查清单

- Base URL 不包含 `/mainnet`，也不以 `/x402` 结尾。
- 每次支付前都使用最新 402 challenge，不缓存或手写 B402 metadata。
- 金额严格为所选 Token 的 `100000000000000000` 原子单位。
- U/USD1 使用 EIP-3009；USDC/USDT 使用 Permit2 Exact。
- USDC capability metadata 为 `USD Coin / version 1`。
- 只发送 `Payment-Signature`，不发送 `X-Payment` 或 `Wallet-Signature`。
- 202 后立即安全保存 Job Token，并使用 `X-Job-Token` 查询任务。
- 相同任务的网络歧义重试必须复用同一 proof，避免重复授权或重复结算。
- 遵循 429 `Retry-After` 和每钱包 30 次/滚动小时限制。
