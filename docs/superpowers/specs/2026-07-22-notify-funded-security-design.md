# Secure `notify_funded` Design

Date: 2026-07-22

## Objective

Prevent an A2A caller from using another buyer's funded ERC-8183 job to replace
delivery parameters, exfiltrate a personalized report, make the seller request
internal network resources, or inject untrusted portfolio instructions into the
LLM prompt.

The new protocol is intentionally fail-closed. Unsigned legacy
`notify_funded` requests are rejected; backward compatibility is out of scope.

## Security invariants

1. Only the wallet recorded as the on-chain job `client` may supply off-chain
   delivery or portfolio context for that job.
2. Every field used for delivery or prompt construction is covered by the
   buyer's signature.
3. No per-job context is stored until identity, job funding, input schema, and
   gateway policy checks all pass.
4. The first authorized context for a job is immutable. An identical retry is
   idempotent; a different context is rejected.
5. A valid buyer signature does not grant access to the seller's internal
   network. Gateway SSRF protection is enforced independently of authorization.
6. Transient delivery retries reuse the same authorized context. Terminal jobs
   cannot acquire new context.

## Considered approaches

### 1. EIP-712 signed notification (selected)

The wallet that created the job signs the complete notification. The seller
recovers the signer and compares it with the on-chain job client.

This keeps the trust decision portable and cryptographically tied to the ERC-8183
identity without requiring changes to the commerce contracts.

### 2. Context commitment in the job description

The buyer could place gateway and portfolio commitments in the description
covered by the seller's negotiated quote. This provides strong immutability but
requires the final delivery context before job creation and complicates token
rotation and operational recovery.

### 3. OAuth client-to-wallet registry

The seller could map a Cognito client ID to an EVM wallet. This is easier to
operate initially but introduces a centralized identity registry, makes wallet
rotation stateful, and does not work in unauthenticated local development.

OAuth remains an endpoint access control layer, but is not the job-ownership
proof.

## Wire protocol

The buyer sends a structured DataPart with this logical shape:

```json
{
  "skill": "notify_funded",
  "job_id": "123",
  "authorization": {
    "context": "{...exact JSON string...}",
    "expires_at": 1784725200,
    "nonce": "0x<32 bytes>",
    "signature": "0x<65-byte ECDSA signature>"
  }
}
```

`context` is an exact JSON string produced by the buyer and is the only source
for the delivery and portfolio fields. The seller hashes/signs the exact UTF-8
bytes and then parses that same string. This avoids cross-language canonical JSON
differences, particularly JavaScript `1` versus Python `1.0` serialization.

The parsed context has this shape:

```json
{
  "delivery_gateway_url": "https://example.trycloudflare.com",
  "delivery_gateway_token": "opaque bearer token",
  "portfolio": [
    {"symbol": "AAPL", "shares": 10, "avgCost": 190.25, "currency": "USD"}
  ],
  "risk_profile": {
    "tolerance": "moderate",
    "horizonMonths": 12,
    "preferredIndicators": ["RSI-14", "MACD"]
  }
}
```

Optional fields are represented by their absence inside the signed context, not
by unsigned top-level request fields. If legacy top-level gateway or portfolio
fields are present, the request is rejected rather than ambiguously merged.

### EIP-712 typed data

Domain:

- `name`: `stockanalyst-notify-funded`
- `version`: `1`
- `chainId`: the configured ERC-8183 network chain ID
- `verifyingContract`: the configured Commerce contract

Primary type `NotifyFunded`:

- `jobId`: `uint256`
- `context`: `string`
- `expiresAt`: `uint64`
- `nonce`: `bytes32`

The domain prevents cross-chain and cross-contract replay. `jobId` prevents
cross-job replay. The exact context prevents any gateway, token, portfolio, or
risk-profile mutation. `expiresAt` limits captured-message reuse. Nonces must be
exactly 32 bytes and provide uniqueness for signing and audit correlation.

The seller allows a small clock-skew tolerance but rejects notifications that
are expired or unreasonably far in the future. An identical authorized context
may be re-signed with a fresh expiry after a process restart.

## Seller authorization flow

For a named `notify_funded` call, the seller performs these steps in order:

1. Parse a non-negative `job_id` without narrowing it through a JavaScript
   `number`.
2. Require the new authorization envelope and reject legacy top-level context.
3. Validate authorization field shapes and expiry.
4. Read the on-chain job and run the existing signed/funded-job verification.
5. Recover the EIP-712 signer using server-owned domain values.
6. Compare the recovered address with the on-chain job `client` using normalized
   EVM address comparison.
7. Parse and validate the signed context schema.
8. Apply gateway SSRF policy.
9. Atomically install an immutable `JobContext`, or compare it with an existing
   context for idempotency/conflict.
10. Start or deduplicate background delivery.

Failures through step 8 do not mutate per-job state or start background work.
Business rejections return a structured rejected response without exposing the
raw token, signature, portfolio, or internal verification details.

A bare `notify_funded` call with no job ID may continue to trigger the existing
funded-job sweep because it supplies no off-chain context. Swept jobs use default
storage and their signed on-chain work specification.

