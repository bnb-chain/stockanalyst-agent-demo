# x402 Lambda Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the existing paid asynchronous x402 API through API Gateway REST API, AWS WAF, and a stateless Lambda adapter that tunnels restricted HTTP envelopes through the existing AgentCore A2A Runtime.

**Architecture:** API Gateway exposes four explicit `/x402/*` routes and invokes a Python Lambda proxy. Lambda validates and converts each request into an `envelope-v1` carried inside a valid A2A `message/send` data part. The Agent dispatches the envelope directly into the existing `X402Handler` through in-process ASGI calls, returns a restricted response envelope, and continues to own payment verification, B402 settlement, wallet operations, OpenRouter analysis, and private S3 job state.

**Tech Stack:** Python 3.13 Lambda, Python 3.10+ Agent, Amazon API Gateway REST API, AWS SAM/CloudFormation, AWS WAFv2, Cognito OAuth2 client credentials, Bedrock AgentCore A2A, ASGI, AWS Secrets Manager, CloudWatch, BSC Testnet, B402, S3.

## Global Constraints

- Follow `stockanalyst/AGENTS.md`; load the `/bnbagent-studio` skill before implementation if it is available. It was not installed in the planning environment, so its absence must be reported and the documented invariants still apply.
- Use `bag deploy`, never raw `agentcore deploy`, for Agent Runtime changes.
- Every AWS CLI sequence must begin with `export AWS_PROFILE=dev`.
- Never print or commit OAuth credentials, access tokens, B402 secrets, OpenRouter keys, wallet passwords, keystore contents, payment proofs, job tokens, portfolio payloads, S3 object keys, or presigned URLs.
- Never copy `.studio/wallets/` under the Agent deploy code location.
- The Agent remains the only wallet holder and signer. Lambda must not receive wallet or B402 credentials.
- The public gateway exposes exactly four route/method pairs; there is no catch-all route.
- The first release does not expose `/x402/free`, SSE, MCP, or A2A.
- The existing A2A `negotiate` and `notify_funded` contracts must remain unchanged.
- The gateway uses the generated API Gateway HTTPS URL; no custom domain is included.
- No paid smoke test may run until the user explicitly approves the 1 U spend.
- Preserve the user-owned untracked `DEPLOYMENT.zh-CN.md` and job 398 HTML/PDF unless the user separately asks to modify them.

---

## File Map

### Agent and buyer

- Create `stockanalyst/app/agent/x402_envelope.py`
  - Strict `envelope-v1` request/response validation.
  - Direct in-process ASGI dispatch into `X402Handler`.
- Create `stockanalyst/app/agent/tests/test_x402_envelope.py`
  - Contract, route, header, origin, body, response, streaming, and logging tests.
- Modify `stockanalyst/app/agent/executor.py`
  - Add hidden `x402_http_envelope` infrastructure dispatch.
- Modify `stockanalyst/app/agent/main.py`
  - Build one reusable x402 ASGI application and inject it into the executor.
- Modify `stockanalyst/app/agent/x402_handler.py`
  - Generate challenge resource URLs from the trusted public base URL.
  - Keep the B402 settlement attempt inside the API Gateway request budget.
- Modify `stockanalyst/app/agent/x402_verify.py`
  - Resolve and validate the active seller wallet instead of using the stale literal.
- Modify `stockanalyst/app/agent/tests/test_x402_verify.py`
  - Seller-wallet configuration and challenge-resource tests.
- Modify `stockanalyst/app/agent/tests/test_x402_async_handler.py`
  - Trusted public URL propagation and regression coverage.
- Modify `stockanalyst/app/agent/tests/test_agent_card.py`
  - Prove the bridge is not advertised as a business skill.
- Modify `buyer-client/src/x402-payment.ts`
  - Require a configured seller wallet and sign to that address.
- Modify `buyer-client/src/x402-async.ts`
  - Resolve and display the configured seller wallet.
- Modify `buyer-client/src/x402free.ts`
  - Remove the stale seller literal even though the route is not gateway-published.
- Modify `buyer-client/src/x402-async-client.test.ts`
  - Buyer seller-wallet pinning tests.
- Modify `buyer-client/.env.example`
  - Document `X402_SELLER_WALLET` without embedding an environment-specific default.

### Lambda gateway

- Create `gateway/x402_lambda/src/envelope.py`
  - Route/header validation, deterministic request IDs, and envelope construction.
- Create `gateway/x402_lambda/src/oauth_client.py`
  - Exact-secret retrieval and safe Cognito token cache.
- Create `gateway/x402_lambda/src/agentcore_client.py`
  - A2A request construction and AgentCore response extraction.
- Create `gateway/x402_lambda/src/handler.py`
  - API Gateway proxy entrypoint and stable error mapping.
- Create `gateway/x402_lambda/tests/test_envelope.py`
- Create `gateway/x402_lambda/tests/test_oauth_client.py`
- Create `gateway/x402_lambda/tests/test_agentcore_client.py`
- Create `gateway/x402_lambda/tests/test_handler.py`

### Infrastructure and operations

- Create `infra/x402-lambda-gateway.yaml`
  - SAM/CloudFormation gateway stack.
- Create `tests/infra/test_x402_lambda_gateway_template.py`
  - Static security and route assertions.
- Create `docs/x402-lambda-gateway.md`
  - Secret-safe packaging, deployment, validation, and rollback runbook.
- Modify `buyer-client/README.md`
  - Point `X402_ENDPOINT` at the gateway output and document the four-route boundary.
- Modify `stockanalyst/README.md`
  - Document AgentCore envelope bridge and prerequisites.

---

### Task 1: Correct x402 Seller Identity and Trusted Challenge URLs

**Files:**
- Modify: `stockanalyst/app/agent/x402_verify.py`
- Modify: `stockanalyst/app/agent/x402_handler.py`
- Modify: `stockanalyst/app/agent/tests/test_x402_verify.py`
- Modify: `stockanalyst/app/agent/tests/test_x402_async_handler.py`
- Modify: `buyer-client/src/x402-payment.ts`
- Modify: `buyer-client/src/x402-async.ts`
- Modify: `buyer-client/src/x402free.ts`
- Modify: `buyer-client/src/x402-async-client.test.ts`
- Modify: `buyer-client/.env.example`

**Interfaces:**
- Consumes: `[wallet].address` from `studio.toml` or explicit `X402_SELLER_WALLET`.
- Produces: Python constant `SELLER_WALLET`; TypeScript function `resolveX402SellerWallet(env)`; `build_payment_challenge(symbols, resource_url)`.

- [ ] **Step 1: Add failing Python tests for seller-wallet configuration**

Add tests for an injectable resolver so the test suite does not leak a
module-reload mutation into later tests:

```python
def test_seller_wallet_comes_from_studio_wallet_config(self) -> None:
    resolved = x402_verify._resolve_seller_wallet(
        {},
        lambda: {"wallet": {"address": ACTIVE_SELLER}},
    )
    self.assertEqual(resolved, ACTIVE_SELLER)

def test_invalid_seller_wallet_fails_closed(self) -> None:
    with self.assertRaisesRegex(RuntimeError, "seller wallet"):
        x402_verify._resolve_seller_wallet(
            {},
            lambda: {"wallet": {"address": "not-an-address"}},
        )
```

Add a challenge test:

```python
def test_payment_challenge_uses_exact_https_resource_url(self) -> None:
    challenge = x402_verify.build_payment_challenge(
        ["AAPL"],
        "https://api.example.test/testnet/x402/analyze/async",
    )
    self.assertEqual(
        challenge["resource"],
        "https://api.example.test/testnet/x402/analyze/async",
    )
```

