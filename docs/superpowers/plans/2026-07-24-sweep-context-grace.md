# Sweep Context Grace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent a sweep-triggered worker from delivering a personalized funded job without its buyer-authorized gateway and portfolio context.

**Architecture:** The buyer places the exact `uomp_notify_context_required_v1` sentinel in the negotiated terms and copies the seller-signed value into the on-chain job description. A verified sweep worker reads that signed spec, waits on a per-job `asyncio.Event` until a first-write monotonic 60-second deadline, and either consumes arriving authorized context, safely falls back for an unmarked job, or leaves a marked job retryable without invoking work or submission.

**Tech Stack:** TypeScript 5, Node.js test runner, Python 3 asyncio, `unittest.IsolatedAsyncioTestCase`, ERC-8183 negotiation/job-description schema.

## Global Constraints

- The context-required sentinel is exactly `uomp_notify_context_required_v1`.
- The sweep grace interval is exactly 60 seconds in production.
- Only an exact `terms.success_criteria` match makes a structured job context-required; unknown, malformed, legacy, and unstructured descriptions remain optional.
- Named notifications remain immediate after their existing chain, wallet-signature, and gateway checks.
- A grace deadline is created once from `asyncio.get_running_loop().time()` and repeated sweeps or notifications may not shorten or reset it.
- The named-notify context commit and event signal occur in one await-free section.
- The final context snapshot and `_contextless_started` transition occur in one await-free section.
- A context-required job without authorized context must not call the LLM, storage, or on-chain submission.
- Optional jobs retain one context-free fallback after grace; a later named notification is rejected once that fallback has irreversibly started.
- Terminal outcomes remove context, deadline, event, inflight, and contextless state; transient outcomes preserve the deadline/event and authorized context.
- Existing notify payloads, EIP-712 authorization, gateway policy, report generation, and settlement APIs do not change.

---

### Task 1: Carry the signed context requirement from negotiation to chain

**Files:**
- Create: `buyer-client/src/negotiate.test.ts`
- Modify: `buyer-client/src/negotiate.ts:17-27,103-123,168-177`
- Modify: `stockanalyst/app/agent/agent_card.py:32-45`
- Test: `stockanalyst/app/agent/tests/test_agent_card.py`

**Interfaces:**
- Consumes: the existing `negotiate(endpoint, task, deliverables, quality)` and `buildJobDescription(envelope)` APIs.
- Produces: exported `NOTIFY_CONTEXT_REQUIRED = "uomp_notify_context_required_v1"` and request/response term types with optional `success_criteria?: string`; the function signatures remain unchanged.

- [ ] **Step 1: Write failing buyer tests for the negotiated and on-chain marker**

Create `buyer-client/src/negotiate.test.ts` with a fetch stub that captures the negotiation request and returns a representative accepted, seller-signed envelope:

