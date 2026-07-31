# Binance B402 V2 Integration Design

## Context

The paid x402 path currently uses legacy Binance Pay authentication:
HMAC-SHA512, `BinancePay-*` headers, a `B402_SECRET`, and a hard-coded
`https://bpay.binance.com` default. It also sends the older payment payload
shape directly to `/settle`.

The current Binance B402 V2 API instead requires merchant-issued `clientId` and
`accessToken`, a partner-owned Base64 PKCS#8 RSA private key, and an
environment-specific base URL supplied during onboarding. Each request is
signed with RSA-SHA256 over the exact request body followed by the millisecond
timestamp. A merchant must discover the facilitator's current EIP-3009
configuration through `/supported`, forward that configuration to the buyer in
the HTTP 402 response, call `/verify`, and only then call `/settle`.

The existing asynchronous S3 job lifecycle, local EIP-712 verification,
idempotency reservation, and report generation remain valid and should not be
redesigned.

## Goals

- Implement the Binance B402 V2 merchant API exactly enough to run the current
  BSC Testnet U-token EIP-3009 flow.
- Use RSA-SHA256 and the documented `X-Tesla-*` headers.
- Treat the configured sandbox base URL as mandatory; never silently fall back
  to production or a guessed host.
- Fetch and cache `/supported`, select the BSC Testnet EIP-3009 U-token kind,
  and forward its complete `extra` object to buyers.
- Make the buyer sign and submit the official V2 `paymentPayload` shape.
- Call `/verify` before the irreversible `/settle` operation.
- Preserve the existing durable job identity, retry, settlement-recovery, and
  report-delivery behavior.
- Fail closed on missing configuration, malformed responses, ambiguous
  transport failures, or an unsupported network/payment kind.
- Keep all credentials out of source, logs, tests, and generated artifacts.

## Non-goals

- Creating, reading, rotating, or deploying the user's B402 credentials.
- Supporting Permit2 or the `upto` scheme in this change.
- Changing the free x402 quick-quote protocol.
- Changing the generic facilitator fallback or demo mode beyond keeping their
  behavior isolated from the B402 implementation.
- Deploying the new build or sending a real settlement request.

## Chosen Architecture

### Dedicated B402 client

Add a focused `b402_client.py` module. It owns configuration validation,
canonical request serialization, RSA signing, HTTP calls, response-envelope
validation, and supported-kind caching. The x402 HTTP handler remains
responsible for routing and the existing job service remains responsible for
idempotency.

The client reads four environment variables:

- `B402_CLIENT_ID`
- `B402_ACCESS_TOKEN`
- `B402_BASE_URL`
- `B402_PRIVATE_KEY`

`B402_PRIVATE_KEY` is the one-line Base64 encoding of the PKCS#8 DER private
key described by Binance. B402 mode is active only when all four values are
present. A partial configuration is reported as unavailable and the paid
endpoint fails closed. Secret values and signatures are never logged.

For every request, the client:

1. serializes the JSON object exactly once using compact UTF-8 JSON;
2. creates a millisecond timestamp;
3. signs `body + timestamp` with RSA PKCS#1 v1.5 and SHA-256;
4. sends the same serialized bytes as the HTTP body with
   `Content-Type`, `X-Tesla-ClientId`, `X-Tesla-SignAccessToken`,
   `X-Tesla-Timestamp`, and `X-Tesla-Signature`.

This avoids a signature mismatch caused by signing one JSON representation and
letting the HTTP library reserialize another.

### Supported-kind discovery

The first paid price/challenge request calls
`POST /papi/v2/b402/supported` with `{}`. The parsed successful response is
cached in memory for one hour. Concurrent cache misses share a lock so a
single runtime does not stampede Binance.

The selector requires:

- `x402Version == 2`
- `scheme == "exact"`
- `network == "eip155:97"`
- `extra.name` equal to the configured U-token domain name
- `extra.version` equal to the configured U-token domain version
- `extra.assetTransferMethod == "eip3009"`
- a valid `extra.signerAddress`

