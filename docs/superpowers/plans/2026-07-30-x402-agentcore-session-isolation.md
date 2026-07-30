# x402 AgentCore Session Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent stale AgentCore session state from surviving Runtime updates while preserving deterministic x402 request and payment binding.

**Architecture:** Generate a new 128-bit hexadecimal transport session identifier for every `AgentCoreClient.invoke` call. Keep the deterministic envelope request ID unchanged in the JSON-RPC ID, A2A message ID, response validation, and business payload.

**Tech Stack:** Python 3.13, `secrets`, `unittest`, AWS SAM/CloudFormation, AWS CLI profile `dev`

## Global Constraints

- Session IDs must match `x402-gateway-session-[0-9a-f]{32}`.
- Identical envelopes must use different session IDs.
- JSON-RPC ID, A2A message ID, envelope request ID, payment proof, job token,
  and request body must remain unchanged.
- Existing strict response binding and bounded reads must remain intact.
- Every AWS CLI command sequence must begin with `export AWS_PROFILE=dev`.
- No `X-Payment`, B402 call, job creation, funding, or U spend is permitted.
- Preserve the existing Runtime ID/ARN, VPC, fixed EIP, API routes, WAF, and
  gateway stack.

---

### Task 1: Randomize only the AgentCore transport session

**Files:**
- Modify: `gateway/x402_lambda/tests/test_agentcore_client.py`
- Modify: `gateway/x402_lambda/src/agentcore_client.py`

**Interfaces:**
- Consumes: deterministic x402 envelope mapping.
- Produces: one fresh `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` per
  `AgentCoreClient.invoke` call.

- [ ] **Step 1: Write the failing test**

Extend `AgentTransport` to retain every request:

```python
        self.calls = []
```

and append in `__call__`:

```python
        self.calls.append({
            "headers": dict(headers),
            "body": body,
        })
```

Add this test:

```python
    def test_identical_requests_use_fresh_sessions_but_keep_request_binding(self):
        transport = AgentTransport(a2a_response(response_envelope()))
        client = AgentCoreClient(
            "https://agentcore.example.test/runtime",
            lambda: "Bearer access-token",
            transport,
        )

        with patch(
            "agentcore_client.secrets.token_hex",
            side_effect=["a" * 32, "b" * 32],
        ):
            client.invoke(ENVELOPE)
            client.invoke(ENVELOPE)

        session_ids = [
            call["headers"]["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"]
            for call in transport.calls
        ]
        self.assertEqual(session_ids, [
            f"x402-gateway-session-{'a' * 32}",
            f"x402-gateway-session-{'b' * 32}",
        ])
        self.assertNotEqual(session_ids[0], session_ids[1])
        requests = [json.loads(call["body"]) for call in transport.calls]
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[0]["id"], REQUEST_ID)
        self.assertEqual(
            requests[0]["params"]["message"]["messageId"],
            REQUEST_ID,
        )
        self.assertEqual(
            requests[0]["params"]["message"]["parts"][0]["data"]["envelope"]["requestId"],
            REQUEST_ID,
        )
```

Update the existing session assertion to require the prefix and a 32-character
lowercase hexadecimal suffix instead of the request ID.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
PYTHONPATH=gateway/x402_lambda/src \
  stockanalyst/app/agent/.venv/bin/python -m unittest \
  gateway.x402_lambda.tests.test_agentcore_client.AgentCoreClientTests.test_identical_requests_use_fresh_sessions_but_keep_request_binding
```

Expected: `FAIL` because both calls derive the same session ID from
`REQUEST_ID`.

- [ ] **Step 3: Implement the minimal transport-only change**

Add:

```python
import secrets
```

Replace the deterministic session assignment with:

```python
        session_id = f"x402-gateway-session-{secrets.token_hex(16)}"
```

Do not modify envelope construction or response validation.

- [ ] **Step 4: Run focused and full tests**

Run:

```bash
PYTHONPATH=gateway/x402_lambda/src \
  stockanalyst/app/agent/.venv/bin/python -m unittest \
  gateway.x402_lambda.tests.test_agentcore_client -v
PYTHONPATH=gateway/x402_lambda/src \
  stockanalyst/app/agent/.venv/bin/python -m unittest discover \
  -s gateway/x402_lambda/tests -p 'test_*.py' -v
stockanalyst/app/agent/.venv/bin/python -m unittest \
  tests.infra.test_x402_lambda_gateway_template -v
```

Expected: all AgentCore client tests pass, all gateway tests pass, and all
infrastructure tests pass.

- [ ] **Step 5: Verify and commit**

Run:

```bash
git diff --check
git diff -- \
  gateway/x402_lambda/src/agentcore_client.py \
  gateway/x402_lambda/tests/test_agentcore_client.py
git add \
  gateway/x402_lambda/src/agentcore_client.py \
  gateway/x402_lambda/tests/test_agentcore_client.py
git commit -m "fix: isolate AgentCore gateway sessions"
```

Expected: only the session generation and deterministic tests are committed.

---

### Task 2: Update the deployed Lambda and verify no-spend behavior

**Files:**
- Read: `infra/x402-lambda-gateway.yaml`
- Generate outside repository: `/tmp/x402-lambda-gateway-packaged.yaml`
- Append ignored evidence: `.superpowers/sdd/task-6-report.md`

**Interfaces:**
- Consumes: reviewed Task 1 commit and existing
  `Stockanalyst-x402-gateway` stack.
- Produces: updated immutable Lambda version and `live` alias.

- [ ] **Step 1: Re-run the complete regression suite**

Run the three commands from Task 1 Step 4.

Expected: all AgentCore client, gateway, and infrastructure tests pass.

- [ ] **Step 2: Repackage only under the approved artifact prefix**

After explicit code-upload approval, run:

```bash
export AWS_PROFILE=dev
aws cloudformation package \
  --region us-east-1 \
  --template-file infra/x402-lambda-gateway.yaml \
  --s3-bucket bnbagent-code-stock-analyst-agent \
  --s3-prefix x402-lambda-gateway/artifacts \
  --output-template-file /tmp/x402-lambda-gateway-packaged.yaml
```

Expected: the updated Lambda artifact is uploaded only below
`x402-lambda-gateway/artifacts/`.

- [ ] **Step 3: Review an UPDATE change set**

Create a newly named `UPDATE` change set for the existing
`Stockanalyst-x402-gateway` stack with the existing parameters and capabilities.

Expected: no API, WAF, IAM role, alarm, or log-group replacement or deletion;
changes are limited to the adapter Lambda code/version and its `live` alias,
plus any SAM-generated deployment metadata required by the same template.

- [ ] **Step 4: Execute and verify the update**

Execute only the reviewed change set and wait for `UPDATE_COMPLETE`.

Expected: all 16 stack resources remain healthy and `live` points to a new
immutable Lambda version.

- [ ] **Step 5: Run final no-payment verification**

Run one public request for each route:

```text
GET  /x402/price
POST /x402/analyze/async without X-Payment
```

Expected:

- price returns `200`;
- unpaid analyze returns `402`;
- no request sends `X-Payment`;
- no B402 request, job creation, funding, or U spend occurs.

- [ ] **Step 6: Record sanitized evidence**

Append test counts, change-set scope, stack/alias state, Runtime/VPC/EIP
invariants, public statuses, and safe response metadata to
`.superpowers/sdd/task-6-report.md`. Do not record OAuth secrets, access tokens,
payment proofs, wallet material, portfolio data, or response bodies.
