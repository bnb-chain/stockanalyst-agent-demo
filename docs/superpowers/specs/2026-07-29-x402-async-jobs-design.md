# X402 Asynchronous Analysis Jobs Design

## Context

The paid B402/x402 endpoint currently verifies and settles payment, then keeps
the same HTTP request open while the stock-analysis pipeline emits an SSE
stream. Full reports can take several minutes. Heartbeats reduce idle timeouts,
but the caller, reverse proxy, AgentCore runtime, or network can still terminate
the connection before delivery.

The asynchronous API must let the payment request finish quickly without
breaking the existing SSE client. It must keep personalized reports private,
avoid charging twice on retries, survive ordinary AgentCore scale-to-zero
behavior, and provide a recovery path after an instance failure. This is a demo
agent, so the design deliberately reuses S3 instead of adding SQS or DynamoDB.

## Goals

- Return a durable job handle immediately after successful B402 settlement.
- Let wallets poll and download the completed report asynchronously.
- Preserve the existing `POST /x402/analyze` SSE behavior unchanged.
- Use S3 for durable job state and reports; add no database or message queue.
- Make repeated requests for the same payment idempotent across instances.
- Keep report access authenticated and automatically expire all job data.
- Continue counting a successful competition call at payment settlement time,
  independently of report completion.

## Non-goals

- Replacing the existing ERC-8183 asynchronous delivery flow.
- Migrating the free, approximately one-second `/x402/free` response to jobs.
- Building a general-purpose queue, scheduler, or job-search API.
- Providing exactly-once LLM execution under every infrastructure failure.
- Making personalized reports permanently public.

## Chosen Architecture

The agent adds a S3-backed `X402JobService`. The service runs report generation
as a tracked background task in the same AgentCore process and persists every
externally relevant state transition to S3. Its active tasks participate in the
existing `/ping` busy decision so normal scale-to-zero does not reap the
runtime while work is in progress.

If a process crashes or is redeployed, the wallet can call an authenticated
resume endpoint. A conditional S3 lease ensures only one process resumes the
job. This avoids new AWS services while providing recovery from the failure
that an in-memory-only design cannot handle.

The existing SSE endpoint remains available. Wallet integrations that need a
short payment request use the new async endpoint.

## HTTP API

### Create

```http
POST /x402/analyze/async
Content-Type: application/json
X-Payment: <base64 EIP-712/EIP-3009 proof>
```

The JSON request has the same analysis fields as the existing paid endpoint:
`symbols`, `analysis_type`, `portfolio`, and `risk_profile`. The handler applies
an explicit request-body size limit before JSON decoding.

When `X-Payment` is absent, the endpoint returns the existing `402` payment
challenge. Invalid proofs and failed settlement also return `402`. Successful
settlement returns:

```http
HTTP/1.1 202 Accepted
Location: /x402/jobs/x402_<id>
Retry-After: 10
```

```json
{
  "jobId": "x402_<id>",
  "jobToken": "<base64url token>",
  "status": "queued",
  "statusUrl": "/x402/jobs/x402_<id>",
  "expiresAt": 1785945600123
}
```

The successful response is idempotent: the same valid payment proof returns
the same `jobId` and `jobToken` and never invokes settlement twice.

### Query

```http
GET /x402/jobs/{jobId}
X-Job-Token: <job token>
```

- `queued` and `running` return `200` plus `Retry-After: 10`.
- `failed` returns `200` with a stable `errorCode` and `retryable`.
- `succeeded` returns `200` with a private S3 presigned download URL valid for
  exactly 1,800 seconds.
- An invalid token and a nonexistent job both return the same `404` response.
- An application-expired job returns `410`, even if asynchronous S3 Lifecycle
  deletion has not executed yet.

Example success:

```json
{
  "jobId": "x402_<id>",
  "status": "succeeded",
  "downloadUrl": "https://s3.example/...",
  "downloadUrlExpiresAt": 1785342600123,
  "expiresAt": 1785945600123
}
```

The bucket and report object remain private. The URL is a temporary bearer
credential, is not logged, and can be renewed by another authenticated query
while the job remains within its seven-day lifetime.

### Resume

```http
POST /x402/jobs/{jobId}/resume
X-Job-Token: <job token>
```

Resume is allowed only when:

- the job is `failed` with `retryable=true`; or
- the job is `running` and its heartbeat is more than two minutes stale.

The initial attempt plus at most two resumes gives a maximum of three report
executions. Resume never settles payment again and never reports a second
competition call. A normal `queued`, fresh `running`, or `succeeded` job returns
`409`. An exhausted or deterministic failure remains `failed` with
`retryable=false`.

## Identity, Authentication, and Idempotency