```ts
import assert from "node:assert/strict";
import test from "node:test";
import {
  getBytes,
  keccak256,
  toUtf8Bytes,
  verifyMessage,
  Wallet,
} from "ethers";
import {
  buildJobDescription,
  negotiate,
  NOTIFY_CONTEXT_REQUIRED,
  type NegotiationEnvelope,
} from "./negotiate.js";

test("requests the exact notify-context requirement", async () => {
  let requestBody = "";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (_input, init) => {
    requestBody = String(init?.body);
    return new Response(JSON.stringify({
      result: {
        parts: [{
          data: {
            request: {
              task_description: "analyse portfolio",
              terms: {
                deliverables: "report",
                quality_standards: "cited",
                success_criteria: NOTIFY_CONTEXT_REQUIRED,
              },
            },
            response: {
              accepted: true,
              terms: {
                price: "1",
                currency: "USDT",
                deliverables: "report",
                quality_standards: "cited",
                success_criteria: NOTIFY_CONTEXT_REQUIRED,
              },
              provider_sig: "0xsigned",
              negotiation_hash: "0xhash",
            },
          },
        }],
      },
    }));
  };

  try {
    await negotiate("https://seller.example/a2a", "analyse portfolio", "report", "cited");
  } finally {
    globalThis.fetch = originalFetch;
  }

  const payload = JSON.parse(requestBody);
  assert.equal(
    payload.params.message.parts[0].data.terms.success_criteria,
    NOTIFY_CONTEXT_REQUIRED,
  );
});

test("copies the seller-signed requirement into the on-chain description", () => {
  const envelope = {
    request: {
      task_description: "analyse portfolio",
      terms: {
        deliverables: "report",
        quality_standards: "cited",
        success_criteria: NOTIFY_CONTEXT_REQUIRED,
      },
    },
    response: {
      accepted: true,
      terms: {
        price: "1",
        currency: "USDT",
        deliverables: "report",
        quality_standards: "cited",
        success_criteria: NOTIFY_CONTEXT_REQUIRED,
      },
      negotiated_at: 1,
      negotiation_hash: "0xhash",
      provider_sig: "0xsigned",
    },
  } satisfies NegotiationEnvelope;

  const description = JSON.parse(buildJobDescription(envelope));
  assert.equal(description.terms.success_criteria, NOTIFY_CONTEXT_REQUIRED);
  assert.equal(description.negotiation_hash, "0xhash");
  assert.equal(description.provider_sig, "0xsigned");
});

function canonicalize(value: unknown): unknown {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, child]) => [key, canonicalize(child)]),
  );
}

test("preserves a verifiable provider signature over the marked description", async () => {
  const provider = Wallet.createRandom();
  const signedContent = {
    version: 1,
    negotiated_at: 1,
    task: "analyse portfolio",
    terms: {
      deliverables: "report",
      quality_standards: "cited",
      success_criteria: NOTIFY_CONTEXT_REQUIRED,
    },
    price: "1",
    currency: "0x1111111111111111111111111111111111111111",
    chain_id: 97,
    verifying_contract: "0x2222222222222222222222222222222222222222",
  };
  const negotiationHash = keccak256(
    toUtf8Bytes(JSON.stringify(canonicalize(signedContent))),
  );
  const providerSig = await provider.signMessage(getBytes(negotiationHash));
  const envelope = {
    request: {
      task_description: signedContent.task,
      terms: signedContent.terms,
    },
    response: {
      accepted: true,
      terms: {
        ...signedContent.terms,
        price: signedContent.price,
        currency: signedContent.currency,
      },
      negotiated_at: signedContent.negotiated_at,
    },
    negotiation_hash: negotiationHash,
    provider_sig: providerSig,
    chain_id: signedContent.chain_id,
    verifying_contract: signedContent.verifying_contract,
  } satisfies NegotiationEnvelope;

  const description = JSON.parse(buildJobDescription(envelope));
  const { negotiation_hash, provider_sig, ...rehydratedContent } = description;
  const rebuiltHash = keccak256(
    toUtf8Bytes(JSON.stringify(canonicalize(rehydratedContent))),
  );
  assert.equal(rebuiltHash, negotiation_hash);
  assert.equal(
    verifyMessage(getBytes(negotiation_hash), provider_sig),
    provider.address,
  );
});
```

- [ ] **Step 2: Run the buyer tests and verify the marker tests fail**

Run:

```bash
cd buyer-client
npm test -- --test-name-pattern="notify-context requirement|seller-signed requirement"
```

Expected: TypeScript compilation fails because `NOTIFY_CONTEXT_REQUIRED` and
the `success_criteria` request property do not exist, or the assertions fail
because the marker is absent. The signature test also fails until
`buildJobDescription` preserves the marked, signed term exactly.

- [ ] **Step 3: Add the marker without changing the public function signatures**

In `buyer-client/src/negotiate.ts`, define the constant, extend the term types, add it to the request, and copy only the seller-returned value into the description:

```ts
export const NOTIFY_CONTEXT_REQUIRED = "uomp_notify_context_required_v1";

export interface NegotiationEnvelope {
  request: {
    task_description: string;
    terms: {
      deliverables: string;
      quality_standards: string;
      success_criteria?: string;
    };
  };
  response: {
    accepted: boolean;
    terms: {
      price: string;
      currency: string;
      deliverables?: string;
      quality_standards?: string;
      success_criteria?: string;
      [key: string]: unknown;
    };
    negotiated_at?: number;
    quote_expires_at?: number;
    estimated_completion_seconds?: number;
    negotiation_hash?: string;
    provider_sig?: string;
    reason?: string;
  };
  negotiated_at?: number;
  negotiation_hash?: string;
  provider_sig?: string;
  chain_id?: number;
  verifying_contract?: string;
}
```

