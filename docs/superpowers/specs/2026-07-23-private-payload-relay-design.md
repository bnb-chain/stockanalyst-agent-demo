# Private Payload Relay Design

**Date:** 2026-07-23

## Problem

The buyer relay currently treats a payload ID as a secret while publishing the
complete payload URL on-chain. Anyone observing the chain can therefore fetch a
personalized report without authentication. IDs contain only four random bytes,
and authenticated uploads are buffered without per-request or aggregate memory
limits.

## Security Invariants

1. The on-chain `deliverable_url` is a locator, not an access capability.
2. Relay credentials must never appear in that URL, query string, fragment, or
   on-chain metadata.
3. POST, GET, and HEAD payload operations require the relay Bearer token.
4. Authentication happens before payload lookup so unauthenticated callers
   cannot enumerate IDs.
5. Payload IDs contain at least 128 bits of cryptographic randomness.
6. One upload can retain at most 2 MiB of body data.
7. Stored plus in-flight upload data is bounded to 16 MiB per relay.
8. Stored plus active uploads are bounded to 32 payload slots per relay.
9. Rejected, aborted, and failed uploads release every reserved byte and slot.
10. Existing payloads are never evicted to admit a new upload because their
    locators may already be on-chain.

## Protocol

The relay reuses its existing random gateway token:

```http
POST /v1/payload/upload
Authorization: Bearer <gateway-token>

GET /v1/payload/<id>
Authorization: Bearer <gateway-token>

HEAD /v1/payload/<id>
Authorization: Bearer <gateway-token>
```

The upload response remains:

```json
{"payload_id":"pay_<32 lowercase hex characters>","size":1234}
```

The seller stores only
`https://<relay-origin>/v1/payload/<payload-id>` on-chain. The buyer retains the
token in its local `GatewayRelay` object. The Python gateway provider already
holds the same token from the signed notification context and uses it for
upload, download, and existence checks.

Missing or invalid credentials return `401`. Unknown authenticated IDs return
`404`. HEAD returns the same status and content metadata as GET without a
response body. Bearer values are compared as exact UTF-8 byte strings with a
constant-time comparison after an equal-length check. Tokens are never written
to response bodies or logs.

## Relay State and Limits

Each running relay owns an isolated store and accounting state; payloads are no
longer held in a module-global Map.

Constants:

- `MAX_PAYLOAD_BYTES = 2 * 1024 * 1024`
- `MAX_RELAY_BYTES = 16 * 1024 * 1024`
- `MAX_RELAY_PAYLOADS = 32`

An authenticated upload reserves one payload slot before reading its body.
Every received chunk is counted against both the request limit and the relay's
aggregate in-flight byte count. This prevents many concurrent, individually
valid requests from bypassing the total-memory bound.

If `Content-Length` is present and exceeds the request limit, the relay returns
`413 Payload Too Large` before buffering. A streamed request crossing the same
limit also returns `413`. If accepting a chunk would exceed aggregate relay
capacity, or no slot is available, the relay returns `507 Insufficient
Storage`. A malformed or negative Content-Length returns `400 Bad Request`.
After any early response, the request body is drained or destroyed without
retaining further chunks.

On success, in-flight bytes become stored bytes atomically and the reserved
slot becomes a stored payload. On rejection, abort, or request error, the relay
releases the request's in-flight bytes and active slot exactly once. It never
stores or concatenates a partial body.

Payload IDs use:

```ts
`pay_${randomBytes(16).toString("hex")}`
```

The timestamp is removed because it contributes no secrecy.
If an ID collides with an existing payload, the relay generates another ID
instead of overwriting the existing on-chain target.

## Client Changes

The buyer's report fetch sends:

```ts
fetch(deliverableUrl, {
  headers: { Authorization: `Bearer ${relay.token}` },
  redirect: "error",
})
```

The buyer adds this header only when the URL has no userinfo, query, or
fragment; its origin exactly matches the relay's public or local origin; and
its path matches the canonical payload-ID route. Redirects are rejected. A
different origin or non-payload path is fetched without relay credentials so
the token cannot be disclosed to seller-controlled URLs. If no relay exists,
the existing credential-free fetch behavior remains for non-relay storage.

`UOMPGatewayStorageProvider.download` and `.exists` send the same Authorization
header they already use for upload. No token is added to returned URLs.
The provider accepts only canonical `pay_` plus 32-lowercase-hex IDs in upload
responses and payload paths, matching the relay and buyer fetch helper.

The unauthenticated health endpoint returns only `{"status":"ok"}` and does not
expose payload counts.

## Testing

TypeScript loopback tests use an ephemeral local port and no external network.
They cover:

- unauthorized GET, HEAD, and POST;
- authenticated upload followed by authenticated GET and HEAD;
- absence of credentials in the returned payload URL;
- 16-byte random ID format and uniqueness;
- early `Content-Length` rejection;
- chunked upload crossing 2 MiB;
- aggregate stored/in-flight 16 MiB accounting;
- concurrent 32-slot enforcement;
- abort/error cleanup followed by a successful upload;
- isolated stores for two relay instances;
- health output without payload counts.

Buyer integration tests verify the report fetch includes the relay token.
Python provider tests verify upload, download, and exists all send the Bearer
header while returned/on-chain URLs contain no token.

No test contacts Cloudflare, a live chain, a wallet, or an external endpoint.

## Scope

This change protects access to the existing in-memory relay. It does not add
durable storage, public third-party verification, payload encryption, key
exchange, token rotation, or automatic eviction. Those require separate
protocol designs.