- [ ] **Step 2: Run the focused Python tests and observe RED**

Run:

```bash
cd stockanalyst/app/agent
uv run python -m unittest \
  tests.test_x402_verify \
  tests.test_x402_async_handler
```

Expected: failures because `SELLER_WALLET` is still the stale literal and
`build_payment_challenge` still constructs `http://{host}`.

- [ ] **Step 3: Implement validated seller-wallet resolution**

Add a resolver in `x402_verify.py`:

```python
_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")


def _resolve_seller_wallet(
    env: Mapping[str, str] = os.environ,
    studio_loader: Callable[[], Mapping[str, Any] | None] | None = None,
) -> str:
    explicit = env.get("X402_SELLER_WALLET", "").strip()
    if explicit:
        raw = explicit
    else:
        try:
            if studio_loader is None:
                from bnbagent_studio_core import config
                studio_loader = config.load_studio_toml
            studio = studio_loader() or {}
            raw = str((studio.get("wallet") or {}).get("address") or "").strip()
        except Exception as exc:
            raise RuntimeError("x402 seller wallet configuration unavailable") from exc
    if not _EVM_ADDRESS.fullmatch(raw):
        raise RuntimeError("x402 seller wallet must be a 0x-prefixed EVM address")
    return raw


SELLER_WALLET = _resolve_seller_wallet()
```

Change challenge construction to accept a complete trusted URL:

```python
def build_payment_challenge(
    symbols: list[str],
    resource_url: str = "http://localhost:9000/x402/analyze/async",
) -> dict:
    return {
        "x402Version": 2,
        "accepts": [{
            "scheme": "exact",
            "network": f"eip155:{CHAIN_ID}",
            "maxAmountRequired": str(PRICE_WEI),
            "asset": U_TOKEN_BSC_TESTNET,
            "payTo": SELLER_WALLET.lower(),
            "maxTimeoutSeconds": 600,
            "extra": {
                "assetTransferMethod": "eip3009",
                "name": _TOKEN_DOMAIN_NAME,
                "version": _TOKEN_DOMAIN_VERSION,
                "description": (
                    f"Stock analysis for {', '.join(s.upper() for s in symbols)}"
                    if symbols else "Stock analysis report"
                ),
            },
        }],
        "error": "Payment Required",
        "resource": resource_url,
    }
```

In `x402_handler.py`, add:

```python
def _public_resource(scope: dict, path: str) -> str:
    trusted = str(scope.get("x402_public_base_url") or "").rstrip("/")
    if trusted:
        return f"{trusted}{path}"
    scheme = str(scope.get("scheme") or "http")
    return f"{scheme}://{_host(scope)}{path}"
```

Pass `_public_resource(scope, "/x402/analyze/async")` to
`build_payment_challenge`.

Set the outbound B402/facilitator HTTP client timeout to 20 seconds. Add a
focused test that injects a timeout transport and proves the service returns
the existing safe retryable/settlement-pending outcome without deleting the
durable `settling` record. The REST API integration is capped at 29 seconds and
the Lambda at 28 seconds, leaving roughly eight seconds for the AgentCore and
gateway hops.

- [ ] **Step 4: Add failing TypeScript tests for explicit buyer pinning**

Add these tests:

```typescript
test("requires an explicit x402 seller wallet", () => {
  assert.throws(
    () => resolveX402SellerWallet({}),
    /X402_SELLER_WALLET/,
  );
});

test("signs the authorization to the configured seller wallet", async () => {
  const seller = "0xd10BdDC20E4DC42A1a19a9653e994991e25b8153";
  const proof = decodeProof(
    await buildPaymentProof(Wallet.createRandom(), undefined, undefined, seller),
  );
  assert.equal(proof.payload.authorization.to, seller.toLowerCase());
});
```

- [ ] **Step 5: Run the buyer tests and observe RED**

Run:

```bash
cd buyer-client
npm test
```

Expected: compile/test failure because `resolveX402SellerWallet` and the seller
parameter do not exist.

- [ ] **Step 6: Implement buyer seller-wallet pinning**

In `x402-payment.ts`, use `ethers` validation:

```typescript
import { getAddress, type Wallet } from "ethers";

export function resolveX402SellerWallet(
  env: Readonly<Record<string, string | undefined>> = process.env,
): string {
  const raw = env["X402_SELLER_WALLET"]?.trim();
  if (!raw) throw new Error("X402_SELLER_WALLET is required");
  try {
    return getAddress(raw).toLowerCase();
  } catch {
    throw new Error("X402_SELLER_WALLET must be a valid EVM address");
  }
}

export async function buildPaymentProof(
  wallet: Wallet,
  priceWei = "1000000000000000000",
  ttlSeconds = 600,
  sellerWallet = resolveX402SellerWallet(),
): Promise<string> {
  // existing proof construction, with authorization.to = sellerWallet
}
```

Resolve the address once in each CLI, pass it into `buildPaymentProof`, and use
the same value for display. Remove every hard-coded `0x1FF095...` seller
literal from x402 seller and buyer code. Add to `.env.example`:

```dotenv
X402_SELLER_WALLET=0xYourSellerWalletAddress
```

- [ ] **Step 7: Run focused and full tests**

Run:

```bash
cd stockanalyst/app/agent
uv run python -m unittest \
  tests.test_x402_verify \
  tests.test_x402_async_handler
uv run ruff check x402_verify.py x402_handler.py tests/test_x402_verify.py tests/test_x402_async_handler.py

cd ../../../../buyer-client
npm test
```

Expected: all commands pass; no source file contains the stale seller literal
except historical documentation explicitly describing the migration.

- [ ] **Step 8: Commit**

```bash
git add \
  stockanalyst/app/agent/x402_verify.py \
  stockanalyst/app/agent/x402_handler.py \
  stockanalyst/app/agent/tests/test_x402_verify.py \
  stockanalyst/app/agent/tests/test_x402_async_handler.py \
  buyer-client/src/x402-payment.ts \
  buyer-client/src/x402-async.ts \
  buyer-client/src/x402free.ts \
  buyer-client/src/x402-async-client.test.ts \
  buyer-client/.env.example
git commit -m "fix: bind x402 payments to configured seller"
```

---

### Task 2: Add the AgentCore x402 Envelope Bridge

**Files:**
- Create: `stockanalyst/app/agent/x402_envelope.py`
- Create: `stockanalyst/app/agent/tests/test_x402_envelope.py`
- Modify: `stockanalyst/app/agent/executor.py`
- Modify: `stockanalyst/app/agent/main.py`
- Modify: `stockanalyst/app/agent/tests/test_agent_card.py`

**Interfaces:**
- Consumes: ASGI `X402Handler`; A2A data `{skill:"x402_http_envelope", envelope:{...}}`.
- Produces: `async dispatch_x402_envelope(app, envelope, *, expected_public_base_url) -> dict[str, Any]`.

- [ ] **Step 1: Write failing envelope contract tests**

Create `test_x402_envelope.py` with a recording ASGI app and tests for:

