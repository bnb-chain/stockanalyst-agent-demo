# x402 API usage on BSC Mainnet

The paid x402 base URL is `https://stock-agent.bnbchain.org`. Its routes are `GET /x402/price`, `POST /x402/analyze/async`, `GET /x402/jobs/{jobId}`, and `POST /x402/jobs/{jobId}/resume`. Stored-job routes use the private token returned by create.

## Paid token contract

Every accepted analysis costs exactly `100000000000000000` atomic units (0.1 of the selected 18-decimal token).

| Token | BSC address | Method | Price |
| --- | --- | --- | --- |
| U | `0xcE24439F2D9C6a2289F741120FE202248B666666` | `eip3009` | 0.1 U |
| USD1 | `0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d` | `eip3009` | 0.1 USD1 |
| USDC | `0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d` | `permit2-exact` | 0.1 USDC |
| USDT | `0x55d398326f99059fF775485246999027B3197955` | `permit2-exact` | 0.1 USDT |

B402 capabilities may be partial. `signingSchemes` is additive and lists the deduplicated active methods in `accepts` order. The legacy `signingScheme` describes the highest-priority active accept. U/USD1 use EIP-3009 and USDC/USDT use `permit2-exact`. `extra.signerAddress` is facilitator EOA metadata; it is not the Permit2 spender and is not part of `permit2-exact` typed data. `extra.spenderAddress` is the live B402 proxy and the `permit2-exact` typed-data spender; the ERC-20 approval target remains canonical Permit2 `0x000000000022D473030F116dDEE9F6B43aC78BA3`.

## EIP-3009 signature contract for U and USD1

U and USD1 use EIP-712 `TransferWithAuthorization` typed data; never use `eth_sign` or `personal_sign`. The EIP-3009 domain is copied from the selected requirement: `name` and `version` from `accepted.extra`, chain ID 56, and `verifyingContract` equal to `accepted.asset`.

```text
TransferWithAuthorization(
  address from,
  address to,
  uint256 value,
  uint256 validAfter,
  uint256 validBefore,
  bytes32 nonce
)
```

The authorization binds `from`, `to`, exact `value` 100000000000000000, `validAfter`, `validBefore`, and a fresh 32-byte `nonce`. `to` must equal `accepted.payTo`; the validity window must fit the advertised 600-second timeout. All uint256 values are encoded as canonical decimal strings in the JSON payload.

```json
{
  "signature": "0x<65-byte EIP-712 signature>",
  "authorization": {
    "from": "<payer wallet>",
    "to": "<accepted.payTo>",
    "value": "100000000000000000",
    "validAfter": "<unix seconds decimal string>",
    "validBefore": "<unix seconds decimal string>",
    "nonce": "0x<32 fresh bytes>"
  }
}
```

Copy the selected `accepted` requirement unchanged into the V2 proof and send its base64-encoded JSON only in `Payment-Signature`. The client must recover the signer locally, require the configured payer and pay-to addresses, reject expired windows or reused nonces, and never log the signature or private key.

`PAYMENT-RESPONSE` is conditional. The server emits it only when a validated
settlement response is available. A 202 exact retry that still observes a
durable job in `settling` may omit the header; clients must use the response
body and authenticated job polling as the authoritative lifecycle state.

## Permit2 exact signature contract

```json
{
  "name": "Permit2",
  "chainId": 56,
  "verifyingContract": "0x000000000022D473030F116dDEE9F6B43aC78BA3"
}
```

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

```json
{
  "signature": "0x<65-byte signature>",
  "permit2Authorization": {
    "permitted": {
      "token": "<accepted.asset>",
      "amount": "100000000000000000"
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

Every uint256 wire value (`amount`, `nonce`, `deadline`, and `validAfter`) is a canonical decimal string. The authorization `spender` is the lowercase canonical form of `accepted.extra.spenderAddress`; the complete `accepted.extra` object remains unchanged on the wire.

`BSC_RPC_URL` is used only for USDC/USDT allowance reads, approval/revoke, and paid preflight. The commands `npm run x402:allowance`, `npm run x402:approve`, and `npm run x402:revoke` are explicit operations. Both approve and revoke require confirmation; `--yes` is an explicit noninteractive bypass. `npm run x402:async` never approves or revokes. Only a freshly created Permit2 reservation in the same request uses verify-and-settle. Every pre-existing stale Permit2 reservation is recovered settle-only with the identical persisted proof, regardless of `pendingSettlementReference` or deadline; recovery does not call `/verify`.

## Wallet admission and reporting

After local cryptographic payment-proof verification identifies the wallet,
admission allows 30 accepted new jobs per rolling hour for each payer wallet
across Runtime replicas and restarts. The 31st new request returns HTTP 429 and
`Retry-After` before B402 verification or settlement. Explicit payment
rejection releases a newly created reservation. An exact retry reuses the
durable reservation, proof, job identity, and settlement. An exact retry does
not consume another slot or settle twice.

Payment is durably settled before the job enters `queued` and analysis starts.
Payment grants an accepted execution attempt; it does not guarantee a
successful report, and a later analysis failure does not automatically refund
the completed settlement. A failed job with `retryable: true` can be resumed
without another `Payment-Signature`, B402 verification, settlement, or charge.

Competition reporting becomes eligible after `paymentStatus=settled` and the
durable job leaves `settling`, normally when it reaches `queued`; it does not
wait for analysis completion. `calledAt` is the durable `settledAt` millisecond
timestamp. The stable identifier is
`b402:56:sha256(lowercase-wallet:canonical-nonce:lowercase-asset)`. Delivery is
optional, asynchronous, best-effort, and retried with bounded backoff. A lost
response can redeliver the same stable identifier, so the receiving API must
deduplicate by `eventId`; this is not transport-level exactly-once delivery.
Reporting failure does not fail the paid request or analysis job.

ERC-8183 is separate on-chain escrow, not x402. Its fixed price remains 0.21 U and its behavior is unchanged.
