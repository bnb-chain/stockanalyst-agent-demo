# Optional Lambda Reserved Concurrency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow the x402 gateway to deploy without Lambda reserved concurrency while preserving an explicit positive reservation option.

**Architecture:** Keep the existing `ReservedConcurrency` CloudFormation parameter, but use `0` as the sentinel for “omit `ReservedConcurrentExecutions`.” A CloudFormation condition selects either the positive parameter value or `AWS::NoValue`; API Gateway throttling and WAF rate limiting remain unchanged.

**Tech Stack:** AWS SAM/CloudFormation YAML, Python `unittest`, AWS CLI with profile `dev`

## Global Constraints

- `ReservedConcurrency=0` must omit `ReservedConcurrentExecutions`.
- Positive values from `1` through `100` must retain the existing reservation behavior.
- No API route, authentication, payment, envelope, WAF, API Gateway throttle, or AgentCore behavior may change.
- Every AWS CLI command sequence must begin with `export AWS_PROFILE=dev`.
- Deployment verification must not send `X-Payment`, call B402, or create/fund a job.
- Preserve the existing Runtime ID, Runtime ARN, VPC configuration, and fixed egress IP.

---

### Task 1: Make reserved concurrency optional

**Files:**
- Modify: `tests/infra/test_x402_lambda_gateway_template.py`
- Modify: `infra/x402-lambda-gateway.yaml`

**Interfaces:**
- Consumes: CloudFormation numeric parameter `ReservedConcurrency`.
- Produces: `UseReservedConcurrency` condition and conditional `ReservedConcurrentExecutions` property.

- [ ] **Step 1: Write the failing infrastructure test**

In `tests/infra/test_x402_lambda_gateway_template.py`, add:

```python
    def test_reserved_concurrency_is_optional_and_defaults_off(self) -> None:
        text = TEMPLATE.read_text()
        adapter = resource_section(text, "X402Adapter", "X402ApiInvokePermission")
        parameter = re.search(
            r"(?ms)^  ReservedConcurrency:\n(?P<section>(?:    .*\n)+)",
            text,
        )

        self.assertIsNotNone(parameter)
        self.assertIn("Default: 0", parameter.group("section"))
        self.assertIn("MinValue: 0", parameter.group("section"))
        self.assertIn(
            "UseReservedConcurrency: !Not [!Equals [!Ref ReservedConcurrency, 0]]",
            text,
        )
        self.assertIn(
            'ReservedConcurrentExecutions: !If [UseReservedConcurrency, !Ref ReservedConcurrency, !Ref "AWS::NoValue"]',
            adapter,
        )
```

Replace the old unconditional assertion in
`test_lambda_has_hardened_runtime_configuration_without_api_reference` with
the same conditional-property assertion.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
stockanalyst/.venv/bin/python -m unittest \
  tests.infra.test_x402_lambda_gateway_template.X402LambdaGatewayTemplateTests.test_reserved_concurrency_is_optional_and_defaults_off
```

Expected: `FAIL` because the parameter still defaults to `10`, has minimum
`1`, and the condition does not exist.

- [ ] **Step 3: Implement the minimal SAM change**

In `infra/x402-lambda-gateway.yaml`, change the parameter and add the condition:

```yaml
  ReservedConcurrency:
    Type: Number
    Default: 0
    MinValue: 0
    MaxValue: 100

Conditions:
  UseReservedConcurrency: !Not [!Equals [!Ref ReservedConcurrency, 0]]
```

Change the adapter property to:

```yaml
      ReservedConcurrentExecutions: !If [UseReservedConcurrency, !Ref ReservedConcurrency, !Ref "AWS::NoValue"]
```

- [ ] **Step 4: Run focused and full gateway tests**

Run:

```bash
stockanalyst/.venv/bin/python -m unittest \
  tests.infra.test_x402_lambda_gateway_template -v
stockanalyst/.venv/bin/python -m unittest discover \
  -s gateway/x402_lambda/tests -p 'test_*.py' -v