```python
async def test_dispatch_preserves_402_response(self) -> None:
    envelope = request_envelope(method="POST", path="/x402/analyze/async")
    result = await dispatch_x402_envelope(
        payment_required_app,
        envelope,
        expected_public_base_url=PUBLIC_BASE,
    )
    self.assertEqual(result["status"], 402)
    self.assertEqual(result["requestId"], envelope["requestId"])
    self.assertEqual(
        base64.b64decode(result["bodyBase64"]),
        b'{"error":"Payment Required"}',
    )

async def test_dispatch_rejects_arbitrary_path(self) -> None:
    with self.assertRaisesRegex(EnvelopeError, "route_not_allowed"):
        await dispatch_x402_envelope(
            recording_app,
            request_envelope(path="/admin"),
            expected_public_base_url=PUBLIC_BASE,
        )

async def test_dispatch_rejects_public_base_mismatch(self) -> None:
    envelope = request_envelope(public_base_url="https://evil.example")
    with self.assertRaisesRegex(EnvelopeError, "public_base_mismatch"):
        await dispatch_x402_envelope(
            recording_app,
            envelope,
            expected_public_base_url=PUBLIC_BASE,
        )

async def test_dispatch_rejects_streaming_response(self) -> None:
    with self.assertRaisesRegex(EnvelopeError, "streaming_not_supported"):
        await dispatch_x402_envelope(
            streaming_app,
            request_envelope(),
            expected_public_base_url=PUBLIC_BASE,
        )
```

Also test method/path pairs, lowercase header allowlist, base64 validity, 256 KiB
request limit, response header allowlist, response size limit, duplicate response
start, missing response start, and caller `Host`/`Authorization` rejection.

- [ ] **Step 2: Run the new tests and observe RED**

Run:

```bash
cd stockanalyst/app/agent
uv run python -m unittest tests.test_x402_envelope
```

Expected: import failure because `x402_envelope.py` does not exist.

- [ ] **Step 3: Implement the strict envelope dispatcher**

Create:

```python
class EnvelopeError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_ROUTES = {
    ("GET", "/x402/price"),
    ("POST", "/x402/analyze/async"),
}
_JOB_ROUTE = re.compile(r"/x402/jobs/x402_[0-9a-f]{32}(?:/resume)?\Z")
_REQUEST_HEADERS = {"accept", "content-type", "x-payment", "x-job-token"}
_RESPONSE_HEADERS = {
    "content-type",
    "location",
    "retry-after",
    "cache-control",
    "vary",
    "x-payment-required",
}
_MAX_REQUEST_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


async def dispatch_x402_envelope(
    app,
    envelope: dict[str, Any],
    *,
    expected_public_base_url: str,
) -> dict[str, Any]:
    request = _validate_request(envelope, expected_public_base_url)
    sent: list[dict[str, Any]] = []
    pending = [{
        "type": "http.request",
        "body": request.body,
        "more_body": False,
    }]

    async def receive() -> dict[str, Any]:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "http_version": "1.1",
            "scheme": "https",
            "method": request.method,
            "path": request.path,
            "raw_path": request.path.encode(),
            "query_string": b"",
            "headers": request.headers,
            "x402_public_base_url": request.public_base_url,
        },
        receive,
        send,
    )
    return _validate_and_encode_response(request.request_id, sent)
```

Keep all validation helpers private and deterministic. Do not log envelope
headers or bodies.

- [ ] **Step 4: Write failing executor bridge tests**

Add tests proving:

```python
async def test_hidden_envelope_skill_dispatches_without_llm(self) -> None:
    executor = SellerAgentExecutor(
        run_work=AsyncMock(),
        generator="stockanalyst",
        network="bsc-testnet",
        x402_app=payment_required_app,
        x402_public_base_url=PUBLIC_BASE,
    )
    result = await executor.dispatch_skill({
        "skill": "x402_http_envelope",
        "envelope": request_envelope(),
    })
    self.assertEqual(result["status"], 402)
    executor._run_work.assert_not_awaited()

def test_bridge_is_not_advertised(self) -> None:
    self.assertNotIn("x402_http_envelope", SellerCore._skills())
```

- [ ] **Step 5: Refactor executor dispatch and inject one x402 ASGI app**

Add an executor constructor and a testable dispatch method:

```python
def __init__(
    self,
    *args,
    x402_app=None,
    x402_public_base_url: str = "",
    **kwargs,
) -> None:
    super().__init__(*args, **kwargs)
    self._x402_app = x402_app
    self._x402_public_base_url = x402_public_base_url.rstrip("/")


async def dispatch_skill(self, data: dict[str, Any]) -> dict[str, Any]:
    skill = data.get("skill")
    if skill == "x402_http_envelope":
        if self._x402_app is None or not self._x402_public_base_url:
            raise RuntimeError("x402 envelope bridge is not configured")
        return await dispatch_x402_envelope(
            self._x402_app,
            data.get("envelope"),
            expected_public_base_url=self._x402_public_base_url,
        )
    if skill == "negotiate":
        return await self.negotiate(data)
    if skill == "notify_funded":
        return await self.notify_funded(data)
    return self._unknown_skill(skill)
```

Make `execute` call `dispatch_skill`.

In `main.py`, create one x402 app:

```python
async def _x402_not_found(scope, receive, send):
    await send({
        "type": "http.response.start",
        "status": 404,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({
        "type": "http.response.body",
        "body": b'{"error":"not found"}',
        "more_body": False,
    })


x402_app = X402Handler(
    _x402_not_found,
    free_stream_work=_stream_free,
    job_service=x402_jobs,
)
```

Inject:

```python
executor = SellerAgentExecutor(
    run_work=_run_llm,
    generator=GENERATOR,
    network=_default_network(),
    x402_app=x402_app,
    x402_public_base_url=os.environ.get("X402_GATEWAY_PUBLIC_BASE_URL", ""),
)
```

Reuse `x402_app` for local single-port and optional dual-port startup instead of
constructing another handler.

- [ ] **Step 6: Prove the Agent Card remains unchanged**

Add this regression test:

```python
def test_card_does_not_advertise_internal_x402_envelope(self) -> None:
    card = _load_agent_card().build_agent_card()
    skill_ids = {skill.id for skill in card.skills}
    self.assertEqual(skill_ids, {"negotiate", "notify_funded"})
```

- [ ] **Step 7: Run focused and full Agent tests**

Run:

```bash
cd stockanalyst/app/agent
uv run python -m unittest \
  tests.test_x402_envelope \
  tests.test_x402_async_handler \
  tests.test_agent_card
uv run python -m unittest discover -s tests -p 'test_*.py'
uv run ruff check .
```

Expected: all tests and Ruff pass; no loopback network function exists in
`x402_envelope.py`.

- [ ] **Step 8: Commit**

```bash
git add \
  stockanalyst/app/agent/x402_envelope.py \
  stockanalyst/app/agent/executor.py \
  stockanalyst/app/agent/main.py \
  stockanalyst/app/agent/tests/test_x402_envelope.py \
  stockanalyst/app/agent/tests/test_agent_card.py
git commit -m "feat: bridge x402 HTTP through AgentCore A2A"
```

---

### Task 3: Implement the Stateless Lambda Adapter

**Files:**
- Create: `gateway/x402_lambda/src/envelope.py`
- Create: `gateway/x402_lambda/src/oauth_client.py`
- Create: `gateway/x402_lambda/src/agentcore_client.py`
- Create: `gateway/x402_lambda/src/handler.py`
- Create: `gateway/x402_lambda/tests/test_envelope.py`
- Create: `gateway/x402_lambda/tests/test_oauth_client.py`
- Create: `gateway/x402_lambda/tests/test_agentcore_client.py`
- Create: `gateway/x402_lambda/tests/test_handler.py`

**Interfaces:**
- Consumes: API Gateway REST proxy event; OAuth secret ARN; AgentCore invocation URL.
- Produces: `handler.lambda_handler(event, context) -> dict`; A2A envelope calls.