The initial request remains authenticated by the EIP-712/EIP-3009 payment
proof. Polling cannot reuse that proof as a bearer credential because its nonce
has already been consumed.

After cryptographic proof validation:

```text
paymentKey = SHA-256(chainId || canonicalAddress || canonicalNonce)
jobId      = "x402_" || first_128_bits(paymentKey)
jobToken   = base64url(HMAC-SHA256(X402_JOB_TOKEN_SECRET, paymentKey))
```

`jobId` is an identifier, not a secret. `job.json` stores the complete
`paymentKey` to detect the negligible truncated-ID collision case. It stores
only `SHA-256(jobToken)`, never the token itself. Token comparison is constant
time.

`X402_JOB_TOKEN_SECRET` must contain at least 32 random bytes and is supplied as
a runtime secret. It must remain available for at least the seven-day lifetime
of jobs created under it; a future secret-rotation feature can support current
and previous versions if rotation becomes necessary.

The payment verifier is split into:

- a pure semantic and cryptographic validation operation; and
- nonce consumption used by the legacy SSE flow.

The async flow uses S3 conditional creation as the cross-instance settlement
reservation. Only the request that creates the `settling` job record may call
the facilitator. Other requests revalidate the same signed proof, read the
existing job, and return its deterministic handle.

There is an unavoidable crash window between an external settlement and the
S3 state update. A stale `settling` record is reconciled by reading the U-token
EIP-3009 authorization state for the signed address and nonce:

- used authorization means the payment completed and the job advances to
  `queued`;
- unused, still-valid authorization may retry facilitator settlement;
- expired and unused authorization becomes a non-executable payment failure.

This prevents a timeout or process crash from either charging twice or losing a
payment that landed on-chain.

## S3 Layout and State

Only two private objects are needed per job:

```text
{X402_JOB_S3_PREFIX}/{jobId}/job.json
{X402_JOB_S3_PREFIX}/{jobId}/report.md
```

There is no separate payment index because `jobId` is derived from
`paymentKey`.

Representative `job.json`:

```json
{
  "version": 1,
  "jobId": "x402_<id>",
  "paymentKey": "<64 lowercase hex characters>",
  "paymentStatus": "settled",
  "settlementReference": "0x...",
  "address": "0x...",
  "status": "running",
  "request": {
    "symbols": ["AAPL"],
    "analysisType": "comprehensive",
    "portfolio": [],
    "riskProfile": {}
  },
  "jobTokenHash": "<64 lowercase hex characters>",
  "attempt": 1,
  "leaseOwner": "<runtime-random identifier>",
  "leaseExpiresAt": 1785340920123,
  "createdAt": 1785340800123,
  "updatedAt": 1785340830123,
  "expiresAt": 1785945600123,
  "errorCode": null,
  "retryable": null
}
```

The state machine is:

```text
settling -> queued -> running -> succeeded
                         \----> failed
```

Creation uses `If-None-Match: *`. Mutable state updates use `If-Match` with the
last observed ETag. Lease acquisition and resume are compare-and-swap updates,
so concurrent processes cannot both own the same execution attempt.

The worker updates `updatedAt` and extends its lease every 30 seconds. A
heartbeat older than two minutes is stale. The report object is written before
the final compare-and-swap to `succeeded`; a succeeded job therefore always has
a downloadable report.

Both prefixes expire through an S3 Lifecycle rule seven days after object
creation. The API also enforces `expiresAt` synchronously because Lifecycle
deletion timing is asynchronous.

## Background Execution

`X402JobService` consumes the existing `stream_work` async generator. Progress
events update heartbeats but are not persisted individually. The final
`report` event supplies Markdown for `report.md`; a missing report, generator
error, or timeout transitions the job to a stable failure code.

Every active task is retained in a task set. `is_busy()` is true while any task
is active. `main._ping_status()` returns `HEALTHY_BUSY` if either the existing
ERC-8183 executor or the x402 job service is busy.

Task failures are caught and persisted. They never become unhandled event-loop
exceptions. Cancellation during process shutdown leaves the last heartbeat in
S3, allowing the wallet to resume the job after the two-minute stale period.

## Components

### `x402_job_store.py`

- Loads and validates S3 configuration.
- Serializes and validates versioned job records.
- Implements conditional create, read, and ETag compare-and-swap.
- Stores the Markdown report with explicit content type and size limit.
- Generates 1,800-second `get_object` presigned URLs.
- Contains no HTTP, payment, or LLM logic.

### `x402_job_service.py`

- Derives payment keys, job IDs, and job tokens.
- Orchestrates settlement reservation, job creation, task execution, leases,
  heartbeats, terminal state, query, and resume.