Use the constant in `negotiate`:

```ts
terms: {
  deliverables,
  quality_standards: quality,
  success_criteria: NOTIFY_CONTEXT_REQUIRED,
},
```

Copy the signed response term in `buildJobDescription`; do not synthesize the sentinel here because the on-chain content must remain the seller-signed response:

```ts
const terms: Record<string, unknown> = {
  deliverables: sanitize(responseTerms.deliverables ?? ""),
  quality_standards: sanitize(responseTerms.quality_standards ?? ""),
};
if (responseTerms.success_criteria != null) {
  terms["success_criteria"] = sanitize(responseTerms.success_criteria);
}
```

- [ ] **Step 4: Update the advertised negotiation contract and its test**

Add this assertion to `test_negotiate_handoff_requires_signed_notify_helper` in `stockanalyst/app/agent/tests/test_agent_card.py`:

```python
self.assertIn("uomp_notify_context_required_v1", description)
```

Update `_NEGOTIATE.description` in `stockanalyst/app/agent/agent_card.py` so its request example and handoff state:

```python
'"terms": {"deliverables": "...", "quality_standards": "...", '
'"success_criteria": "uomp_notify_context_required_v1"}} (all three terms '
"keys are REQUIRED) and receive a "
"wallet-signed price quote. The exact success_criteria value must be copied "
"from the signed response into the on-chain job description before funding. "
```

- [ ] **Step 5: Run focused buyer and agent-card tests**

Run:

```bash
cd buyer-client
npm test
cd ../stockanalyst/app/agent
PYTHONPATH=../../.. .venv/bin/python -m unittest tests.test_agent_card -v
```

Expected: all buyer tests and all agent-card tests pass. The buyer test proves the signed response term—not a locally invented replacement—is anchored on-chain with the unchanged hash and provider signature.

- [ ] **Step 6: Commit the signed marker propagation**

```bash
git add buyer-client/src/negotiate.ts buyer-client/src/negotiate.test.ts stockanalyst/app/agent/agent_card.py stockanalyst/app/agent/tests/test_agent_card.py
git commit -m "fix: sign context requirement into buyer jobs"
```

---

### Task 2: Add the verified sweep grace and same-worker wakeup

**Files:**
- Modify: `stockanalyst/app/agent/seller_core.py:75-88,135-151,332-355,393-475`
- Modify: `stockanalyst/app/agent/tests/test_seller_core_notify.py:809-888`

**Interfaces:**
- Consumes: `signing.verify_signed_job(job_id)`, `signing.job_spec(job_id)`, `_job_contexts`, `_inflight`, and the exact Task 1 sentinel.
- Produces: `_CONTEXT_REQUIRED_CRITERION: str`, `_SWEEP_CONTEXT_GRACE_SECONDS: float`, `_context_deadlines: dict[int, float]`, `_context_events: dict[int, asyncio.Event]`, `_requires_notify_context(spec: object) -> bool`, and `async _await_sweep_context(job_id: int, *, required: bool) -> bool`.

- [ ] **Step 1: Replace the old race test with failing grace/wakeup tests**

In `stockanalyst/app/agent/tests/test_seller_core_notify.py`, keep the existing “named context before sweep work is used” coverage, replace `test_named_context_is_rejected_after_context_free_work_starts`, and add tests with a patched short grace:

```python
def _job_spec(*, required: bool):
    criteria = (
        "uomp_notify_context_required_v1"
        if required
        else "legacy_optional_delivery"
    )
    return SimpleNamespace(
        task="analyse portfolio",
        terms={"success_criteria": criteria},
    )

async def test_named_notify_during_grace_wakes_same_sweep_worker(self) -> None:
    run_work = AsyncMock(return_value="report")
    core = SellerCore(run_work=run_work, generator="test")
    core._sweep = AsyncMock(return_value=None)
    submitted = SimpleNamespace(submit_tx="0xtx", deliverable_url="https://result")

    with (
        patch.object(seller_core_module, "_SWEEP_CONTEXT_GRACE_SECONDS", 1.0),
        patch.object(seller_core_module.signing, "job_spec", return_value=_job_spec(required=True)),
        patch.object(seller_core_module.signing, "submit_result", return_value=submitted) as submit,
    ):
        core._spawn_job(JOB_ID, verified=False)
        while JOB_ID not in core._context_events:
            await asyncio.sleep(0)
        original_task_count = len(core._tasks)
        result = await core.notify_funded(self._signed_request())
        await self._drain_tasks(core)

    self.assertEqual(result["status"], "accepted")
    self.assertEqual(original_task_count, 1)
    run_work.assert_awaited_once()
    submit.assert_called_once()
    self.assertIsNotNone(submit.call_args.kwargs["gateway_url"])

async def test_marked_sweep_without_context_never_starts_work(self) -> None:
    run_work = AsyncMock(return_value="must not run")
    core = SellerCore(run_work=run_work, generator="test")

    async def sweep_victim() -> None:
        core._spawn_job(JOB_ID, verified=False)

    core._sweep = AsyncMock(side_effect=sweep_victim)

    with (
        patch.object(seller_core_module, "_SWEEP_CONTEXT_GRACE_SECONDS", 0.001),
        patch.object(seller_core_module.signing, "job_spec", return_value=_job_spec(required=True)),
        patch.object(seller_core_module.signing, "submit_result") as submit,
    ):
        response = await core.notify_funded({})
        await self._drain_tasks(core)

    self.assertEqual(response["status"], "accepted")
    run_work.assert_not_awaited()
    submit.assert_not_called()
    self.assertNotIn(JOB_ID, core._contextless_started)
    self.assertNotIn(JOB_ID, core._inflight)
    self.assertNotIn(JOB_ID, core._handled)
```

Also add a direct exact-match test:

```python
def test_only_exact_signed_marker_requires_context(self) -> None:
    required = SimpleNamespace(
        terms={"success_criteria": "uomp_notify_context_required_v1"}
    )
    wrong = SimpleNamespace(
        terms={"success_criteria": "UOMP_NOTIFY_CONTEXT_REQUIRED_V1"}
    )
    malformed = SimpleNamespace(terms="not-a-dict")

    self.assertIs(seller_core_module._requires_notify_context(required), True)
    self.assertIs(seller_core_module._requires_notify_context(wrong), False)
    self.assertIs(seller_core_module._requires_notify_context(malformed), False)
    self.assertIs(seller_core_module._requires_notify_context(None), False)
```

- [ ] **Step 2: Run the focused tests and verify the new behavior fails**

Run:

```bash
cd stockanalyst/app/agent
PYTHONPATH=../../.. .venv/bin/python -m unittest \
  tests.test_seller_core_notify.NotifyFundedAuthorizationTests.test_named_notify_during_grace_wakes_same_sweep_worker \
  tests.test_seller_core_notify.NotifyFundedAuthorizationTests.test_marked_sweep_without_context_never_starts_work \
  tests.test_seller_core_notify.NotifyFundedAuthorizationTests.test_only_exact_signed_marker_requires_context -v
```

Expected: failures because the constants, event/deadline state, and `_requires_notify_context` do not exist; current marked sweeps start context-free work.

- [ ] **Step 3: Add exact marker parsing and per-job grace state**

In `stockanalyst/app/agent/seller_core.py`, add:

```python
_CONTEXT_REQUIRED_CRITERION = "uomp_notify_context_required_v1"
_SWEEP_CONTEXT_GRACE_SECONDS = 60.0


def _requires_notify_context(spec: object) -> bool:
    terms = getattr(spec, "terms", None)
    return (
        isinstance(terms, dict)
        and terms.get("success_criteria") == _CONTEXT_REQUIRED_CRITERION
    )
```

Initialize the state in `SellerCore.__init__`:

```python
self._context_deadlines: dict[int, float] = {}
self._context_events: dict[int, asyncio.Event] = {}
```

Add the wait helper. Its state creation, final read, and optional fallback marker are await-free:

```python
async def _await_sweep_context(self, job_id: int, *, required: bool) -> bool:
    if job_id in self._job_contexts:
        return True

    loop = asyncio.get_running_loop()
    deadline = self._context_deadlines.setdefault(
        job_id,
        loop.time() + _SWEEP_CONTEXT_GRACE_SECONDS,
    )
    event = self._context_events.setdefault(job_id, asyncio.Event())
    remaining = max(0.0, deadline - loop.time())
    if remaining > 0 and not event.is_set():
        try:
            await asyncio.wait_for(event.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            pass

    if job_id in self._job_contexts:
        return True
    if required:
        return False
    self._contextless_started.add(job_id)
    return True
```

- [ ] **Step 4: Signal the waiter atomically from an authorized named notification**

In the existing no-await commit section of `notify_funded`, immediately after the context compare-and-set succeeds and before `_spawn_job`, add:

```python
context_event = self._context_events.get(job_id)
if context_event is not None:
    context_event.set()
self._spawn_job(job_id, verified=True)
```

Do not create/reset a deadline on the named path. If the sweep owns `_inflight`, the existing `_spawn_job` dedupe keeps exactly one worker while the event wakes it.

- [ ] **Step 5: Gate only verified sweep workers before work**

Change `_fulfill_job` so it reads the signed spec after verification and gates the existing delivery path:

```python
async def _fulfill_job(self, job_id: int) -> dict[str, Any]:
    ok, reason, permanent = await asyncio.to_thread(
        signing.verify_signed_job,
        job_id,
    )
    if not ok:
        return {
            "ok": False,
            "job_id": job_id,
            "skip": permanent,
            "reason": reason,
        }

    spec = await asyncio.to_thread(signing.job_spec, job_id)
    ready = await self._await_sweep_context(
        job_id,
        required=_requires_notify_context(spec),
    )
    if not ready:
        return {
            "ok": False,
            "job_id": job_id,
            "skip": False,
            "reason": "notify_context_required",
        }
    return await self._do_work_and_submit(job_id, spec=spec)
```

Update `_do_work_and_submit` to accept the already-read sweep spec while preserving named-delivery behavior:

```python
_JOB_SPEC_UNSET = object()

async def _do_work_and_submit(
    self,
    job_id: int,
    *,
    spec: object = _JOB_SPEC_UNSET,
) -> dict[str, Any]:
    context = self._job_contexts.get(job_id)
    if context is None:
        self._contextless_started.add(job_id)
    gateway_url = context.gateway_url if context is not None else None
    gateway_token = context.gateway_token if context is not None else None
    portfolio = context.portfolio_for_prompt() if context is not None else []
    risk_profile = (
        context.risk_profile_for_prompt() if context is not None else None
    )

    if spec is _JOB_SPEC_UNSET:
        spec = await asyncio.to_thread(signing.job_spec, job_id)
    if spec is not None:
        task = json.dumps(
            {"task": spec.task, "terms": spec.terms},
            ensure_ascii=False,
        )
    else:
        task = f"job {job_id}"
```

This is a signature/context/spec-loading substitution only: keep the current
`_build_stock_analysis_prompt`, `_run_work`, `signing.submit_result`, exception
mapping, and success-result statements byte-for-byte unchanged after the
`task = ...` assignment.

The optional marker transition in `_await_sweep_context` and the repeated context snapshot at the start of `_do_work_and_submit` contain no intervening await, so context cannot be mixed after fallback starts.

- [ ] **Step 6: Run the focused notify suite**

Run:

```bash
cd stockanalyst/app/agent
PYTHONPATH=../../.. .venv/bin/python -m unittest tests.test_seller_core_notify -v
```

Expected: all tests pass, including one LLM call and one gateway submit when context arrives during grace, and zero work/submit calls for a marked job without context.

- [ ] **Step 7: Commit the sweep grace state machine**

```bash
git add stockanalyst/app/agent/seller_core.py stockanalyst/app/agent/tests/test_seller_core_notify.py
git commit -m "fix: wait for authorized context during sweeps"
```

---

### Task 3: Prove deadline stability, fallback boundary, and state cleanup

