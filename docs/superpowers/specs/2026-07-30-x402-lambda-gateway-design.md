# x402 Lambda Gateway Design

Date: 2026-07-30

## Summary

Publish the stock-analysis agent's paid asynchronous x402 API through an
AWS-managed, stateless HTTP adapter:

```text
x402 buyer
  -> API Gateway REST API
  -> AWS WAF
  -> Lambda x402 adapter
  -> Cognito client_credentials
  -> existing AgentCore A2A Runtime
  -> internal x402 envelope dispatcher
  -> B402 settlement
  -> OpenRouter analysis
  -> private S3 job storage
```

The adapter preserves standard x402-facing HTTP methods, paths, headers, and
status codes without deploying a second long-running HTTP service. The existing
AgentCore Runtime remains the only wallet holder and the only component that
verifies payment proofs, calls B402, runs the LLM, and stores job state.

The first release exposes only the paid asynchronous x402 flow. It does not
publish the free SSE endpoint, MCP, or A2A through the new gateway.

## Context

The existing AgentCore Runtime is configured for the A2A protocol. AgentCore
accepts HTTPS `InvokeAgentRuntime` calls, then transparently sends A2A JSON-RPC
payloads to the container's root path on port 9000. It is not a general-purpose
public reverse proxy for arbitrary container ports.

The application also implements REST-like x402 routes:

- `GET /x402/price`
- `POST /x402/analyze/async`
- `GET /x402/jobs/{jobId}`
- `POST /x402/jobs/{jobId}/resume`
- `GET|POST /x402/free`

These routes work during local single-port development because the x402 ASGI
handler wraps the A2A app on localhost. The self-hosted AgentCore deployment
does not expose the configured second port, and the buyer cannot use the raw
AgentCore invocation URL as a normal x402 origin.

The previously completed remote report test used the ERC-8183 A2A
`notify_funded` skill. It did not prove that the public x402 routes were
reachable through AgentCore.

## Goals

1. Present a conventional HTTPS x402 API to partners.
2. Preserve HTTP `402`, `202`, `404`, `409`, and `503` semantics.
3. Keep `X-Payment` and `X-Job-Token` at the public HTTP boundary.
4. Reuse the existing AgentCore Runtime, Cognito, fixed outbound IP, B402
   integration, OpenRouter model, and private S3 job implementation.
5. Keep all wallet operations and payment settlement inside AgentCore.
6. Provide anonymous paid x402 access with WAF-based abuse controls.
7. Make the adapter replaceable by a shared platform gateway later without
   changing the public API or AgentCore's internal envelope contract.

## Non-goals

- Publishing `/x402/free` or preserving SSE in the first release.
- Publishing MCP through the Lambda.
- Proxying the existing A2A endpoint.
- Moving B402 settlement, payment verification, wallet material, LLM work, or
  job persistence into Lambda.
- Adding a custom domain in the first release.
- Supporting arbitrary user-selected methods, paths, upstreams, or response
  headers.
- Replacing the existing AgentCore Runtime.

## Selected Architecture

### Public edge

Use an API Gateway REST API with Lambda proxy integration. Associate an AWS WAF
Web ACL with the deployed stage.

REST API is selected instead of HTTP API because the first release explicitly
requires a directly associated WAF rate-based rule for anonymous traffic. The
initial endpoint is the generated API Gateway HTTPS URL. A custom domain and
ACM certificate can be added later without changing the routes.

Only these route and method pairs exist:

| Method | Route | Public authentication |
|---|---|---|
| `GET` | `/x402/price` | Anonymous, WAF rate limited |
| `POST` | `/x402/analyze/async` | Anonymous transport; valid `X-Payment` required by AgentCore |
| `GET` | `/x402/jobs/{jobId}` | Valid `X-Job-Token` required by AgentCore |
| `POST` | `/x402/jobs/{jobId}/resume` | Valid `X-Job-Token` required by AgentCore |

There is no catch-all `ANY /{proxy+}` route. API Gateway and Lambda both reject
anything outside this table.

### Lambda adapter

Lambda performs five operations:

1. Validate the public method, path, body size, path parameters, and allowed
   headers.
2. Convert the HTTP request into an `envelope-v1` data object.
3. Obtain and cache a Cognito client-credentials token for the existing
   AgentCore Runtime.
4. Send a valid A2A JSON-RPC `message/send` request whose data part carries the
   envelope.
