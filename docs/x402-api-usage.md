# x402 API usage on BSC Mainnet

The public x402 base URL is `https://stock-agent.bnbchain.org`. Append
`/x402/price`, `/x402/analyze/async`, `/x402/jobs/{jobId}`, or
`/x402/jobs/{jobId}/resume`; do not append `/mainnet`, and do not use the raw
AgentCore invocation URL.

## Paid token contract

Every paid analysis costs exactly 0.21 of one 18-decimal token
(`210000000000000000` atomic units).

| Token | BSC address | Method | Price |
| --- | --- | --- | --- |
| U | `0xcE24439F2D9C6a2289F741120FE202248B666666` | `eip3009` | 0.21 U |
| USD1 | `0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d` | `eip3009` | 0.21 USD1 |
| USDC | `0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d` | `permit2-exact` | 0.21 USDC |
| USDT | `0x55d398326f99059fF775485246999027B3197955` | `permit2-exact` | 0.21 USDT |

B402 capabilities may be partial. Treat `supportedAssets` as public metadata,
then select only a requirement in the live `accepts` set whose asset, method,
amount, network, and metadata match this table. Do not synthesize a missing
requirement or fall back to another token silently.

U and USD1 use EIP-3009 `TransferWithAuthorization`. USDC and USDT use
`permit2-exact`: `spenderAddress` comes from the live B402 capability and must
be copied into and signed with the returned requirement. This dynamic spender
is not the ERC-20 approval target. The token allowance always targets canonical
Permit2 `0x000000000022D473030F116dDEE9F6B43aC78BA3`.

## Explicit Permit2 allowance management

Set `BSC_RPC_URL` to an HTTP(S) BSC Mainnet (chain ID 56) RPC URL only when
managing or preflighting USDC/USDT. `BSC_RPC_URL` is used only for USDC/USDT
allowance reads, approval/revoke, and paid preflight; U/USD1 and local signing
do not use it.

From `buyer-client/`, select exactly `USDC` or `USDT`:

```bash
npm run x402:allowance -- USDC
npm run x402:approve -- USDC
npm run x402:revoke -- USDC
```

The three scripts are `npm run x402:allowance`, `npm run x402:approve`, and
`npm run x402:revoke`. Approval requires typing exactly `yes` unless the
operator explicitly supplies `--yes`. A differing nonzero allowance is first
reset to zero and then set to exactly 50 tokens; revoke sets exactly zero. A
50-token allowance covers 238 complete 0.21 payments and leaves 0.02 token.
Values below 0.21 or above 50 fail the paid preflight.

`npm run x402:async` never approves or revokes. For a new USDC/USDT flow it
checks the existing canonical-Permit2 allowance before any x402 request or
payment signature. Operators should revoke when the allowance is no longer
needed.

## Paid request and durable recovery

1. Run `X402_PAYMENT_TOKEN=USDC npm run x402:async` (or select U, USD1, or
   USDT).
2. A new Permit2 flow validates its RPC network and allowance first. U/USD1 do
   not construct an RPC provider.
3. The client requests a challenge, strictly validates the selected returned
   requirement, signs it, and atomically stores the exact proof and request in
   `.agent-data/x402-pending-create.json` with owner-only mode `0600` before
   the first paid POST.
4. On HTTP 202 it durably stores the private receipt, then polls with the
   private job token. Neither proof, signature, job token, nor private report
   URL is printed.

If a POST response is lost or a process restarts, the pending Permit2
settlement is resumed with the same proof, nonce, requirement metadata, and
request body. The client does not sign a replacement authorization. Pending
and receipt recovery also run before current token/RPC configuration is read,
so recovery is not blocked by a later environment change. A stale lock may be
removed only after confirming no async client process is running; never delete
the pending record merely to bypass recovery.

## Promotional mode

Promotional mode exposes only U and USD1; USDC and USDT are excluded. With
`X402_PROMO_FREE_MODE=1`, a create can return HTTP 202 without a wallet or
`Payment-Signature`; it never invokes B402 verify/settle, Permit2, RPC, or a
chain write. The `npm run x402:async` promotional path never approves.
`supportedAssets` remains capability metadata and must not be interpreted as a
payment requirement when `paymentRequired=false` and `accepts=[]`.

The promotional quota is 30 accepted creates per trusted IP in a rolling 24
hours and is process-local. Restarts clear it and replicas do not share it.
The source IP comes from trusted API Gateway request context, never a caller
header. `/x402/free` remains retired.
