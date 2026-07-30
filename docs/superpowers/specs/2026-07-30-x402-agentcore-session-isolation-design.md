# x402 AgentCore Session Isolation

## Context

The Lambda adapter currently derives the AgentCore Runtime session ID from the
deterministic x402 gateway request ID. Identical public requests therefore
reuse the same AgentCore session. AgentCore sessions survive Runtime updates,
so a request can continue executing against stale in-session state after a
successful deployment.

This was reproduced directly: the public request's deterministic session
returned the pre-update `x402 envelope bridge is not configured` error, while a
new session returned a valid, request-bound JSON-RPC data response.

## Design

- Generate a fresh random AgentCore session ID for every adapter invocation.
- Keep the `x402-gateway-session-` prefix for operational identification.
- Use 32 lowercase hexadecimal random characters after the prefix.
- Keep the JSON-RPC `id`, A2A `messageId`, envelope `requestId`, payment proof,
  job token, body, and deterministic request digest unchanged.
- Continue validating the response JSON-RPC ID and data-part `requestId`
  against the deterministic envelope request ID.

The random value isolates only AgentCore transport sessions. It is not a
payment, settlement, or business idempotency key.

## Failure behavior

Random session generation occurs before transport invocation. If generation
fails, the existing safe internal-error boundary handles the failure and no
AgentCore request is sent. Transport timeouts and response validation retain
their existing behavior.

## Verification

- Two invocations of the same envelope produce different Runtime session IDs.
- Each session ID matches
  `x402-gateway-session-[0-9a-f]{32}`.
- Both invocations keep the same JSON-RPC ID, A2A message ID, and envelope
  request ID.
- Existing response binding, timeout, size, header, OAuth, and gateway tests
  remain green.
- After Lambda redeployment, no-payment public verification must return:
  - `GET /x402/price` → `200`;
  - `POST /x402/analyze/async` without `X-Payment` → `402`.
- No paid request, B402 call, job creation, funding, or U spend is permitted.