**Approved boundary correction (2026-07-30):** Lambda derives the envelope
`publicBaseUrl` from trusted REST `requestContext.domainName` plus
`requestContext.stage`, requiring the generated execute-api hostname shape, a
safe stage, and HTTPS construction. Missing or malformed context maps to the
safe configuration `503`; caller `Host` and `X-Forwarded-*` never influence
the value. `build_envelope(public_base_url=...)` remains unchanged, but Lambda
configuration now consumes only `AGENTCORE_INVOKE_URL` and `OAUTH_SECRET_ARN`.
The Agent continues to compare the envelope value with its separately
configured `X402_GATEWAY_PUBLIC_BASE_URL`. Lambda safe logging is one stdout
root JSON record with exactly request ID, route, status, outcome, and duration.

- [ ] **Step 1: Write failing route and envelope tests**

Test exact method/path validation:

```python
def test_create_route_builds_envelope(self) -> None:
    event = api_event(
        method="POST",
        path="/x402/analyze/async",
        headers={"X-Payment": "proof", "Content-Type": "application/json"},
        body=b'{"symbols":["AAPL"]}',
    )
    envelope = build_envelope(event, public_base_url=PUBLIC_BASE)
    self.assertEqual(envelope["method"], "POST")
    self.assertEqual(envelope["path"], "/x402/analyze/async")
    self.assertEqual(envelope["publicBaseUrl"], PUBLIC_BASE)
    self.assertEqual(envelope["headers"]["x-payment"], "proof")

def test_rejects_unpublished_route(self) -> None:
    with self.assertRaisesRegex(GatewayRequestError, "route_not_allowed"):
        build_envelope(
            api_event(method="GET", path="/x402/free"),
            public_base_url=PUBLIC_BASE,
        )

def test_request_id_is_stable_for_exact_retry(self) -> None:
    first = build_envelope(EVENT, public_base_url=PUBLIC_BASE)
    second = build_envelope(EVENT, public_base_url=PUBLIC_BASE)
    self.assertEqual(first["requestId"], second["requestId"])
```

Test forbidden headers, invalid base64 API events, 256 KiB body limit, job ID
shape, stage-prefix normalization, and absence of Host/Authorization/forwarding
headers.

- [ ] **Step 2: Run tests and observe RED**

Run:

```bash
PYTHONPATH=gateway/x402_lambda/src \
python3 -m unittest discover -s gateway/x402_lambda/tests -p 'test_*.py'
```

Expected: import failures because the Lambda modules do not exist.

- [ ] **Step 3: Implement `envelope.py`**

Use standard-library-only request processing:

```python
class GatewayRequestError(ValueError):
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def build_envelope(
    event: Mapping[str, Any],
    *,
    public_base_url: str,
) -> dict[str, Any]:
    method, path = _validated_route(event)
    body = _decode_body(event)
    headers = _allowed_headers(event.get("headers") or {})
    digest = hashlib.sha256()
    for part in (
        b"x402-gateway-v1\0",
        method.encode(),
        b"\0",
        path.encode(),
        b"\0",
        headers.get("x-payment", "").encode(),
        b"\0",
        headers.get("x-job-token", "").encode(),
        b"\0",
        body,
    ):
        digest.update(part)
    request_id = f"x402gw_{digest.hexdigest()}"
    return {
        "version": 1,
        "requestId": request_id,
        "method": method,
        "path": path,
        "publicBaseUrl": _validate_public_base(public_base_url),
        "headers": headers,
        "bodyBase64": base64.b64encode(body).decode(),
    }
```

Do not log `event`, `headers`, `body`, or the digest inputs.

- [ ] **Step 4: Write and implement OAuth cache tests**

Tests must prove:

```python
def test_secret_is_read_only_once_per_warm_cache(self) -> None:
    client = OAuthClient(secret_reader, token_transport, clock=clock)
    self.assertEqual(client.authorization_header(), "Bearer token-1")
    self.assertEqual(client.authorization_header(), "Bearer token-1")
    secret_reader.assert_called_once()
    token_transport.assert_called_once()

def test_refreshes_before_expiry(self) -> None:
    # advance clock to less than 30 seconds before expiry
    self.assertEqual(client.authorization_header(), "Bearer token-2")
```

Implement an `OAuthClient` that validates the secret JSON contains exactly
non-empty `client_id`, `client_secret`, `token_url`, and `scope`. Use Basic auth
for the token request, cache until `expires_in - 30 seconds`, and raise stable
`OAuthUnavailable` errors without including response bodies.

- [ ] **Step 5: Write and implement AgentCore client tests**

Tests:

```python
def test_invocation_is_valid_a2a_data_part(self) -> None:
    response = client.invoke(ENVELOPE)
    request = json.loads(transport.last_body)
    data = request["params"]["message"]["parts"][0]["data"]
    self.assertEqual(data, {
        "skill": "x402_http_envelope",
        "envelope": ENVELOPE,
    })
    self.assertGreaterEqual(
        len(transport.last_headers[
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
        ]),
        33,
    )

def test_rejects_mismatched_response_request_id(self) -> None:
    with self.assertRaises(InvalidAgentResponse):
        client.invoke(ENVELOPE)
```

Implement `AgentCoreClient.invoke(envelope)`. It posts JSON to the configured
runtime URL with Bearer auth, `Content-Type: application/json`, and a stable
session ID:

```python
session_id = f"x402-gateway-session-{envelope['requestId']}"
```

Extract only `result.parts[*].kind == "data"` and require one response envelope.
Map upstream 401/403, 429, timeout, and 5xx to typed exceptions without returning
upstream bodies.

- [ ] **Step 6: Write and implement Lambda handler tests**

Test:

```python
def test_reconstructs_application_402(self) -> None:
    agentcore.invoke.return_value = response_envelope(
        status=402,
        headers={
            "content-type": "application/json",
            "x-payment-required": '{"x402Version":2}',
        },
        body=b'{"error":"Payment Required"}',
    )
    result = lambda_handler(EVENT, CONTEXT)
    self.assertEqual(result["statusCode"], 402)
    self.assertEqual(result["headers"]["x-payment-required"], '{"x402Version":2}')

def test_oauth_failure_returns_safe_503(self) -> None:
    oauth.authorization_header.side_effect = OAuthUnavailable()
    self.assertEqual(lambda_handler(EVENT, CONTEXT)["statusCode"], 503)

def test_malformed_agent_response_returns_safe_502(self) -> None:
    agentcore.invoke.side_effect = InvalidAgentResponse()
    result = lambda_handler(EVENT, CONTEXT)
    self.assertEqual(result["statusCode"], 502)
    self.assertNotIn("AgentCore", result["body"])

def test_upstream_timeout_returns_indeterminate_503(self) -> None:
    agentcore.invoke.side_effect = AgentInvocationTimeout()
    result = lambda_handler(EVENT, CONTEXT)
    self.assertEqual(result["statusCode"], 503)
    self.assertEqual(
        json.loads(result["body"])["errorCode"],
        "settlement_status_unknown",
    )
```

Implement proxy output:

```python
return {
    "statusCode": envelope["status"],
    "headers": envelope["headers"],
    "isBase64Encoded": True,
    "body": envelope["bodyBase64"],
}
```

Log only JSON fields `requestId`, `route`, `status`, `outcome`, and
`durationMilliseconds`.

The timeout response must be retryable and must not tell the caller to create a
new proof. The asynchronous buyer already persists the exact proof before
create and retries it; the Agent's deterministic job identity and durable
`settling` record reconcile the retry without authorizing a second payment.

- [ ] **Step 7: Run the complete Lambda suite**

Run:

```bash
PYTHONPATH=gateway/x402_lambda/src \
python3 -m unittest discover -s gateway/x402_lambda/tests -p 'test_*.py'
```

