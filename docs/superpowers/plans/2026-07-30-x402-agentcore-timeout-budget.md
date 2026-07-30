# x402 AgentCore Timeout Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give fresh AgentCore sessions enough time to return while retaining a bounded timeout below the Lambda and API Gateway request limits.

**Architecture:** Increase only `AgentCoreClient`'s default transport timeout from 10 seconds to 25 seconds. Keep the 28-second Lambda timeout and every existing validation, session, OAuth, payment, and error-mapping boundary unchanged.

**Tech Stack:** Python 3.13, `unittest`, AWS SAM/CloudFormation, AWS CLI profile `dev`

## Global Constraints

- Default AgentCore transport timeout must be exactly `25.0` seconds.
- Lambda timeout must remain exactly `28` seconds.
- API Gateway, WAF, OAuth, session isolation, request/response validation,
  body limits, response limits, and payment binding must not change.
- Actual transport timeouts must still map to retryable
  `503 settlement_status_unknown`.
- Every AWS CLI sequence must begin with `export AWS_PROFILE=dev`.
- No `X-Payment`, B402 call, job creation, funding, or U spend is permitted.

---

### Task 1: Increase the bounded AgentCore client timeout

**Files:**
- Modify: `gateway/x402_lambda/tests/test_agentcore_client.py`
- Modify: `gateway/x402_lambda/src/agentcore_client.py`

**Interfaces:**
- Consumes: the existing optional `timeout_seconds` constructor argument.
- Produces: a `25.0`-second default passed unchanged to the transport.

- [ ] **Step 1: Write the failing test**

Extend `AgentTransport.__init__`:

```python
        self.last_timeout_seconds = None
```

Record the value in `AgentTransport.__call__`:

```python
        self.last_timeout_seconds = timeout_seconds
```

Add:

```python
    def test_default_transport_timeout_is_twenty_five_seconds(self):
        transport = AgentTransport(a2a_response(response_envelope()))
        AgentCoreClient(
            "https://agentcore.example.test/runtime",
            lambda: "Bearer access-token",
            transport,
        ).invoke(ENVELOPE)

        self.assertEqual(transport.last_timeout_seconds, 25.0)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
PYTHONPATH=gateway/x402_lambda/src \
  stockanalyst/app/agent/.venv/bin/python -m unittest \
  gateway.x402_lambda.tests.test_agentcore_client.AgentCoreClientTests.test_default_transport_timeout_is_twenty_five_seconds
```

Expected: `FAIL` with `10.0 != 25.0`.

- [ ] **Step 3: Make the minimal implementation**

Change only the constructor default:

```python
        timeout_seconds: float = 25.0,
```

- [ ] **Step 4: Run all relevant tests**

Run:

```bash
PYTHONPATH=gateway/x402_lambda/src \
  stockanalyst/app/agent/.venv/bin/python -m unittest \
  gateway.x402_lambda.tests.test_agentcore_client -v
PYTHONPATH=gateway/x402_lambda/src \
  stockanalyst/app/agent/.venv/bin/python -m unittest discover \
  -s gateway/x402_lambda/tests -p 'test_*.py' -v
stockanalyst/app/agent/.venv/bin/python -m unittest discover \
  -s tests/infra -p 'test_x402_lambda_gateway_template.py' -v
```

Expected: all AgentCore client tests, all gateway tests, and all 10
infrastructure tests pass. The infrastructure suite continues to verify
`Timeout: 28`.

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
git commit -m "fix: extend AgentCore gateway timeout"
```

Expected: only the default timeout and its transport-level test are committed.

---

### Task 2: Update Lambda and complete no-spend verification

**Files:**
- Read: `infra/x402-lambda-gateway.yaml`
- Generate outside repository: `/tmp/x402-lambda-gateway-packaged.yaml`
- Append ignored evidence: `.superpowers/sdd/task-6-report.md`

**Interfaces:**
- Consumes: reviewed Task 1 commit and existing healthy gateway stack.
- Produces: a new immutable Lambda version selected by `live`.

- [ ] **Step 1: Re-run the complete regression**

Run all commands from Task 1 Step 4.

Expected: all tests pass.

- [ ] **Step 2: Repackage the updated Lambda**

After explicit approval for this code upload, run:

```bash
export AWS_PROFILE=dev
aws cloudformation package \
  --region us-east-1 \
  --template-file infra/x402-lambda-gateway.yaml \
  --s3-bucket bnbagent-code-stock-analyst-agent \
  --s3-prefix x402-lambda-gateway/artifacts \
  --output-template-file /tmp/x402-lambda-gateway-packaged.yaml
```

Expected: upload occurs only below the approved artifact prefix.

- [ ] **Step 3: Review and execute an UPDATE change set**

Create a newly named `UPDATE` change set using current parameters and
capabilities. Require no API, WAF, IAM, log-group, metric-filter, or alarm
replacement/deletion. Execute only changes limited to Lambda code, immutable
version, and `live` alias.

Expected: stack returns to `UPDATE_COMPLETE`; all 16 resources remain healthy.

- [ ] **Step 4: Perform final public no-payment checks**

Send exactly:

```text
GET  /x402/price
POST /x402/analyze/async without X-Payment
```

Expected: `200` and `402`, respectively. Do not retry a non-timeout response.

- [ ] **Step 5: Record sanitized completion evidence**

Append test counts, reviewed change scope, stack/alias state, Runtime/VPC/EIP
invariants, durations, and public statuses to
`.superpowers/sdd/task-6-report.md`. Do not record secrets, tokens, response
bodies, wallet material, payment proofs, or business data.
