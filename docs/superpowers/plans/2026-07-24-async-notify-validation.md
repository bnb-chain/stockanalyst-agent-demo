# Async Notify Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move signed notification recovery and synchronous gateway DNS validation off the asyncio event loop while bounding lingering validation workers.

**Architecture:** Keep the existing cheap authorization preflight and chain reads unchanged. Run signature recovery plus gateway canonicalization as one pure synchronous helper in `asyncio.to_thread`, admitted through a four-slot semaphore whose permit remains held until the underlying worker actually exits, even if the request times out. Preserve all existing response codes and commit shared notification state only after successful validation.

**Tech Stack:** Python 3.10+, asyncio, `asyncio.to_thread`, `asyncio.Semaphore`, unittest `IsolatedAsyncioTestCase`, threading test primitives.

## Global Constraints

- No synchronous DNS or signature recovery may run on the `notify_funded` event-loop thread.
- Keep `preflight_notify_authorization` on the event loop.
- Allow at most four active or lingering notification validation workers per `SellerCore`.
- The existing `NOTIFY_PREVERIFY_TIMEOUT_SECONDS` deadline, default 30 seconds, includes waiting for a validation slot.
- A timed-out worker retains its slot until the underlying thread exits.
- A request that times out while waiting for capacity must not start a worker.
- Map `invalid_gateway_url` to `unsafe_gateway` exactly as before.
- Map validation timeout, scheduling failure, and unexpected worker failure to retryable `verification_unavailable`.
- Never expose gateway URL, token, signed context, signature, or worker exception details.
- Failed and timed-out validation must not write `_job_contexts`, mark jobs, spawn named delivery, or spawn a sweep.
- Keep the successful notification protocol, response, context binding, named delivery, and sweep behavior unchanged.
- Do not add a new runtime dependency or dedicated executor.

---

### Task 1: Add bounded asynchronous notification validation

**Files:**
- Modify: `stockanalyst/app/agent/seller_core.py:67-126`
- Modify: `stockanalyst/app/agent/seller_core.py:155-280`
- Test: `stockanalyst/app/agent/tests/test_seller_core_notify.py`

**Interfaces:**
- Produces: `_verify_and_validate_notify_context(...) -> JobContext`, a private synchronous pure helper.
- Produces: `SellerCore._run_notify_validation(...) -> JobContext`, a private async admission/lifetime wrapper.
- Produces: `SellerCore._notify_validation_slots`, an `asyncio.Semaphore` initialized to four permits.
- Consumes: existing `verify_notify_authorization`, `validate_gateway_url`, `NotifySecurityError`, `JobContext`, and `_PREVERIFY_TIMEOUT_SECONDS`.

- [ ] **Step 1: Write RED tests for event-loop responsiveness and worker placement**

Add tests to `NotifyFundedAuthorizationTests` that patch
`seller_core_module.validate_gateway_url` with a function that records
`threading.get_ident()`, signals a `threading.Event`, and waits for a release
event.

The first test must:

```python
async def test_slow_gateway_validation_does_not_block_event_loop(self) -> None:
    started = threading.Event()
    release = threading.Event()
    loop_thread = threading.get_ident()
    validator_threads: list[int] = []

    def blocking_validate(url: str) -> str:
        validator_threads.append(threading.get_ident())
        started.set()
        release.wait(timeout=2)
        return url

    with patch.object(
        seller_core_module,
        "validate_gateway_url",
        side_effect=blocking_validate,
    ):
        notification = asyncio.create_task(
            self.core.notify_funded(self._signed_request())
        )
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        heartbeat = asyncio.create_task(asyncio.sleep(0))
        await asyncio.wait_for(heartbeat, timeout=0.1)
        self.assertFalse(notification.done())
        release.set()
        result = await notification

    self.assertNotEqual(validator_threads, [loop_thread])
    self.assertEqual(result["status"], "accepted")
```

Add a second focused test patching
`seller_core_module.verify_notify_authorization` to record its thread identity
and delegate to the real function, proving signature recovery runs on the same
non-loop worker path.

- [ ] **Step 2: Run the two focused tests and verify RED**

Run:

