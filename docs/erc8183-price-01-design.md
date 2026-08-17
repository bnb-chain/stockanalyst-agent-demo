# ERC-8183 0.1 U 定价调整设计

日期：2026-08-17
状态：已确认，待实施

## 目标

将 Stock Analyst Agent 的 ERC-8183 固定报价从 0.21 U 调整为 0.1 U，使其与
x402 的单次分析价格一致。

## 配置

`stockanalyst/app/agent/studio.toml` 中的 ERC-8183 配置调整为：

```toml
[payments.erc8183]
price = "100000000000000000"       # 0.1 U
min_price = "100000000000000000"   # 0.1 U
max_price = "5000000000000000000"  # 5 U
```

U 使用 18 位小数，因此 0.1 U 等于 `100000000000000000` 个原子单位。

`max_price` 保持 5 U。正常报价继续使用固定 `price`，`max_price` 只保留为报价安全
上界。

## 行为

- 部署后签发的新 ERC-8183 报价固定为 0.1 U；
- `price` 与 `min_price` 同时修改，避免 0.1 U 被旧的 0.21 U floor 重新夹紧；
- 部署前已经签发的 0.21 U 报价在原 15 分钟 TTL 内继续有效；
- 已创建、已托管或已付款的链上 Job 金额不修改；
- Buyer 继续使用 Seller 返回并签名的实际报价，不增加客户端硬编码价格；
- x402 继续保持每次 0.1 U、USD1、USDC 或 USDT，不修改其价格逻辑。

## 修改范围

- ERC-8183 `studio.toml` 的 `price` 与 `min_price`；
- Seller 报价与 clamp 契约测试；
- Buyer ERC-8183 精确金额、余额提示与格式化测试；
- mainnet 部署配置契约测试；
- 根 README、Buyer README、Stockanalyst README、Runtime README；
- 其他公开文档中仍声称 ERC-8183 为 0.21 U 的内容。

## 不修改

- ERC-8183 合约、Token 地址、Chain ID、争议窗口或结算流程；
- `max_price=5 U`；
- Buyer 的通用报价校验和 `MAX_PRICE_U` 上限；
- x402 支付、B402 verify/settle、钱包限流或 Competition；
- Gateway、Lambda、IAM、Role、Secret 结构和网络资源。

## 测试

- 先将现有 0.21 U 断言改成 0.1 U，确认 RED；
- 配置修改后确认 Seller `list_price()`、`price_bounds()` 和 `clamp_price()` 返回
  精确 0.1 U floor；
- Buyer 使用精确 0.1 U quote，并继续原样转发非浮点安全的签名原子金额；
- 部署契约确认 ERC-8183 `price/min_price=100000000000000000` 且
  `max_price=5000000000000000000`；
- 扫描公开 README，不能再把 ERC-8183 当前价格写成 0.21 U；
- 运行 Seller、Buyer 和 deployment/infra 相关回归。

## 上线

代码合并后使用现有 mainnet Runtime 和既有部署流程更新。部署不创建 Runtime、用户、
Role 或其他 AWS 资源。

真实签名、付款、结算、RPC 或链上测试必须另行取得明确授权。