**Files:**
- Modify: `stockanalyst/app/agent/seller_core.py:393-439`
- Modify: `stockanalyst/app/agent/tests/test_seller_core_notify.py`

**Interfaces:**
- Consumes: Task 2 `_context_deadlines`, `_context_events`, `_await_sweep_context`, `_contextless_started`, `_handled`, and `_run_job`.
- Produces: complete terminal/transient lifecycle guarantees for grace state; no new public API.

- [ ] **Step 1: Write failing tests for fixed deadlines and optional fallback**

Add deterministic tests that patch the grace duration rather than sleeping for 60 seconds:

```python
async def test_repeated_waits_do_not_reset_or_shorten_deadline(self) -> None:
    core = SellerCore(run_work=AsyncMock(), generator="test")
    with patch.object(
        seller_core_module,
        "_SWEEP_CONTEXT_GRACE_SECONDS",
        0.02,
    ):
        first = asyncio.create_task(
            core._await_sweep_context(JOB_ID, required=True)
        )
        while JOB_ID not in core._context_deadlines:
            await asyncio.sleep(0)
        original_deadline = core._context_deadlines[JOB_ID]
        await asyncio.sleep(0.005)
        second = asyncio.create_task(
            core._await_sweep_context(JOB_ID, required=True)
        )
        self.assertEqual(core._context_deadlines[JOB_ID], original_deadline)
        self.assertEqual(await first, False)
        self.assertEqual(await second, False)
    self.assertEqual(core._context_deadlines[JOB_ID], original_deadline)

async def test_repeated_sweep_discovery_keeps_one_grace_worker(self) -> None:
    core = SellerCore(run_work=AsyncMock(), generator="test")
    with (
        patch.object(seller_core_module, "_SWEEP_CONTEXT_GRACE_SECONDS", 1.0),
        patch.object(
            seller_core_module.signing,
            "job_spec",
            return_value=_job_spec(required=True),
        ),
    ):
        core._spawn_job(JOB_ID, verified=False)
        core._spawn_job(JOB_ID, verified=False)
        while JOB_ID not in core._context_events:
            await asyncio.sleep(0)
        self.assertEqual(len(core._tasks), 1)
        core._job_contexts[JOB_ID] = self._context_from_request(
            self._signed_request()
        )
        core._context_events[JOB_ID].set()
        core._run_work = AsyncMock(return_value="report")
        with patch.object(
            seller_core_module.signing,
            "submit_result",
            return_value=SimpleNamespace(
                submit_tx="0xtx",
                deliverable_url="https://result",
            ),
        ):
            await self._drain_tasks(core)

async def test_unmarked_job_falls_back_contextless_after_grace(self) -> None:
    run_work = AsyncMock(return_value="report")
    core = SellerCore(run_work=run_work, generator="test")
    core._inflight.add(JOB_ID)
    submitted = SimpleNamespace(
        submit_tx="0xtx",
        deliverable_url="https://result",
    )
    with (
        patch.object(seller_core_module, "_SWEEP_CONTEXT_GRACE_SECONDS", 0.001),
        patch.object(seller_core_module.signing, "job_spec", return_value=_job_spec(required=False)),
        patch.object(seller_core_module.signing, "submit_result", return_value=submitted) as submit,
    ):
        await core._run_job(JOB_ID, verified=False)

    run_work.assert_awaited_once()
    submit.assert_called_once()
    self.assertIsNone(submit.call_args.kwargs["gateway_url"])
    self.assertIn(JOB_ID, core._handled)
```

- [ ] **Step 2: Write failing tests for the atomic fallback boundary**

Wake the waiting coroutine only after installing context, which places the
authorized value immediately before the optional final snapshot. Then retain
the existing late-notify rejection test for context arriving after
`_contextless_started`:

```python
async def test_context_arriving_before_optional_transition_wins(self) -> None:
    core = SellerCore(run_work=AsyncMock(), generator="test")
    context = self._context_from_request(self._signed_request())
    event = asyncio.Event()
    core._context_events[JOB_ID] = event
    core._context_deadlines[JOB_ID] = asyncio.get_running_loop().time() + 1.0
    waiter = asyncio.create_task(
        core._await_sweep_context(JOB_ID, required=False)
    )
    await asyncio.sleep(0)
    core._job_contexts[JOB_ID] = context
    event.set()

    ready = await waiter

    self.assertIs(ready, True)
    self.assertIs(core._job_contexts[JOB_ID], context)
    self.assertNotIn(JOB_ID, core._contextless_started)
```