```

Expected: all infrastructure tests pass and all 42 gateway tests pass.

- [ ] **Step 5: Verify the diff and commit**

Run:

```bash
git diff --check
git diff -- infra/x402-lambda-gateway.yaml tests/infra/test_x402_lambda_gateway_template.py
git add infra/x402-lambda-gateway.yaml tests/infra/test_x402_lambda_gateway_template.py
git commit -m "fix: make x402 Lambda concurrency optional"
```

Expected: only the parameter, condition, conditional function property, and
their deterministic tests are committed.

---

### Task 2: Repackage and resume no-spend deployment

**Files:**
- Read: `infra/x402-lambda-gateway.yaml`
- Generate outside repository: `/tmp/x402-lambda-gateway-packaged.yaml`
- Append ignored operational evidence: `.superpowers/sdd/task-6-report.md`

**Interfaces:**
- Consumes: updated SAM template and existing artifact bucket prefix `x402-lambda-gateway/artifacts/`.
- Produces: reviewed 16-resource CloudFormation change set and deployed gateway base URL.

- [ ] **Step 1: Re-run the complete pre-package regression**

Run:

```bash
stockanalyst/.venv/bin/python -m unittest \
  tests.infra.test_x402_lambda_gateway_template -v
stockanalyst/.venv/bin/python -m unittest discover \
  -s gateway/x402_lambda/tests -p 'test_*.py' -v
```

Expected: all infrastructure tests and all 42 gateway tests pass.

- [ ] **Step 2: Repackage the updated template**

Run:

```bash
export AWS_PROFILE=dev
aws cloudformation package \
  --region us-east-1 \
  --template-file infra/x402-lambda-gateway.yaml \
  --s3-bucket bnbagent-code-stock-analyst-agent \
  --s3-prefix x402-lambda-gateway/artifacts \
  --output-template-file /tmp/x402-lambda-gateway-packaged.yaml
```

Expected: packaging succeeds and uploads only beneath the approved artifact
prefix.

- [ ] **Step 3: Replace the rollback-complete stack through a reviewed change set**

Delete only the exact `Stockanalyst-x402-gateway` rollback-complete stack,
verify its generated role, Lambda, log groups, alarms, and WebACL are absent,
then create a newly named `CREATE` change set using the preserved OAuth secret
ARN and existing AgentCore invocation URL.

Expected: the change set is `CREATE_COMPLETE/AVAILABLE`, contains exactly the
approved 16 `Add` actions, contains no replacement, and uses only
`CAPABILITY_IAM` and `CAPABILITY_AUTO_EXPAND`.

- [ ] **Step 4: Execute and verify the gateway stack**

Execute only the reviewed change set and wait for `CREATE_COMPLETE`.

Expected: the adapter has no reserved concurrency configuration, API Gateway
invokes the immutable `live` alias, WAF is associated with the `testnet`
stage, and the stack publishes `X402GatewayBaseUrl`.

- [ ] **Step 5: Update the existing Runtime and perform no-spend smoke tests**

Set the Agent environment value `X402_GATEWAY_PUBLIC_BASE_URL` to the exact
stack output and deploy in place with the verified locked local `bag`.

Expected:

- Runtime ID and ARN are unchanged and status returns to `READY/VPC`.
- Fixed egress remains `52.73.72.22`.
- Direct envelope without payment returns the expected 402 response.
- Public `GET /x402/price` returns 200.
- Public analyze without payment returns 402.
- No request sends `X-Payment`; no B402 request or job funding occurs; U spent
  remains exactly `0`.

- [ ] **Step 6: Record sanitized evidence**

Append stack, Runtime, WAF, alias, response-status, and safe CloudWatch
consistency evidence to `.superpowers/sdd/task-6-report.md`. Do not record
client secrets, access tokens, payment proofs, portfolio data, wallet
passwords, keystore contents, or secret values.