5. Validate the returned response envelope and reconstruct the public HTTP
   response.

Lambda does not parse or validate the Ethereum payment signature and does not
call B402. The Agent remains the security authority for both operations.

### AgentCore transport

The Lambda-to-AgentCore request remains valid A2A JSON-RPC rather than relying
on an undocumented non-A2A payload:

```json
{
  "jsonrpc": "2.0",
  "id": "gateway-request-id",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "messageId": "gateway-request-id",
      "parts": [
        {
          "kind": "data",
          "data": {
            "skill": "x402_http_envelope",
            "envelope": {
              "version": 1,
              "requestId": "opaque-id",
              "method": "POST",
              "path": "/x402/analyze/async",
              "headers": {},
              "bodyBase64": ""
            }
          }
        }
      ]
    }
  }
}
```

The `x402_http_envelope` operation is an infrastructure bridge, not an
LLM-callable tool. It is not advertised as a buyer business skill in the Agent
Card.

Lambda sends the required AgentCore session header. The session identifier is
opaque, at least 33 characters, and derived from the gateway request ID. It is
never returned to or accepted from the public caller.

### Internal dispatch

The Agent handles `x402_http_envelope` before normal `negotiate` and
`notify_funded` business dispatch.

The dispatcher:

1. Validates the envelope again.
2. Builds a synthetic ASGI HTTP scope, receive channel, and send collector.
3. Calls the existing `X402Handler` directly in-process.
4. Collects the non-streaming response.
5. Returns a restricted response envelope in the A2A data part.

It must not make a loopback HTTP request to localhost. Direct ASGI dispatch
avoids a second listener, socket overhead, Host confusion, request smuggling,
and differences between local and deployed routing.

The existing x402 business code remains the single implementation of payment
challenge generation, payment verification, B402 settlement, job creation,
job-token authorization, resume behavior, and safe error responses.

## Envelope Contract

### Request envelope

```json
{
  "version": 1,
  "requestId": "opaque-id",
  "method": "POST",
  "path": "/x402/analyze/async",
  "headers": {
    "accept": "application/json",
    "content-type": "application/json",
    "x-payment": "base64-payment-proof"
  },
  "bodyBase64": "base64-encoded-request-body"
}
```

Rules:

- `version` must equal integer `1`.
- `requestId` is generated by Lambda and matches a bounded opaque pattern.
- `method` and `path` must match the fixed route table.
- Query parameters are supported only where explicitly added to a route. The
  first release defines none.
- Header names are lowercase.
- Request headers are limited to:
  - `accept`
  - `content-type`
  - `x-payment`
  - `x-job-token`
- Body bytes are base64 encoded exactly once.
- Decoded request bodies must not exceed the application's existing asynchronous
  request limit of 256 KiB.
- GET requests carry an empty body.

The envelope never contains the public caller's `Authorization`, `Host`,
`X-Forwarded-*`, API Gateway headers, AWS trace headers, or hop-by-hop headers.

### Response envelope

```json
{
  "version": 1,
  "requestId": "opaque-id",
  "status": 202,
  "headers": {
    "content-type": "application/json",
    "location": "/x402/jobs/x402_...",
    "retry-after": "10",
    "cache-control": "private, no-store"
  },
  "bodyBase64": "base64-encoded-response-body"
}
```

Rules:

- `requestId` must exactly match the request.
- `status` is an integer from 100 through 599.
- Response headers are limited to:
  - `content-type`
  - `location`
  - `retry-after`
  - `cache-control`
  - `vary`
  - `x-payment-required`
- `set-cookie`, `authorization`, `transfer-encoding`, `connection`, arbitrary
  redirects, and all AWS-specific headers are rejected.
- The decoded response body has a strict bounded size.
- Streaming response frames are unsupported in envelope version 1.

Malformed AgentCore or envelope responses fail closed as public HTTP `502`
with a stable error body. Sensitive upstream details are logged only as safe
error codes.

## Authentication and Secrets

### Public caller

The x402 routes do not require Cognito credentials from partners:

- The price route is anonymous.
- The create route is authorized economically by the `X-Payment` proof.
- Job access and resume are authorized by `X-Job-Token`.

The existing Agent code performs the authoritative checks.

### Lambda to AgentCore

Create a dedicated Cognito machine client for the x402 gateway with only the
AgentCore invoke scope. Do not reuse or disclose the partner-facing client
secret.

Store the machine-client configuration as one Secrets Manager JSON secret:

