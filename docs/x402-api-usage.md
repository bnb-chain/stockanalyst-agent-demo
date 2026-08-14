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
`permit2-exact`. `extra.signerAddress` is facilitator EOA metadata; it is not
the Permit2 spender and is not part of `permit2-exact` typed data.
`extra.spenderAddress` is the live B402 proxy and the `permit2-exact`
typed-data spender; the ERC-20 approval target remains canonical Permit2
`0x000000000022D473030F116dDEE9F6B43aC78BA3`.

## Price and challenge metadata

The active `accepts` list is authoritative. `signingSchemes` is additive and
lists the deduplicated active methods in `accepts` order. The legacy
`signingScheme` describes the highest-priority active accept. Clients that do
not understand `signingSchemes` can continue reading `signingScheme`, while
new clients should validate the method on their selected exact accept.

## Permit2 exact signature contract

The Permit2 EIP-712 domain is exactly the following three fields and has no
`version` field:

```json
{
  "name": "Permit2",
  "chainId": 56,
  "verifyingContract": "0x000000000022D473030F116dDEE9F6B43aC78BA3"
}
```

The primary and nested types have this exact field order:

```text
PermitWitnessTransferFrom(
  TokenPermissions permitted,
  address spender,
  uint256 nonce,
  uint256 deadline,
  Witness witness
)
TokenPermissions(address token, uint256 amount)
Witness(address to, uint256 validAfter)
```

For Permit2, the exact `payload` value on the wire is:

```json
{
  "signature": "0x<65-byte signature>",
  "permit2Authorization": {
    "permitted": {
      "token": "<accepted.asset>",
      "amount": "210000000000000000"
    },
    "from": "<payer wallet>",
    "spender": "<lowercase accepted.extra.spenderAddress>",
    "nonce": "<uint256 decimal string>",
    "deadline": "<unix seconds decimal string>",
    "witness": {
      "to": "<accepted.payTo>",
      "validAfter": "<unix seconds decimal string>"
    }
  }
}
```

Every uint256 wire value (`amount`, `nonce`, `deadline`, and `validAfter`) is a
canonical decimal string. The authorization `spender` is the lowercase
canonical form of `accepted.extra.spenderAddress`; the complete
`accepted.extra` object remains unchanged on the wire. This includes additive
facilitator metadata.

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
`npm run x402:revoke`. Both approve and revoke require confirmation; `--yes` is
an explicit noninteractive bypass. A differing nonzero allowance is first
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

Only a freshly created Permit2 reservation in the same request uses
verify-and-settle. Every pre-existing stale Permit2 reservation is recovered
settle-only with the identical persisted proof, regardless of
`pendingSettlementReference` or deadline; recovery does not call `/verify`.
This applies after response loss before a pending marker, response loss before
the terminal settled transition, a lost POST response, or a process restart.
Recovery creates no new signature, nonce, or approval and never starts
analysis before terminal settlement. The proof includes the original
requirement metadata and request body. Pending and receipt recovery also run
before current token/RPC configuration is read, so recovery is not blocked by
a later environment change. A stale lock may be removed only after confirming
no async client process is running; never delete the pending record merely to
bypass recovery.

## Promotional mode

In promotional mode, `paymentRequired=false` and the active `accepts=[]`;
`supportedAssets` may still list all four tokens as registry metadata and is
not an active payment requirement. There is no USDC/USDT promotional proof,
B402 verify/settle, or automatic approval. With `X402_PROMO_FREE_MODE=1`, a
create returns HTTP 202 without a wallet or `Payment-Signature` and performs no
chain write. On a genuinely new CLI run with `X402_PAYMENT_TOKEN=USDC` or
`USDT`, the zero-POST safety policy may perform a read-only Permit2 preflight
before discovering the proofless promotional response; it still performs no
approval.

The promotional quota is 30 accepted creates per trusted IP in a rolling 24
hours and is process-local. Restarts clear it and replicas do not share it.
The source IP comes from trusted API Gateway request context, never a caller
header. `/x402/free` remains retired.