- Owns the tracked task set and `is_busy()`.
- Depends on an injected store, settlement callable, authorization-state
  reader, competition reporter, clock, and `stream_work`.

### `x402_verify.py`

- Exposes pure verified-payment parsing without mutating replay state.
- Preserves the current `verify_payment_proof` contract for legacy SSE by
  composing pure validation with the existing in-memory nonce guard.

### `x402_handler.py`

- Preserves all existing routes.
- Adds the async create, query, and resume routes.
- Enforces HTTP body and identifier limits.
- Delegates job behavior to `X402JobService`.
- Maps domain outcomes to the documented HTTP responses.

### `main.py`

- Constructs one shared job service for both local combined-port and deployed
  dual-port modes.
- Includes x402 job activity in the ping busy state.

### `buyer-client/src/x402-async.ts`

- Demonstrates signing/payment, receipt persistence, polling, resume, renewed
  presigned URLs, and report download.
- Leaves the existing `buyer-client/src/x402.ts` SSE example unchanged.

## Configuration and Deployment

Required to enable async jobs:

```text
X402_JOB_S3_BUCKET=<private bucket>
X402_JOB_S3_PREFIX=x402-jobs
X402_JOB_TOKEN_SECRET=<at least 32 random bytes>
```

The prefix may be in the existing S3 bucket only if bucket policy and any
public distribution expose no objects under that prefix. Otherwise deployment
must use a separate private bucket.

The AgentCore role receives only the required object permissions for the
configured prefix. Deployment continues to use `bag deploy`; wallet material,
signing domains, and x402 allowed-host security policy are unchanged.

Rollout order:

1. Create or verify the private S3 destination and seven-day Lifecycle rule.
2. Grant the AgentCore role minimum object permissions.
3. Install configuration and token secret through runtime secrets.
4. Deploy with `bag deploy`.
5. Smoke-test free x402, legacy paid SSE, async create/query/download/resume.
6. Move the wallet integration to the async route.

Rollback stops creation of new async jobs but keeps query, resume, and download
available for at least seven days. The legacy SSE endpoint remains usable
throughout.

## Error Handling

Public responses use stable codes such as:

- `invalid_request`
- `payment_rejected`
- `payment_unavailable`
- `analysis_failed`
- `analysis_timeout`
- `attempts_exhausted`

Internal exceptions, S3 keys, payment proofs, portfolio/risk inputs, job tokens,
and presigned URLs are never logged. Invalid token and nonexistent job responses
are intentionally indistinguishable.

S3 unavailability before settlement fails closed and does not charge. S3
unavailability after a confirmed settlement returns a retryable service error
and relies on deterministic job recovery plus on-chain authorization-state
reconciliation.

## Testing

### Payment and idempotency

- Legacy SSE signature, nonce replay, and settlement tests remain green.
- Concurrent async creation for one proof settles once and returns one handle.
- Retrying the same proof returns identical job ID and token.
- A truncated job-ID collision is detected through the stored full payment key.
- A stale settling record reconciles both used and unused on-chain nonce states.

### Store and security

- Conditional create and ETag conflict behavior use a fake S3 client.
- Token plaintext never appears in serialized state.
- Invalid and missing tokens produce the same response.
- Reports are private and cannot be returned through the configured public base.
- Presigned URL generation uses `ExpiresIn=1800`.
- Application expiry returns `410` independently of Lifecycle timing.

### Execution and recovery

- Every allowed state transition is covered.
- Report write precedes the `succeeded` transition.
- Heartbeat refresh, stale detection, lease contention, and maximum attempts are
  deterministic under an injected clock.
- Concurrent resumes start only one worker.
- Active jobs affect busy status and release it on every terminal path.
- Cancellation leaves a recoverable stale job rather than an unhandled task.

### HTTP and client

- Async challenge, successful `202`, polling, success download, failure, resume,
  conflict, expiry, invalid token, malformed ID, and body-limit responses are
  exercised as ASGI tests.
- Existing paid SSE and free routes are regression-tested unchanged.
- The TypeScript client covers normal polling, interrupted polling, resume,
  download, and URL renewal.
- Full Python and buyer-client test suites run before deployment.

## Acceptance Criteria

- A successfully settled async request returns a job handle without waiting for
  report generation.
- The wallet can retrieve the report after a several-minute generation without
  maintaining the payment request connection.
- Repeated or concurrent submissions of one proof never cause a second payment
  or a second competition count.
- Existing SSE and free clients continue to work.
- A normal scale-to-zero does not interrupt active work, and an abnormal process
  loss can be resumed without another payment.
- Report objects are private, presigned URLs last 30 minutes, and all job data
  becomes inaccessible after seven days.
- No database, queue, or additional worker service is required.