```bash
cd stockanalyst/app/agent
PYTHONPATH=../../.. .venv/bin/python -m unittest \
  tests.test_seller_core_notify.NotifyFundedAuthorizationTests.test_slow_gateway_validation_does_not_block_event_loop \
  tests.test_seller_core_notify.NotifyFundedAuthorizationTests.test_signature_recovery_runs_off_event_loop
```

Expected: the heartbeat test times out or observes loop-thread validation, and
the signature-recovery test observes the event-loop thread.

- [ ] **Step 3: Write RED tests for timeout and side-effect safety**

Add a test with a blocking gateway validator and temporarily patch
`seller_core_module._PREVERIFY_TIMEOUT_SECONDS` to `0.05`. Assert the result is
exactly:

```python
{
    "status": "rejected",
    "job_id": JOB_ID,
    "reason": "verification_unavailable",
    "retryable": True,
}
```

Before releasing the worker, assert:

```python
self.assertEqual(self.core._job_contexts, {})
self.assertEqual(self.core.spawned_jobs, [])
self.assertNotIn(JOB_ID, self.core._handled)
self.assertNotIn(JOB_ID, self.core._contextless_started)
```

Patch `_spawn_sweep` with a mock or use `RecordingSellerCore.spawned_jobs` to
prove no delivery side effect occurs.

- [ ] **Step 4: Write RED tests for the four-slot lifetime bound**

Use a fresh `RecordingSellerCore`, five signed requests, and a blocking
`verify_notify_authorization` wrapper that tracks:

```python
active = 0
maximum_active = 0
started_calls = 0
lock = threading.Lock()
four_started = threading.Event()
release = threading.Event()
```

Start four notifications under a long timeout and wait for `four_started`.
Then patch the timeout to `0.05` and submit the fifth. Assert:

```python
self.assertEqual(fifth["reason"], "verification_unavailable")
self.assertEqual(started_calls, 4)
self.assertEqual(maximum_active, 4)
```

Keep the first four workers blocked for longer than the fifth request timeout,
then assert `started_calls` remains four. Release them, await all tasks, and
assert the semaphore eventually returns to four available permits.

This test proves both that capacity waiting is inside the deadline and that
caller timeout does not release a lingering worker permit.

- [ ] **Step 5: Run the new timeout/capacity tests and verify RED**

Run the four new test methods directly with unittest.

Expected: current code starts validation on the event loop, has no four-slot
admission control, or blocks the test event loop.

- [ ] **Step 6: Implement the pure synchronous validation helper**

Add near the module constants:

```python
_MAX_NOTIFY_VALIDATION_WORKERS = 4


def _verify_and_validate_notify_context(
    authorization: object,
    *,
    job_id: int,
    expected_client: str,
    chain_id: int,
    verifying_contract: str,
) -> JobContext:
    context = verify_notify_authorization(
        authorization,
        job_id=job_id,
        expected_client=expected_client,
        chain_id=chain_id,
        verifying_contract=verifying_contract,
    )
    if context.gateway_url is not None:
        context = replace(
            context,
            gateway_url=validate_gateway_url(context.gateway_url),
        )
    return context
```

This helper must not read or write `SellerCore` state.

- [ ] **Step 7: Implement semaphore initialization and lifetime-safe offload**

In `SellerCore.__init__` add:

```python
self._notify_validation_slots = asyncio.Semaphore(
    _MAX_NOTIFY_VALIDATION_WORKERS
)
```

Add a private async method:

```python
async def _run_notify_validation(
    self,
    authorization: object,
    *,
    job_id: int,
    expected_client: str,
    chain_id: int,
    verifying_contract: str,
) -> JobContext:
    await self._notify_validation_slots.acquire()
    release_in_finally = True
    worker: asyncio.Task[JobContext] | None = None
    try:
        worker = asyncio.create_task(
            asyncio.to_thread(
                _verify_and_validate_notify_context,
                authorization,
                job_id=job_id,
                expected_client=expected_client,
                chain_id=chain_id,
                verifying_contract=verifying_contract,
            )
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            release_in_finally = False

            def release_when_finished(done: asyncio.Task[JobContext]) -> None:
                self._notify_validation_slots.release()
                try:
                    done.exception()
                except asyncio.CancelledError:
                    pass

            worker.add_done_callback(release_when_finished)
            raise
    finally:
        if release_in_finally:
            self._notify_validation_slots.release()
```