Retain/adjust the former `test_named_context_is_rejected_after_context_free_work_starts` so it first lets an optional worker pass its grace and enter the blocking `job_spec` or `run_work` phase, then asserts:

```python
self.assertEqual(result["status"], "rejected")
self.assertEqual(result["reason"], "delivery_already_started")
self.assertIsNone(submit.call_args.kwargs["gateway_url"])
```

- [ ] **Step 3: Write failing terminal/transient cleanup tests**

Extend the existing terminal and transient tests with exact lifecycle assertions:

```python
# Before calling _run_job in each test:
event = asyncio.Event()
core._context_events[JOB_ID] = event
core._context_deadlines[JOB_ID] = 123.0

# Terminal result assertions:
self.assertNotIn(JOB_ID, core._context_events)
self.assertNotIn(JOB_ID, core._context_deadlines)
self.assertNotIn(JOB_ID, core._contextless_started)

# Transient result assertions:
self.assertIs(core._context_events[JOB_ID], event)
self.assertEqual(core._context_deadlines[JOB_ID], 123.0)
```

Also add a permanent verification-skip case to prove it performs the same terminal cleanup without waiting for grace:

```python
async def test_permanent_sweep_skip_clears_grace_state(self) -> None:
    core = SellerCore(run_work=AsyncMock(), generator="test")
    core._inflight.add(JOB_ID)
    core._context_events[JOB_ID] = asyncio.Event()
    core._context_deadlines[JOB_ID] = 123.0
    with patch.object(
        seller_core_module.signing,
        "verify_signed_job",
        return_value=(False, "invalid_provider_signature", True),
    ):
        await core._run_job(JOB_ID, verified=False)
    self.assertIn(JOB_ID, core._handled)
    self.assertNotIn(JOB_ID, core._context_events)
    self.assertNotIn(JOB_ID, core._context_deadlines)
```

- [ ] **Step 4: Run the new lifecycle tests and verify they fail**

Run:

```bash
cd stockanalyst/app/agent
PYTHONPATH=../../.. .venv/bin/python -m unittest tests.test_seller_core_notify -v
```

Expected before cleanup implementation: deadline/event assertions fail for terminal results; deadline stability and optional fallback tests pass only if Task 2 is correct.

- [ ] **Step 5: Complete terminal cleanup without reopening grace on transient results**

In `_run_job`'s terminal branch, remove all per-job state:

```python
if terminal:
    self._handled.add(job_id)
    self._job_contexts.pop(job_id, None)
    self._context_deadlines.pop(job_id, None)
    self._context_events.pop(job_id, None)
    self._contextless_started.discard(job_id)
    self._inflight.discard(job_id)
else:
    self._inflight.discard(job_id)
    self._contextless_started.discard(job_id)
```

Do not pop `_context_deadlines`, `_context_events`, or `_job_contexts` in the transient branch. A marked no-context retry must see its already-expired deadline and return without work until a valid named notification installs context and sets the retained event.

- [ ] **Step 6: Run focused, full, build, and race regression checks**

Run:

```bash
cd stockanalyst/app/agent
PYTHONPATH=../../.. .venv/bin/python -m unittest tests.test_seller_core_notify -v
PYTHONPATH=../../.. .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
cd ../../../buyer-client
npm test
npm run build
cd ..
git diff --check
```

Expected:

- the focused seller notify suite passes;
- the complete Python agent suite passes;
- all buyer tests pass;
- TypeScript compilation succeeds;
- `git diff --check` emits no output;
- the marked no-context tests report zero LLM/submit calls;
- the wakeup test reports exactly one delivery task and one submission.

- [ ] **Step 7: Commit lifecycle and concurrency regression coverage**

```bash
git add stockanalyst/app/agent/seller_core.py stockanalyst/app/agent/tests/test_seller_core_notify.py
git commit -m "test: harden sweep context race lifecycle"
```