The complete returned `extra` mapping is copied into the payment requirement.
No facilitator address is hard-coded. A missing match makes the paid endpoint
temporarily unavailable instead of issuing an unusable challenge.

### Buyer-facing V2 challenge and proof

The paid 402 response uses the official requirement fields:

```json
{
  "x402Version": 2,
  "accepts": [{
    "scheme": "exact",
    "network": "eip155:97",
    "amount": "1000000000000000000",
    "asset": "0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565",
    "payTo": "0x...",
    "maxTimeoutSeconds": 600,
    "extra": {
      "name": "U",
      "version": "1",
      "assetTransferMethod": "eip3009",
      "signerAddress": "0x..."
    }
  }]
}
```

The asynchronous buyer first performs an unpaid request to obtain this
challenge, validates that the selected requirement matches its expected BSC
Testnet token and seller, and then signs EIP-3009 using the domain values in
`extra`. It submits Base64 JSON in the official V2 shape:

```json
{
  "x402Version": 2,
  "resource": {
    "url": "https://.../x402/analyze/async",
    "description": "Stock analysis report",
    "mimeType": "application/json"
  },
  "accepted": {
    "...": "the exact selected payment requirement"
  },
  "payload": {
    "signature": "0x...",
    "authorization": {
      "...": "EIP-3009 authorization"
    }
  }
}
```

The seller's local verifier is updated to validate the `accepted` requirement
and its equality to the seller's current requirement before checking the
EIP-712 signature. The free endpoint retains its current zero-value proof
shape so this paid-flow change does not alter its behavior.

Durable pending buyer state stores the complete Base64 V2 proof. Existing
retry logic therefore resubmits byte-for-byte identical authorization data.

### Verify and settle

After the existing local validation and durable settlement reservation, the
settlement callback reconstructs:

```json
{
  "x402Version": 2,
  "paymentPayload": "<decoded official V2 proof>",
  "paymentRequirements": "<paymentPayload.accepted>"
}
```

It first posts this identical request to `/papi/v2/b402/verify`. Settlement
continues only when the common response envelope has `code == "000000"` and
`data.isValid == true`. A structured invalid response becomes an explicit
payment rejection.

The same body is then posted to `/papi/v2/b402/settle`. A successful settlement
requires `code == "000000"`, `data.success == true`, and a non-empty
`data.transaction`.

Failure handling preserves the job service's existing safety model:

- authentication errors, malformed JSON/envelopes, HTTP 5xx, timeouts, and
  transport loss are indeterminate and leave the reservation recoverable;
- a verified structured rejection with no broadcast transaction is terminal;
- `success == false` with a non-empty transaction is indeterminate because the
  transaction may still be confirming, so retry/reconciliation remains safe;
- no report work starts until settlement is confirmed.

## Testing Strategy

Tests use generated in-memory RSA keys and fake HTTP clients. They never use the
user's credentials or contact Binance.

Coverage includes:

- exact RSA signature verification over the exact transmitted body and
  timestamp;
- correct `X-Tesla-*` headers with no legacy headers;
- strict complete/partial configuration handling;
- `/supported` selection, one-hour caching, refresh, and malformed-response
  failures;
- propagation of `signerAddress` into the 402 challenge;
- buyer challenge validation and official V2 proof construction;
- local validation of official V2 paid proofs while free proofs remain
  compatible;
- `/verify` preceding `/settle`;
- valid, rejected, malformed, HTTP 5xx, timeout, and broadcast-pending response
  handling;
- regression tests for asynchronous job idempotency and report delivery.

## Rollout

This code change does not deploy or invoke B402. After tests pass, deployment is
a separate step that injects the four existing values into the runtime's secret
configuration and updates the runtime. A read-only `/supported` or price check
may then validate integration. Any real `/settle` test requires separate
explicit approval because it can execute an irreversible on-chain payment.