```json
{
  "client_id": "...",
  "client_secret": "...",
  "token_url": "...",
  "scope": "bnbagent-seller/invoke"
}
```

The stack accepts the secret ARN as a parameter. It never embeds secret values.
The Lambda execution role receives `secretsmanager:GetSecretValue` only for
that ARN.

Lambda caches the OAuth access token in the warm execution environment until
shortly before expiry. Tokens and credentials are never logged.

## Idempotency and Retry Safety

API Gateway or Lambda may retry a request, and a caller may retry after losing a
response. Retrying must not settle the same authorization twice.

For `POST /x402/analyze/async`, Lambda derives a stable request ID from a
domain-separated SHA-256 digest of the method, canonical path, payment-proof
bytes, and request-body bytes. It does not log the digest inputs.

For status and resume calls, the request ID is derived from the method,
canonical path, job-token bytes, and body bytes.

The request ID is correlation and retry metadata, not the final settlement
guard. The Agent continues to enforce:

- EIP-3009 authorization nonce checks;
- on-chain authorization state checks;
- S3 conditional creation and state transitions;
- stable job-token authentication;
- safe recovery of indeterminate settlement.

Lambda treats a timeout after sending the envelope as indeterminate. It does
not invent a success or automatically send a different payment authorization.
The caller may retry the exact same request, which produces the same request ID
and reaches the Agent's idempotent recovery path.

## Rate Limiting and Abuse Protection

Associate AWS WAF with the API Gateway REST stage.

The Web ACL contains:

1. A rate-based IP rule scoped to `/x402/*`.
2. A request-size rule that rejects oversized requests before Lambda.
3. AWS-managed common exploit rules where they do not interfere with the
   base64 payment header.

API Gateway stage throttling provides an additional account-level safety net.
Lambda reserved concurrency caps downstream load on AgentCore and Cognito.

Only API Gateway/WAF-derived source IP information is trusted. Lambda ignores
caller-supplied forwarding headers.

## Logging and Observability

API Gateway access logs contain only:

- API request ID;
- route;
- final HTTP status;
- response latency;
- integration status.

API Gateway execution data tracing is disabled.

Lambda structured logs may contain:

- internal request ID;
- route;
- upstream outcome class;
- mapped status;
- duration;
- stable safe error code.

Logs must never contain:

- `X-Payment`;
- `X-Job-Token`;
- Cognito credentials or access tokens;
- wallet passwords or keystore data;
- portfolio or risk-profile payloads;
- S3 object keys;
- presigned URLs;
- B402 secrets;
- OpenRouter keys.

CloudWatch metrics and alarms cover:

- API Gateway 4xx/5xx and latency;
- Lambda errors, throttles, duration, and concurrency;
- AgentCore invocation failures;
- OAuth token failures;
- envelope validation failures;
- public `502` and `503` rates.

## Error Mapping

Application-level statuses returned inside a valid envelope are preserved.

Infrastructure failures map as follows:

| Failure | Public response |
|---|---|
| WAF block or rate limit | WAF/API Gateway response |
| Invalid public request before invocation | `400` or `413` |
| Lambda cannot obtain OAuth token | `503` |
| AgentCore auth rejection | `503` |
| AgentCore throttled or unavailable | `503` with bounded `Retry-After` |
| AgentCore timeout after dispatch | `503`, indeterminate and retryable |
| Invalid A2A response | `502` |
| Invalid response envelope | `502` |

The Lambda never returns raw AgentCore, Cognito, Python, AWS SDK, or stack-trace
messages.

## Current-runtime Prerequisites

The Lambda gateway must not be deployed for paid testing until these existing
source and runtime inconsistencies are corrected:

1. Replace the old hard-coded x402 seller address
   `0x1FF095E1C5Cf4bC72a3DC54be17B6cf85043Fb67` with the active seller wallet
   `0xd10BdDC20E4DC42A1a19a9653e994991e25b8153` in both seller verification and
   buyer payment construction.
2. Deploy the x402 asynchronous job source that was merged after the last
   AgentCore deployment.
3. Configure the private x402 job bucket, prefix, and job-token secret.
4. Configure `B402_CLIENT_ID` and `B402_SECRET`.
5. Confirm Binance has allowlisted AgentCore's outbound IP
   `52.73.72.22/32`.
6. Keep `X402_DEMO_MODE` disabled.

