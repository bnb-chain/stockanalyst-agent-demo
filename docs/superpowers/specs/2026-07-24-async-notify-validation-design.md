# Async Notify Validation Design

## Objective

Prevent `notify_funded` authorization and gateway validation from blocking the
agent's asyncio event loop while preserving the existing authentication,
gateway, context-binding, and delivery behavior.

## Current Problem

`SellerCore.notify_funded` correctly moves chain reads into worker threads, but
then calls `verify_notify_authorization` and `validate_gateway_url` directly on
the event loop. Gateway validation synchronously calls
`socket.getaddrinfo`. A slow resolver can therefore stop all work on that event
loop until DNS returns.

The caller still needs a funded job and a valid job-client signature, so this is
an availability issue rather than an authorization bypass.

## Design

### Synchronous validation unit

Add one private synchronous helper that:

1. verifies and recovers the signed notification authorization;
2. parses the signed context;
3. validates and canonicalizes the optional gateway URL; and
4. returns the final immutable `JobContext`.

The helper performs no shared-state mutation. It may safely execute on a worker
thread because `_job_contexts`, replay state, and task creation remain on the
event loop.

The existing cheap `preflight_notify_authorization` remains on the event loop.
It bounds the signed context and rejects malformed requests before they consume
a worker slot.

### Bounded thread offload

Each `SellerCore` owns an asyncio semaphore allowing at most four concurrent
authorization/gateway validation workers.

The permit must cover the worker's real lifetime, not only the caller's wait.
If the request times out while `socket.getaddrinfo` is still running, the permit
is released only by the worker-future completion callback. This prevents a
sequence of timed-out requests from starting an unbounded number of lingering
DNS threads.

Waiting for a permit and running the validation share the existing
`NOTIFY_PREVERIFY_TIMEOUT_SECONDS` deadline, whose default is 30 seconds.
Requests that time out before acquiring a permit do not start worker work.

The implementation continues to use the process default executor through
`asyncio.to_thread`; the four-worker admission limit leaves capacity for the
existing chain and background operations that also use the default executor.

### Error handling

An ordinary `NotifySecurityError` from the worker retains the current response
mapping:

- `invalid_gateway_url` becomes `unsafe_gateway`;
- all other security codes remain unchanged.

A timeout, worker scheduling failure, or unexpected worker exception returns:

```json
{
  "status": "rejected",
  "reason": "verification_unavailable",
  "retryable": true
}
```

No exception details, gateway URL, token, signature, or signed context are
logged or returned.

### State and delivery invariants

No failure or timeout may:

- write `_job_contexts`;
- mark the job handled or contextless;
- spawn the named delivery;
- spawn a sweep; or
- consume the notification context.

The existing atomic, await-free context commit and job-spawn section remains
unchanged and still runs only on the event loop after successful validation.

## Alternatives Considered

### Bare `asyncio.to_thread` with `wait_for`

This keeps the event loop responsive but releases no explicit capacity control.
Timed-out DNS calls continue in the background, so repeated requests could
occupy the default executor. Rejected.

### `loop.getaddrinfo`

This exposes an async interface but normally delegates to the event loop's
executor. Cancellation still cannot stop a resolver already running, so it
requires the same admission and lifetime accounting. It would also split the
currently cohesive gateway security validation. Not selected.

### Dedicated executor

A dedicated four-thread executor provides stronger isolation but adds lifecycle
and shutdown management to every `SellerCore`. The semaphore-held `to_thread`
design already caps this workload while retaining capacity in the larger
default pool. A dedicated executor can be introduced later if resolver behavior
shows isolation is necessary.

## Test Strategy

Use test-driven development.

Tests must prove:

1. a blocking gateway resolver does not stop an event-loop heartbeat;
2. authorization and gateway validation execute off the event-loop thread;
3. a timeout returns retryable `verification_unavailable`;
4. timeout and validation failures do not save context or spawn work;
5. no more than four validation workers run concurrently;
6. a timed-out but still-running worker retains its permit until it exits;
7. a request timing out while waiting for capacity never starts its validator;
8. existing `NotifySecurityError` response mappings are preserved; and
9. a valid notification still stores the same context and starts the same
   delivery and sweep behavior.

Run the focused seller-core notification tests, the full Python suite, buyer
tests, compilation checks, and `git diff --check`.

## Compatibility

There are no protocol, payload, signature, gateway-policy, or successful
response changes. The only observable change is that slow or unavailable
authorization/DNS validation no longer blocks unrelated asyncio work and may
return a bounded retryable failure.

## Acceptance Criteria

1. No synchronous DNS or signature recovery runs on the asyncio event-loop
   thread in `notify_funded`.
2. At most four notification validation workers are active or lingering.
3. The 30-second preverification deadline includes capacity waiting.
4. Worker permits survive caller timeout until the underlying thread completes.
5. Failed or timed-out validation has no job-context or delivery side effects.
6. All focused and regression tests pass.