Expected: all tests pass without AWS credentials or network access.

- [ ] **Step 8: Commit**

```bash
git add gateway/x402_lambda
git commit -m "feat: add x402 Lambda adapter"
```

---

### Task 4: Define the Gateway Infrastructure

**Files:**
- Create: `infra/x402-lambda-gateway.yaml`
- Create: `tests/infra/test_x402_lambda_gateway_template.py`

**Interfaces:**
- Consumes: packaged Lambda source; existing OAuth secret ARN; AgentCore invoke URL.
- Produces: stack output `X402GatewayBaseUrl`.

**Approved infrastructure correction (2026-07-30):** This supersedes the
earlier sample's Lambda public-base environment and SAM `Events` wiring. The
REST API owns an explicit OpenAPI definition containing the four fixed
method/path integrations and references the Lambda; an explicit
`AWS::Lambda::Permission` authorizes invocation. No Lambda property or
environment references `X402Api`, removing the API-to-Lambda dependency while
the output may still reference the API. WAF keeps the `/x402/` IP rate rule and
count-only AWS common rules, but has no body-size rule: WAF cannot accurately
represent the application's exact 256 KiB body limit. Lambda and Agent retain
that validation. Use `LoggingConfig.LogFormat: Text` so the safe stdout JSON
record is a root CloudWatch log event.

- [ ] **Step 1: Write failing static infrastructure tests**

Create tests that read the template text and assert:

```python
def test_template_has_only_four_api_events(self) -> None:
    text = TEMPLATE.read_text()
    self.assertIn("Path: /x402/price", text)
    self.assertIn("Path: /x402/analyze/async", text)
    self.assertIn("Path: /x402/jobs/{jobId}", text)
    self.assertIn("Path: /x402/jobs/{jobId}/resume", text)
    self.assertNotIn("{proxy+}", text)
    self.assertNotIn("Path: /x402/free", text)

def test_lambda_secret_policy_is_resource_scoped(self) -> None:
    text = TEMPLATE.read_text()
    self.assertIn("Resource: !Ref OAuthSecretArn", text)
    self.assertNotRegex(
        text,
        r"secretsmanager:[^\\n]+\\n\\s+Resource:\\s+[\"']?\\*[\"']?",
    )

def test_waf_is_associated_with_rest_stage(self) -> None:
    text = TEMPLATE.read_text()
    self.assertIn("AWS::WAFv2::WebACLAssociation", text)
    self.assertIn("AWS::Serverless::Api", text)
```

- [ ] **Step 2: Run tests and observe RED**

Run:

```bash
python3 -m unittest tests.infra.test_x402_lambda_gateway_template
```

Expected: failure because the template does not exist.

- [ ] **Step 3: Create the SAM/CloudFormation template**

Start with:

```yaml
AWSTemplateFormatVersion: "2010-09-09"
Transform: AWS::Serverless-2016-10-31
Description: Public x402 HTTP adapter for an existing AgentCore A2A Runtime

Parameters:
  StageName:
    Type: String
    Default: testnet
    AllowedPattern: "^[a-z0-9-]+$"
  AgentCoreInvokeUrl:
    Type: String
    AllowedPattern: "^https://bedrock-agentcore\\.[a-z0-9-]+\\.amazonaws\\.com/.+$"
  OAuthSecretArn:
    Type: String
    AllowedPattern: "^arn:[^:]+:secretsmanager:[^:]+:[0-9]{12}:secret:.+$"
  RateLimitPerFiveMinutes:
    Type: Number
    Default: 300
    MinValue: 100
  ReservedConcurrency:
    Type: Number
    Default: 10
    MinValue: 1
    MaxValue: 100

Resources:
  X402Api:
    Type: AWS::Serverless::Api
    Properties:
      Name: !Sub "${AWS::StackName}-api"
      StageName: !Ref StageName
      EndpointConfiguration: REGIONAL
      TracingEnabled: true
      AccessLogSetting:
        DestinationArn: !GetAtt X402ApiAccessLogGroup.Arn
        Format: >-
          {"requestId":"$context.requestId","route":"$context.httpMethod $context.resourcePath","status":"$context.status","latency":"$context.responseLatency","integrationStatus":"$context.integrationStatus"}
      MethodSettings:
        - ResourcePath: "/*"
          HttpMethod: "*"
          MetricsEnabled: true
          DataTraceEnabled: false
          ThrottlingBurstLimit: 50
          ThrottlingRateLimit: 25

  X402Adapter:
    Type: AWS::Serverless::Function
    Properties:
      FunctionName: !Sub "${AWS::StackName}-adapter"
      Runtime: python3.13
      Handler: handler.lambda_handler
      CodeUri: ../gateway/x402_lambda/src
      AutoPublishAlias: live
      Timeout: 28
      MemorySize: 256
      ReservedConcurrentExecutions: !Ref ReservedConcurrency
      Tracing: Active
      Environment:
        Variables:
          AGENTCORE_INVOKE_URL: !Ref AgentCoreInvokeUrl
          OAUTH_SECRET_ARN: !Ref OAuthSecretArn
          X402_PUBLIC_BASE_URL: !Sub
            "https://${X402Api}.execute-api.${AWS::Region}.${AWS::URLSuffix}/${StageName}"
      Policies:
        - Statement:
            - Effect: Allow
              Action: secretsmanager:GetSecretValue
              Resource: !Ref OAuthSecretArn
      Events:
        Price:
          Type: Api
          Properties:
            RestApiId: !Ref X402Api
            Path: /x402/price
            Method: GET
        Create:
          Type: Api
          Properties:
            RestApiId: !Ref X402Api
            Path: /x402/analyze/async
            Method: POST
        Status:
          Type: Api
          Properties:
            RestApiId: !Ref X402Api
            Path: /x402/jobs/{jobId}
            Method: GET
        Resume:
          Type: Api
          Properties:
            RestApiId: !Ref X402Api
            Path: /x402/jobs/{jobId}/resume
            Method: POST
```

Complete the template with these exact security resources and associations:

```yaml
  X402ApiAccessLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      LogGroupName: !Sub "/aws/apigateway/${AWS::StackName}"
      RetentionInDays: 30

  X402AdapterLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      LogGroupName: !Sub "/aws/lambda/${X402Adapter}"
      RetentionInDays: 30

  X402Gateway5xxMetric:
    Type: AWS::Logs::MetricFilter
    Properties:
      LogGroupName: !Ref X402AdapterLogGroup
      FilterPattern: '{ $.status >= 500 }'
      MetricTransformations:
        - MetricNamespace: Stockanalyst/X402Gateway
          MetricName: SafeGateway5xx
          MetricValue: "1"
          DefaultValue: 0

  X402WebAcl:
    Type: AWS::WAFv2::WebACL
    Properties:
      Name: !Sub "${AWS::StackName}-regional"
      Scope: REGIONAL
      DefaultAction: {Allow: {}}
      VisibilityConfig:
        CloudWatchMetricsEnabled: true
        MetricName: !Sub "${AWS::StackName}-waf"
        SampledRequestsEnabled: false
      Rules:
        - Name: X402RateLimit
          Priority: 0
          Action: {Block: {}}
          Statement:
            RateBasedStatement:
              AggregateKeyType: IP
              Limit: !Ref RateLimitPerFiveMinutes
              ScopeDownStatement:
                ByteMatchStatement:
                  FieldToMatch: {UriPath: {}}
                  PositionalConstraint: STARTS_WITH
                  SearchString: /x402/
                  TextTransformations:
                    - Priority: 0
                      Type: NONE
          VisibilityConfig:
            CloudWatchMetricsEnabled: true
            MetricName: x402-rate-limit
            SampledRequestsEnabled: false
        - Name: RejectOversizedBody
          Priority: 1
          Action: {Block: {}}
          Statement:
            SizeConstraintStatement:
              ComparisonOperator: GT
              Size: 262144
              FieldToMatch:
                Body:
                  OversizeHandling: MATCH
              TextTransformations:
                - Priority: 0
                  Type: NONE
          VisibilityConfig:
            CloudWatchMetricsEnabled: true
            MetricName: x402-body-size
            SampledRequestsEnabled: false
        - Name: AwsCommonRulesObserveOnly
          Priority: 2
          OverrideAction: {Count: {}}
          Statement:
            ManagedRuleGroupStatement:
              VendorName: AWS
              Name: AWSManagedRulesCommonRuleSet
          VisibilityConfig:
            CloudWatchMetricsEnabled: true
            MetricName: x402-common-rules-count
            SampledRequestsEnabled: false

  X402WebAclAssociation:
    Type: AWS::WAFv2::WebACLAssociation
    DependsOn: X402ApiStage
    Properties:
      ResourceArn: !Sub
        - "arn:${AWS::Partition}:apigateway:${AWS::Region}::/restapis/${ApiId}/stages/${Stage}"
        - ApiId: !Ref X402Api
          Stage: !Ref StageName
      WebACLArn: !GetAtt X402WebAcl.Arn

  X402LambdaErrors:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: !Sub "${AWS::StackName}-lambda-errors"
      Namespace: AWS/Lambda
      MetricName: Errors
      Dimensions:
        - Name: FunctionName
          Value: !Ref X402Adapter
      Statistic: Sum
      Period: 300
      EvaluationPeriods: 1
      Threshold: 1
      ComparisonOperator: GreaterThanOrEqualToThreshold
      TreatMissingData: notBreaching

  X402LambdaThrottles:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: !Sub "${AWS::StackName}-lambda-throttles"
      Namespace: AWS/Lambda
      MetricName: Throttles
      Dimensions:
        - Name: FunctionName
          Value: !Ref X402Adapter
      Statistic: Sum
      Period: 300
      EvaluationPeriods: 1
      Threshold: 1
      ComparisonOperator: GreaterThanOrEqualToThreshold
      TreatMissingData: notBreaching

  X402ApiServerErrors:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: !Sub "${AWS::StackName}-api-5xx"
      Namespace: AWS/ApiGateway
      MetricName: 5XXError
      Dimensions:
        - Name: ApiName
          Value: !Sub "${AWS::StackName}-api"
        - Name: Stage
          Value: !Ref StageName
      Statistic: Sum
      Period: 300
      EvaluationPeriods: 1
      Threshold: 1
      ComparisonOperator: GreaterThanOrEqualToThreshold
      TreatMissingData: notBreaching

Outputs:
  X402GatewayBaseUrl:
    Value: !Sub
      "https://${X402Api}.execute-api.${AWS::Region}.${AWS::URLSuffix}/${StageName}"
```

SAM generates the logical stage resource `X402ApiStage` for the explicitly
named `X402Api`; assert that `sam validate`/packaging resolves this dependency.
Keep Lambda outside a VPC. It needs normal managed internet egress to Cognito
and the AgentCore public endpoint and never calls B402.

API Gateway execution data tracing stays disabled. The access log format is
fixed to request ID, route, status, latency, and integration status; it contains
no request/response headers or bodies. The template owns only the destination
log group. It relies on an already configured region-wide API Gateway
CloudWatch role and must not create or replace that shared account setting.

- [ ] **Step 4: Run static tests and local template checks**

Run:

```bash
python3 -m unittest tests.infra.test_x402_lambda_gateway_template
sam validate --lint --template-file infra/x402-lambda-gateway.yaml
git diff --check -- infra/x402-lambda-gateway.yaml
```

Expected: tests and SAM lint pass and there are no whitespace errors.

- [ ] **Step 5: Package without deploying**

Use an operator-provided artifact bucket:

```bash
export AWS_PROFILE=dev
aws cloudformation package \
  --region us-east-1 \
  --template-file infra/x402-lambda-gateway.yaml \
  --s3-bucket "$X402_GATEWAY_ARTIFACT_BUCKET" \
  --s3-prefix x402-lambda-gateway/assets \
  --output-template-file /tmp/x402-lambda-gateway-packaged.yaml
```

Expected: packaging succeeds and the output template contains an S3 Lambda code
location but no secret value.

- [ ] **Step 6: Validate the packaged template**

```bash
export AWS_PROFILE=dev
aws cloudformation validate-template \
  --region us-east-1 \
  --template-body file:///tmp/x402-lambda-gateway-packaged.yaml \
  --query '{Description:Description,Parameters:Parameters[].ParameterKey}' \
  --output json
```

Expected: valid template metadata. If the current IAM principal still lacks
`cloudformation:ValidateTemplate`, record that exact limitation and use a
no-execute change set in Task 6; do not claim validation passed.

- [ ] **Step 7: Commit**

```bash
git add \
  infra/x402-lambda-gateway.yaml \
  tests/infra/test_x402_lambda_gateway_template.py
git commit -m "infra: define x402 Lambda gateway"
```

---

### Task 5: Add Cross-boundary Integration Tests and Operator Documentation

**Files:**
- Create: `gateway/x402_lambda/tests/test_gateway_integration.py`
- Create: `docs/x402-lambda-gateway.md`
- Modify: `buyer-client/README.md`
- Modify: `stockanalyst/README.md`

**Interfaces:**
- Consumes: Lambda adapter, A2A envelope bridge, infrastructure outputs.
- Produces: reproducible local proof and a secret-safe operator runbook.

- [ ] **Step 1: Write an in-process end-to-end integration test**

Build a fake AgentCore transport that:

1. receives the Lambda A2A JSON-RPC;
2. extracts the envelope data part;
3. calls `dispatch_x402_envelope` with a real `X402Handler` and fake job service;
4. wraps the response in an A2A result.

Test:

```python
def test_missing_payment_round_trip_returns_real_402(self) -> None:
    response = lambda_handler(
        api_event(
            method="POST",
            path="/x402/analyze/async",
            body=b'{"symbols":["AAPL"]}',
        ),
        CONTEXT,
    )
    self.assertEqual(response["statusCode"], 402)
    body = json.loads(base64.b64decode(response["body"]))
    self.assertEqual(body["error"], "Payment Required")

def test_valid_create_round_trip_returns_202(self) -> None:
    response = lambda_handler(
        api_event(
            method="POST",
            path="/x402/analyze/async",
            headers={"X-Payment": "test-proof"},
            body=b'{"symbols":["AAPL"]}',
        ),
        CONTEXT,
    )
    self.assertEqual(response["statusCode"], 202)
```

Use fakes only; do not use a real payment proof, AWS, B402, OpenRouter, or S3.

- [ ] **Step 2: Run all local suites**

Run:

```bash
cd stockanalyst/app/agent
uv run python -m unittest discover -s tests -p 'test_*.py'
uv run ruff check .

cd ../../../../
PYTHONPATH=gateway/x402_lambda/src:stockanalyst/app/agent \
python3 -m unittest discover -s gateway/x402_lambda/tests -p 'test_*.py'
python3 -m unittest tests.infra.test_x402_lambda_gateway_template

cd buyer-client
npm test
```

Expected: all suites pass.

- [ ] **Step 3: Write the operator runbook**

Document exact commands with these required properties:

- every AWS block starts with `export AWS_PROFILE=dev`;
- secrets are read into shell variables with `set +x` and never printed;
- a dedicated Cognito app client and Secrets Manager secret are used;
- Agent deploy uses `bag deploy`;
- CloudFormation packaging and deploy use a temporary packaged template;
- output URL retrieval uses `describe-stacks`;
- no-spend tests cover price and missing-payment 402;
- paid test is a separate explicitly approved section;
- rollback first disables new jobs, then API ingress, while preserving accepted
  job access windows.

Include a sanitized gateway credential secret shape but no real values.

- [ ] **Step 4: Update buyer and Agent README files**

Add these environment examples:

```dotenv
X402_ENDPOINT=https://<api-id>.execute-api.us-east-1.amazonaws.com/testnet
X402_SELLER_WALLET=0xd10BdDC20E4DC42A1a19a9653e994991e25b8153
```

Explain that `X402_ENDPOINT` is the API Gateway base URL, not the raw AgentCore
invocation URL, and only the four asynchronous paid routes are public.

- [ ] **Step 5: Scan documentation for unsafe instructions**

Run:

```bash
rg -n \
  'client_secret\\s*=|B402_SECRET\\s*=|OPENROUTER_API_KEY\\s*=|WALLET_PASSWORD\\s*=' \
  docs/x402-lambda-gateway.md buyer-client/README.md stockanalyst/README.md
```

Expected: only redacted example values or variable-name instructions; no
credential values.

- [ ] **Step 6: Commit**

```bash
git add \
  gateway/x402_lambda/tests/test_gateway_integration.py \
  docs/x402-lambda-gateway.md \
  buyer-client/README.md \
  stockanalyst/README.md
git commit -m "docs: add x402 gateway operations"
```

---

### Task 6: Deploy the Agent Repair and Gateway Without Spending U

**Files:**
- No new source files.
- Generated package: `/tmp/x402-lambda-gateway-packaged.yaml` only.
- Evidence: `.superpowers/sdd/x402-lambda-gateway-deployment-report.md` (ignored operational ledger).

**Interfaces:**
- Consumes: Tasks 1-5 commits; existing AgentCore Runtime; fixed-egress stack; private S3 bucket; dedicated OAuth secret.
- Produces: updated in-place Runtime, gateway stack, generated public base URL, no-spend smoke evidence.

- [ ] **Step 1: Audit pre-deployment repository state**

```bash
git status --short
git log --oneline -8
```

Expected: no tracked changes; preserve the user-owned untracked deployment
document and job 398 artifacts.

- [ ] **Step 2: Verify live dependencies read-only**

```bash
export AWS_PROFILE=dev
aws bedrock-agentcore-control get-agent-runtime \
  --region us-east-1 \
  --agent-runtime-id stockanalyst_stockanalyst-hrXlh1BUtQ \
  --query '{Status:status,Arn:agentRuntimeArn,Network:networkConfiguration}' \
  --output json
aws cloudformation describe-stacks \
  --region us-east-1 \
  --stack-name AgentCore-stockanalyst-fixed-egress \
  --query 'Stacks[0].{Status:StackStatus,Outputs:Outputs}' \
  --output json
aws apigateway get-account \
  --region us-east-1 \
  --query '{CloudWatchRoleArn:cloudwatchRoleArn}' \
  --output json
```

Expected: same Runtime ARN, `READY/VPC`, fixed stack healthy, EIP
`52.73.72.22`, and a non-empty API Gateway `CloudWatchRoleArn`. If the role is
empty, stop before deploying the gateway. Creating or changing this shared
regional setting requires a separately reviewed account-level change.

- [ ] **Step 3: Provision gateway OAuth credentials without printing them**

Use a dedicated Cognito client with only `bnbagent-seller/invoke`, then store
its four values in a dedicated Secrets Manager secret. Shell tracing must be
disabled and command output queries must exclude the client secret. First run a
read-only collision check:

```bash
export AWS_PROFILE=dev
set +x
aws cognito-idp list-user-pool-clients \
  --region us-east-1 \
  --user-pool-id us-east-1_aH4tfhMnq \
  --query 'UserPoolClients[?ClientName==`stockanalyst-x402-gateway`].{ClientId:ClientId,ClientName:ClientName}' \
  --output json
aws secretsmanager describe-secret \
  --region us-east-1 \
  --secret-id bnbagent/stockanalyst/x402-gateway-oauth \
  --query '{ARN:ARN,Name:Name}' \
  --output json
```

`ResourceNotFoundException` for the secret and an empty client array are the
expected create path. If either object exists, stop and inspect only its
metadata; reuse or rotate it through a separately reviewed change rather than
creating a duplicate.

Only on the empty create path:

```bash
export AWS_PROFILE=dev
set +x
GATEWAY_CLIENT_ID="$(
  aws cognito-idp create-user-pool-client \
    --region us-east-1 \
    --user-pool-id us-east-1_aH4tfhMnq \
    --client-name stockanalyst-x402-gateway \
    --generate-secret \
    --allowed-o-auth-flows client_credentials \
    --allowed-o-auth-scopes bnbagent-seller/invoke \
    --allowed-o-auth-flows-user-pool-client \
    --query 'UserPoolClient.ClientId' \
    --output text
)"
GATEWAY_CLIENT_SECRET="$(
  aws cognito-idp describe-user-pool-client \
    --region us-east-1 \
    --user-pool-id us-east-1_aH4tfhMnq \
    --client-id "$GATEWAY_CLIENT_ID" \
    --query 'UserPoolClient.ClientSecret' \
    --output text
)"
GATEWAY_SECRET_FILE="$(mktemp /tmp/stockanalyst-x402-oauth.XXXXXX)"
chmod 600 "$GATEWAY_SECRET_FILE"
trap 'rm -f "$GATEWAY_SECRET_FILE"' EXIT
jq -cn \
  --arg client_id "$GATEWAY_CLIENT_ID" \
  --arg client_secret "$GATEWAY_CLIENT_SECRET" \
  --arg token_url \
    'https://bnbagent-seller-201243086760.auth.us-east-1.amazoncognito.com/oauth2/token' \
  --arg scope 'bnbagent-seller/invoke' \
  '{client_id:$client_id,client_secret:$client_secret,token_url:$token_url,scope:$scope}' \
  > "$GATEWAY_SECRET_FILE"
X402_GATEWAY_OAUTH_SECRET_ARN="$(
  aws secretsmanager create-secret \
    --region us-east-1 \
    --name bnbagent/stockanalyst/x402-gateway-oauth \
    --secret-string "file://$GATEWAY_SECRET_FILE" \
    --query ARN \
    --output text
)"
test -n "$X402_GATEWAY_OAUTH_SECRET_ARN"
rm -f "$GATEWAY_SECRET_FILE"
trap - EXIT
unset GATEWAY_CLIENT_SECRET GATEWAY_SECRET_FILE
```

If secret creation fails after client creation, record the new client ID,
delete the unused client only after confirming it has never been distributed,
and restart the collision check. Never print the client secret.

- [ ] **Step 4: Configure Agent Runtime prerequisite secret names**

Load the sensitive values from the approved operator secret source with shell
tracing disabled, then set each key explicitly:

```bash
export AWS_PROFILE=dev
set +x
test -n "$X402_JOB_TOKEN_SECRET"
test -n "$B402_CLIENT_ID"
test -n "$B402_SECRET"
bag env set X402_SELLER_WALLET \
  0xd10BdDC20E4DC42A1a19a9653e994991e25b8153
bag env set X402_JOB_S3_BUCKET bnbagent-code-stock-analyst-agent
bag env set X402_JOB_S3_PREFIX x402/jobs/
bag env set X402_JOB_TOKEN_SECRET "$X402_JOB_TOKEN_SECRET"
bag env set B402_CLIENT_ID "$B402_CLIENT_ID"
bag env set B402_SECRET "$B402_SECRET"
unset X402_JOB_TOKEN_SECRET B402_CLIENT_ID B402_SECRET
```