## Job context lifecycle and concurrency

The separate `_job_gateways` and `_job_portfolios` dictionaries are replaced by
one immutable per-job context record containing:

- a digest of the exact signed context;
- normalized gateway URL and token, when present;
- validated portfolio and risk profile.

Installation uses one synchronous compare-and-set operation on the event loop:

- no existing context: install it;
- same digest: accept as an idempotent retry;
- different digest: reject with `context_conflict`.

The background worker reads rather than pops context at its start. Transient
failures therefore cannot fall back to an attacker-planted or empty context on a
later sweep. Context is removed only after a terminal outcome while the terminal
job remains protected by the existing in-flight/handled set.

Two differently signed contexts from the legitimate buyer may race; whichever
authorized request completes compare-and-set first wins. The other receives an
explicit conflict and cannot silently change an in-progress delivery.

## Gateway policy

Authorization and SSRF prevention are separate controls because the legitimate
buyer is still untrusted with respect to the seller's network.

Production defaults:

- scheme must be `https`;
- URL must be an origin only: no username, password, path other than `/`, query,
  or fragment;
- port must be the default HTTPS port;
- hostname must resolve successfully;
- every resolved address must be globally routable and must not be loopback,
  private, link-local, multicast, unspecified, reserved, or otherwise special;
- HTTP redirects are disabled rather than followed;
- normal certificate and hostname verification remains enabled;
- upload response size is bounded and must be JSON containing a bounded,
  syntactically valid `payload_id`.

Deployment may further restrict accepted host suffixes with an allowlist. The
stockanalyst deployment should default to the Cloudflare tunnel suffix it uses,
while retaining a configuration mechanism for an operator-approved gateway.

Local development may set `ALLOW_PRIVATE_DELIVERY_GATEWAY=true`. In that mode,
the only private exception is an HTTP loopback origin such as
`http://127.0.0.1:9444`; arbitrary RFC1918, link-local, and metadata addresses
remain forbidden. The exception is explicit and disabled by default.

Network egress policy remains recommended as defense in depth because
application-level DNS validation cannot fully replace an infrastructure egress
boundary.

## Context validation and prompt safety

Validation is allowlist-based and produces new normalized data structures rather
than passing caller-owned objects through.

Portfolio rules:

- array with a bounded number of holdings;
- each holding is an object with no unexpected fields;
- symbol matches a bounded uppercase ticker pattern;
- shares and average cost are finite numbers in defined non-negative ranges;
- currency is a bounded uppercase currency code.

Risk-profile rules:

- tolerance is exactly `conservative`, `moderate`, or `aggressive`;
- horizon is a bounded positive integer;
- preferred indicators are selected from a fixed supported set and are bounded
  in count.

These rules prevent control characters and arbitrary prose from reaching the
prompt. Prompt construction uses only the validated normalized values. The
on-chain signed job description remains the authoritative list of stocks and
work terms.

## Buyer changes

The buyer client passes its existing job-creation wallet to `notifyFunded` and:

1. builds the context object from the relay and UOMP data;
2. serializes it once to the exact context string;
3. creates a random 32-byte nonce and short expiry;
4. signs the typed data with `signTypedData`;
5. sends the decimal job ID as a string plus the authorization envelope.

The client never logs the context string, gateway bearer token, or signature.
Existing OAuth headers remain unchanged.

## Error behavior

Expected rejection reasons are stable, coarse codes suitable for clients and
logs, including:

- `authorization_required`
- `authorization_expired`
- `invalid_authorization`
- `caller_not_job_client`
- `invalid_context`
- `unsafe_gateway`
- `context_conflict`
- existing permanent on-chain verification reasons

Logs include job ID and reason code but exclude tokens, signed context, portfolio
contents, and signatures. Unexpected failures continue through the existing
internal-error path.

## Test strategy

Tests are added before implementation and must first fail for the missing
security behavior.

Seller unit tests cover:

- unsigned and malformed authorization rejection;
- expiry and wrong-domain rejection;
- a deterministic valid EIP-712 signature from the job client's wallet;
- a valid signature from a different wallet being rejected;
- mutation of any signed context byte invalidating the signature;
- permanent and transient job-verification failures leaving no context;
- identical retry idempotency and conflicting context rejection;
- concurrent notifications not changing an installed context;
- context retention across transient delivery retry;
- portfolio/risk-profile schema limits and injection strings;
- gateway scheme, credentials, path, query, fragment, IP classes, DNS results,
  redirect, response-size, and payload-ID rules;
- the explicit development loopback exception.

Buyer tests cover typed-data construction, decimal job-ID preservation, and a
fixed cross-language signature vector that the Python verifier also consumes.

Verification includes the complete Python unit suite, TypeScript test/build,
and existing tests that do not require live chain credentials. Live-chain E2E is
optional unless its required funded test job and credentials are already
available; secrets are never printed or committed.

## Out of scope

- Preserving unsigned legacy notifications.
- Changing ERC-8183 smart contracts.
- Making OAuth credentials represent blockchain wallet ownership.
- Redesigning the default-storage sweep path.
- General prompt hardening unrelated to the signed portfolio and risk context.