The fixed outbound IP applies to AgentCore's B402 request. Lambda does not call
B402 and therefore does not need the allowlisted egress IP.

## Infrastructure as Code

Add a separate CloudFormation or SAM template for the gateway. It owns:

- Lambda function and version;
- least-privilege Lambda execution role;
- API Gateway REST API, explicit resources, methods, deployment, and stage;
- Lambda invoke permission for API Gateway;
- WAF Web ACL, rules, and stage association;
- CloudWatch log groups with bounded retention;
- metric filters and alarms;
- parameters for AgentCore invocation URL, Cognito secret ARN, and operational
  limits.

It does not own or replace:

- the AgentCore application stack;
- the fixed-egress stack;
- Cognito user pool;
- seller wallet secret;
- B402 secret;
- OpenRouter secret;
- x402 private job bucket.

The gateway stack output includes the generated x402 base URL.

## Testing Strategy

### Unit tests

Lambda adapter tests cover:

- exact route and method allowlist;
- header allowlist and case normalization;
- body-size enforcement;
- stable request-ID derivation;
- A2A envelope construction;
- OAuth token caching and expiry;
- AgentCore response parsing;
- response status/header/body reconstruction;
- fail-closed malformed and oversized envelopes;
- secret-safe logging.

Agent tests cover:

- `x402_http_envelope` is not LLM callable;
- strict request and response envelope schemas;
- direct in-process ASGI dispatch;
- no arbitrary path dispatch;
- no loopback network call;
- preservation of `402`, `202`, `404`, `409`, and `503`;
- payment proof and job token are not logged;
- normal A2A `negotiate` and `notify_funded` remain unchanged.

### Integration tests

Run against a local fake AgentCore endpoint:

1. No payment returns a reconstructed HTTP `402`.
2. Valid create returns `202`, `jobId`, `jobToken`, and `Location`.
3. Poll without or with a wrong token returns the same `404`.
4. Resume preserves the Agent's status and retry semantics.
5. Duplicate exact create calls use the same internal request ID.
6. AgentCore outer HTTP 200 correctly reconstructs non-200 application status.
7. AgentCore outer 4xx/5xx maps to stable gateway errors.

### Deployed smoke tests

The staged rollout is:

1. Deploy the current AgentCore source and runtime prerequisites.
2. Invoke the envelope skill directly with operator credentials.
3. Deploy the gateway stack with a restrictive initial WAF rate.
4. Verify `/x402/price`.
5. Verify missing-payment `402`.
6. Run one explicitly authorized paid test.
7. Poll to completion and verify the S3 report.
8. Correlate API Gateway, Lambda, AgentCore, B402, S3, and chain evidence by
   safe request and job identifiers.
9. Confirm B402 observed the allowlisted AgentCore egress IP.
10. Reverify existing A2A/ERC-8183 behavior.

No paid smoke test runs without explicit approval for the 1 U spend.

## Rollback

Rollback does not mutate AgentCore jobs:

1. Disable the API Gateway stage or WAF-allow only the operator.
2. Set the existing x402 runtime acceptance flag to stop new jobs while
   preserving status, resume, and downloads for accepted jobs.
3. Keep Lambda, Cognito credentials, AgentCore, S3 state, and job-token secret
   available through the latest accepted job's access and download windows.
4. Delete the gateway stack only after no accepted job depends on it.

Existing A2A/ERC-8183 traffic remains available throughout gateway rollback.

## Alternatives Considered

### Dedicated EC2 proxy

Rejected for the first release because it introduces OS patching, process
supervision, TLS termination, scaling, and high-availability work for a
stateless short-request adapter.

### ECS/Fargate shared platform gateway

Viable when several agents share the gateway or when streaming protocols are
required. Deferred because the first release serves one agent and explicitly
excludes SSE.

### Convert x402 to public A2A skills

Rejected because partners would lose conventional HTTP `402`, `X-Payment`, and
job polling semantics and would require a custom A2A buyer.

### Change the existing Runtime from A2A to HTTP

Rejected because it would disrupt the deployed A2A/ERC-8183 contract and still
would not provide the required independent public authentication boundary.

## Decision

Implement the first x402 public gateway as API Gateway REST API + AWS WAF +
Lambda proxy adapter. Tunnel only the four paid asynchronous x402 routes through
a versioned envelope carried inside valid A2A JSON-RPC. Keep B402, wallet
operations, report generation, and private job state in the existing AgentCore
Runtime.