Do not enable `X402_DEMO_MODE`. Use `bag env list` only if it redacts values;
otherwise verify the required key names through deployment metadata without
printing their values.

- [ ] **Step 5: Package and deploy the gateway stack**

```bash
export AWS_PROFILE=dev
aws cloudformation package \
  --region us-east-1 \
  --template-file infra/x402-lambda-gateway.yaml \
  --s3-bucket "$X402_GATEWAY_ARTIFACT_BUCKET" \
  --s3-prefix x402-lambda-gateway/assets \
  --output-template-file /tmp/x402-lambda-gateway-packaged.yaml
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name Stockanalyst-x402-gateway \
  --template-file /tmp/x402-lambda-gateway-packaged.yaml \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --parameter-overrides \
    StageName=testnet \
    AgentCoreInvokeUrl="$AGENTCORE_INVOKE_URL" \
    OAuthSecretArn="$X402_GATEWAY_OAUTH_SECRET_ARN" \
  --no-fail-on-empty-changeset
```

Expected: stack reaches `CREATE_COMPLETE` or `UPDATE_COMPLETE`. Do not send
public requests yet: the old Agent version does not understand the envelope.

- [ ] **Step 6: Capture the generated base URL**

```bash
export AWS_PROFILE=dev
X402_GATEWAY_BASE_URL="$(
  aws cloudformation describe-stacks \
    --region us-east-1 \
    --stack-name Stockanalyst-x402-gateway \
    --query 'Stacks[0].Outputs[?OutputKey==`X402GatewayBaseUrl`].OutputValue | [0]' \
    --output text
)"
test -n "$X402_GATEWAY_BASE_URL"
```

- [ ] **Step 7: Bind the URL and deploy the current Agent source in place**

Set `X402_GATEWAY_PUBLIC_BASE_URL`, then deploy from
`stockanalyst/app/agent` with the same approved flags used by the existing
self-hosted runtime:

```bash
export AWS_PROFILE=dev
bag env set X402_GATEWAY_PUBLIC_BASE_URL "$X402_GATEWAY_BASE_URL"
bag deploy agent \
  --project-root /Users/zhaoyu/corp/bnbchain/chain_middleware/stockanalyst-agent-demo/stockanalyst/app/agent \
  --skip-prepare \
  --accept-risk \
  --force \
  --force-deploy-broken-storage
```

Expected: same Runtime ID/ARN; application stack update completes; Runtime
returns to `READY/VPC`.

- [ ] **Step 8: Invoke the envelope bridge directly without payment**

Obtain a transient OAuth token and send one `x402_http_envelope` A2A call for
`POST /x402/analyze/async` without `x-payment`.

Expected inner envelope:

```json
{
  "status": 402,
  "headers": {
    "content-type": "application/json"
  }
}
```

Do not print the token. Confirm CloudWatch contains no secret and no B402 call
was attempted.

- [ ] **Step 9: Run no-spend public smoke tests**

```bash
curl --silent --show-error --fail \
  "$X402_GATEWAY_BASE_URL/x402/price" |
  jq '{x402Version,price_u,network,asset,payTo}'

curl --silent --show-error \
  --output /tmp/x402-missing-payment.json \
  --write-out '%{http_code}\\n' \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{"symbols":["AAPL"]}' \
  "$X402_GATEWAY_BASE_URL/x402/analyze/async"
```

Expected: price returns 200 with `payTo` equal to
`0xd10BdDC20E4DC42A1a19a9653e994991e25b8153`; create returns HTTP 402. No U is
spent.

- [ ] **Step 10: Verify logs, resources, and regression boundaries**

Confirm:

- API Gateway and Lambda safe request IDs correlate with AgentCore;
- no payment proof or job token appears in logs;
- no B402 settlement occurred;
- Runtime remains the same `READY/VPC` Runtime;
- NAT remains available with EIP `52.73.72.22`;
- existing A2A `negotiate` still returns a signature from
  `0xd10BdDC20E4DC42A1a19a9653e994991e25b8153`;
- job 398 remains unchanged.

- [ ] **Step 11: Record sanitized evidence and commit deploy metadata**

Record resource IDs, statuses, route results, and safe log counts in the
ignored SDD report. Commit only deployment-generated tracked metadata:

```bash
git add stockanalyst/app/agent/studio.toml
git commit -m "chore: record x402 gateway deployment"
```

---

### Task 7: Run One Explicitly Authorized Paid End-to-End Test

**Files:**
- No source changes expected.
- Evidence: append to `.superpowers/sdd/x402-lambda-gateway-deployment-report.md`.

**Interfaces:**
- Consumes: healthy gateway and Runtime; B402 whitelist; buyer wallet with 1 U plus testnet gas if required.
- Produces: one paid report and correlated settlement/generation/storage evidence.

- [ ] **Step 1: Stop and obtain explicit approval**

Ask the user to approve exactly one 1 U BSC Testnet x402 payment. Do not infer
approval from prior tests or from approval to deploy infrastructure.

- [ ] **Step 2: Verify the payment destination and balance**

Read `/x402/price` and require:

- network `eip155:97`;
- token `0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565`;
- `payTo` `0xd10BdDC20E4DC42A1a19a9653e994991e25b8153`;
- amount `1000000000000000000`.

Abort on any mismatch.

- [ ] **Step 3: Run exactly one buyer flow**

Set:

```dotenv
X402_ENDPOINT=<X402GatewayBaseUrl>
X402_SELLER_WALLET=0xd10BdDC20E4DC42A1a19a9653e994991e25b8153
```

Then run:

```bash
cd buyer-client
npm run x402:async
```

Do not rerun automatically if settlement is indeterminate. Use the durable
pending record and documented recovery path.

- [ ] **Step 4: Verify end-to-end evidence**

Correlate without exposing secrets:

- API Gateway final statuses;
- Lambda safe request ID;
- AgentCore envelope receipt;
- B402 success and transaction hash;
- BSC Testnet authorization consumption;
- private S3 job state transitions;
- OpenRouter report generation;
- S3 report object and presigned download;
- locally saved HTML/PDF;
- Binance-observed source IP `52.73.72.22`.

- [ ] **Step 5: Preserve rollback and job access**

If the test fails after payment, do not delete the gateway, Cognito client,
Lambda, Agent Runtime secret, private S3 bucket, or job-token secret. Keep the
resume and polling paths available until the accepted job reaches a terminal
state and its access window closes.

- [ ] **Step 6: Final regression and audit**

Run all Python, Lambda, infrastructure, and buyer tests again. Verify no tracked
secrets, no staged unrelated files, unchanged job 398, and no claim of success
unless B402, S3, report content, and the payment authorization all correlate.

---

## Final Review Gate

After Task 7, run an independent whole-feature review from the commit before
Task 1 through final HEAD. The reviewer must check:

- public route/auth semantics;
- envelope request/response validation;
- no arbitrary path or header forwarding;
- no secret or sensitive payload logging;
- seller-wallet consistency;
- payment idempotency and indeterminate recovery;
- AgentCore A2A regression safety;
- Lambda least privilege;
- WAF and API Gateway route scope;
- Runtime in-place update and fixed egress;
- job 398 preservation;
- accurate distinction between no-spend smoke success and paid B402 success.
