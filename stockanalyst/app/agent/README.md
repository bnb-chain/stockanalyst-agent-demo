# Stock Analyst seller runtime

This runtime serves paid asynchronous x402 jobs at
`https://stock-agent.bnbchain.org`: `GET /x402/price`,
`POST /x402/analyze/async`, `GET /x402/jobs/{jobId}`, and
`POST /x402/jobs/{jobId}/resume`.

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
Each wallet may receive 30 accepted new jobs per rolling hour. The 31st new
job receives HTTP 429 with `Retry-After`. An exact retry does not consume
another slot. Competition reporting occurs once after terminal settlement or
queued state using `settledAt`.

ERC-8183 is a separate escrow channel, not x402; its fixed price remains 0.21
U and its behavior is unchanged.