There must be no `await` between successful semaphore acquisition and worker
task creation. The completion callback both releases the permit and retrieves
any late exception so asyncio does not emit “Task exception was never
retrieved”.

- [ ] **Step 8: Replace the event-loop validation block**

Replace the direct calls at the current `seller_core.py:241-253` with:

```python
try:
    context = await asyncio.wait_for(
        self._run_notify_validation(
            authorization,
            job_id=job_id,
            expected_client=target.client,
            chain_id=target.chain_id,
            verifying_contract=target.verifying_contract,
        ),
        timeout=_PREVERIFY_TIMEOUT_SECONDS,
    )
except NotifySecurityError as error:
    reason = "unsafe_gateway" if error.code == "invalid_gateway_url" else error.code
    logger.warning("job %s: notification rejected — %s", job_id, reason)
    return {"status": "rejected", "job_id": job_id, "reason": reason}
except Exception:  # includes timeout, scheduling, and unexpected worker failure
    logger.warning("job %s: notification validation unavailable", job_id)
    return {
        "status": "rejected",
        "job_id": job_id,
        "reason": "verification_unavailable",
        "retryable": True,
    }
```

Do not change the following await-free context compare-and-set and spawn block.

- [ ] **Step 9: Run focused tests GREEN**

Run:

```bash
cd stockanalyst/app/agent
PYTHONPATH=../../.. .venv/bin/python -m unittest \
  tests.test_seller_core_notify.NotifyFundedAuthorizationTests
```

Expected: all notification authorization tests pass without pending-task or
unretrieved-exception warnings.

- [ ] **Step 10: Add error-mapping and successful-path regression assertions**

Confirm or add tests proving:

- a worker-raised `NotifySecurityError("invalid_gateway_url")` returns
  `unsafe_gateway`;
- another `NotifySecurityError` retains its existing code;
- an unexpected worker exception returns retryable
  `verification_unavailable` without its message;
- a valid notification stores the same `JobContext`, spawns
  `(JOB_ID, True)`, and triggers one sweep.

Run these tests first against any missing behavior to observe RED, then make
only the minimal production correction needed and rerun GREEN.

- [ ] **Step 11: Run full regression checks**

Run:

```bash
cd stockanalyst/app/agent
PYTHONPATH=../../.. .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
cd ../../../buyer-client
npm test
cd ..
git diff --check
git status --short
```

Expected: the Python and buyer suites pass, TypeScript compilation succeeds,
`git diff --check` is silent, and only the intended Task 1 files are
uncommitted.

- [ ] **Step 12: Commit**

```bash
git add stockanalyst/app/agent/seller_core.py \
  stockanalyst/app/agent/tests/test_seller_core_notify.py
git commit -m "fix: offload notify gateway validation"
```

---

### Task 2: Final concurrency and security review

**Files:**
- Review: `stockanalyst/app/agent/seller_core.py`
- Review: `stockanalyst/app/agent/tests/test_seller_core_notify.py`

**Interfaces:**
- Consumes: Task 1's `_verify_and_validate_notify_context`,
  `_run_notify_validation`, and `_notify_validation_slots`.
- Produces: no new interface; only test-driven corrections if review finds a
  violation.

- [ ] **Step 1: Audit cancellation paths**

Trace all paths after semaphore acquisition and prove exactly one release:

- normal return;
- `NotifySecurityError`;
- unexpected worker exception;
- cancellation before worker completion;
- timeout while DNS remains blocked; and
- completion racing with timeout.

If any path can leak or double-release a permit, add a focused failing test
before correcting it.

- [ ] **Step 2: Audit shared state and error secrecy**

Confirm worker-thread code cannot mutate `_job_contexts`, `_handled`,
`_contextless_started`, `_inflight`, `_tasks`, or `_sweep_active`. Confirm
timeout and unexpected exception responses contain no exception message or
signed context data.

- [ ] **Step 3: Re-run final verification**

Run the focused authorization suite, full Python suite, buyer suite,
`git diff --check`, and `git status --short`.

Expected: all tests pass with clean output and no uncommitted implementation
changes.
