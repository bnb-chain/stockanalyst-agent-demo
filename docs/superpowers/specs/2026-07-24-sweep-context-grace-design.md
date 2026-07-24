# Sweep Context Grace Design

## Objective

Prevent an opportunistic funded-job sweep from consuming a job without its
buyer-authorized gateway, portfolio, and risk context while preserving a safe
context-free fallback for jobs that explicitly permit it.

## Current Problem

Any transport-authenticated caller can trigger a bare `notify_funded`, and every
accepted named notification also starts a provider-wide sweep. A sweep may
reach `_do_work_and_submit` before the actual buyer's signed notification
finishes verification. It then snapshots no context, marks
`_contextless_started`, and causes the later valid notification to be rejected
as `delivery_already_started`.

This is a cross-tenant delivery downgrade. It can remove personalization and
the buyer relay, potentially publishing an unreachable default-storage URL.

## Signed Context Requirement

Use the existing signed ERC-8183 job description rather than an unsigned local
flag. The buyer adds this exact term to negotiation:

```text
success_criteria = "uomp_notify_context_required_v1"
```

The SDK echoes `success_criteria` into the seller-signed negotiation response.
The buyer must copy it into the on-chain `JobDescription.terms`. Existing
provider-signature verification therefore covers the marker.

The seller treats a structured job as context-required only when its parsed
`terms.success_criteria` exactly equals that sentinel. Unknown, malformed,
legacy, and unstructured descriptions do not accidentally acquire the
requirement and retain the optional context-free fallback.

## Sweep State Machine

### Per-job state

`SellerCore` maintains:

- a monotonic grace deadline for each swept job;
- an asyncio event used to wake a waiting sweep when named context arrives; and
- the existing `_inflight`, `_handled`, `_job_contexts`, and
  `_contextless_started` state.

The grace interval is exactly 60 seconds. The deadline is created once from
`loop.time()` when a verified sweep worker first prepares to consume a job.
Further bare notifications, named notifications, and repeated sweeps may not
shorten or reset it.

### Sweep worker

After `verify_signed_job` succeeds, the sweep worker:

1. reads and parses the signed on-chain job spec;
2. determines whether the exact context-required sentinel is present;
3. returns immediately to the existing delivery path if authorized context is
   already installed;
4. otherwise waits on the per-job event until the fixed grace deadline; and
5. re-reads `_job_contexts` after wakeup or timeout.

If context arrived, the same in-flight sweep worker proceeds with it. No second
worker is spawned.

If grace expires without context:

- a context-required job returns a non-terminal, retryable result without
  starting the LLM or submission;
- an optional/legacy job proceeds through the existing context-free fallback.

The final context snapshot and `_contextless_started` transition remain
await-free. A named notification that commits before that transition wins; a
notification arriving after an optional job has irreversibly started
context-free work is still rejected, preventing mixed prompts and duplicate
submissions.

### Named notification

After full chain, signature, and gateway validation, the existing atomic commit
section installs `_job_contexts[job_id]`. In the same await-free section it sets
the job's context event, if present, before calling `_spawn_job`.

If a grace-waiting sweep already owns `_inflight`, `_spawn_job` remains deduped;
the existing worker wakes and consumes the installed context.

## State Cleanup

Terminal delivery or permanent skip removes:

- saved context;
- grace deadline;
- context event; and
- existing inflight/contextless state.

Transient failures after work begins preserve the expired deadline so a retry
does not reopen or extend grace. A context-required no-context result remains
transient and may be reconsidered by a later sweep or named notification, but
can never deliver without context.

No completed, handled, or submitted job may be revived by a late notification.

## Alternatives Considered

### Let authorized context overwrite active contextless work

Rejected. `_do_work_and_submit` snapshots gateway and prompt context before
awaiting the LLM. Late replacement could produce an old prompt delivered to a
new gateway, or race an uncancellable submission thread and double-submit.

### Disable bare sweeps only

Insufficient. Every accepted named notification also triggers the provider-wide
sweep, and a trusted periodic poller creates the same natural race.

### Apply only a grace delay

This reduces the race but does not guarantee correct delivery if chain,
signature, or DNS validation exceeds the grace period. The signed requirement
is needed to guarantee that current personalized jobs never fall back to a
contextless delivery.

### Require context for every structured job

Rejected for compatibility. Existing structured jobs may intentionally rely on
the sweep fallback. Only the exact seller-signed sentinel changes the behavior.

## Error and Availability Behavior

Waiting for grace is background work and does not delay the A2A response that
triggered the sweep. A context-required job without context produces no LLM
call, storage upload, or on-chain submission.

Context wait cancellation, delivery timeout, and transient chain failures use
the existing retry semantics. No gateway token or portfolio data is logged.

## Test Strategy

Use test-driven development. Tests must prove:

1. a third-party bare sweep cannot contextlessly deliver a marked victim job;
2. a named notification during grace installs context, wakes the existing
   worker, and delivers once through the buyer gateway;
3. repeated sweeps cannot shorten, reset, or duplicate the grace wait;
4. a marked job without context performs no LLM, storage, or submit action;
5. an unmarked legacy job retains context-free delivery after exactly 60
   seconds;
6. a notification that wins immediately before the contextless transition is
   consumed;
7. a notification after an optional job has begun contextless work remains
   rejected;
8. transient and terminal cleanup preserves the specified deadline/event
   lifecycle;
9. the buyer negotiation request and on-chain job description contain the
   exact signed sentinel; and
10. provider-signature verification remains valid for the changed description.

Use an injected or patched grace duration in unit tests so the suite does not
sleep for 60 real seconds. Run focused seller and buyer tests, full Python and
buyer suites, TypeScript compilation, and diff checks.

## Compatibility

- Current buyer-created personalized jobs become explicitly context-required.
- Legacy and third-party jobs without the sentinel keep context-free sweep
  delivery after the grace period.
- Named notification payloads, EIP-712 authorization, gateway policy, report
  generation, and settlement APIs do not change.
- Normal named delivery remains immediate; only sweep-owned jobs may wait.

## Acceptance Criteria

1. A context-required job can never call the LLM or submit without authorized
   context.
2. No external sweep trigger can shorten the 60-second grace.
3. Context arriving during grace is used by the already in-flight worker.
4. Optional jobs retain a single context-free fallback after grace.
5. No race can mix old prompt context with a newly installed gateway or produce
   duplicate submission.
6. All focused, regression, signature, and concurrency tests pass.
