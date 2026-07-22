# Secure notify_funded Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Require the on-chain job client to authorize every notify_funded context, make that context immutable across retries, and prevent delivery gateways or portfolio data from becoming SSRF and prompt-injection inputs.

**Architecture:** Add a focused Python security module that verifies EIP-712 authorization, validates signed context, and enforces gateway policy. SellerCore installs one immutable context only after funded-job and wallet checks pass; the buyer signs the exact context string with its job wallet. The UOMP transport independently rechecks its destination and uses a no-redirect, TLS-verifying HTTP client.

**Tech Stack:** Python 3.10+, unittest, eth-account, asyncio, urllib, TypeScript 5, ethers v6, and Node's built-in test runner.

## Global Constraints

- Unsigned legacy named notifications are rejected; backward compatibility is not required.
- Never move, copy, print, or inspect .studio/wallets. Tests use a public fixed test-only key.
- Signing remains fixed entrypoint code and is never exposed as an LLM tool.
- OAuth remains transport access control, not proof of blockchain-wallet ownership.
- Production gateway policy is fail-closed. Loopback HTTP is allowed only with ALLOW_PRIVATE_DELIVERY_GATEWAY=true.
- Do not change smart contracts, quote pricing, settlement, or default-storage sweep behavior.
- Every production-code change follows a witnessed RED test before GREEN implementation.

---

## File map

- Create stockanalyst/app/agent/notify_security.py for immutable context types, schema validation, EIP-712 recovery, and gateway policy.
- Create stockanalyst/app/agent/tests/test_notify_security.py for pure security tests.
- Create stockanalyst/app/agent/tests/fixtures/notify_auth_vector.json as the cross-language EIP-712 fixture.
- Create stockanalyst/app/agent/tests/test_seller_core_notify.py for authorization, mutation, idempotency, and retry tests.
- Create stockanalyst/app/agent/tests/test_uomp_storage.py for redirect, TLS, response-bound, and payload-ID tests.
- Create buyer-client/src/notify-auth.ts and its test for exact serialization and ethers signing.
- Modify seller_core.py, signing.py, uomp_storage.py, and the agent pyproject.
- Modify buyer negotiate.ts, index.ts, package.json, and package-lock.json.
- Modify the agent card and READMEs to describe the strict protocol.

---

### Task 1: Strict signed-context primitives

**Files:**
- Create: stockanalyst/app/agent/notify_security.py
- Create: stockanalyst/app/agent/tests/__init__.py
- Create: stockanalyst/app/agent/tests/fixtures/notify_auth_vector.json
- Create: stockanalyst/app/agent/tests/test_notify_security.py
- Modify: stockanalyst/app/agent/pyproject.toml

**Interfaces:**
- Produces NotifySecurityError(code: str) with a stable .code.
- Produces frozen Holding, RiskProfile, and JobContext dataclasses.
- Produces parse_signed_context(raw: str) -> JobContext.
- Produces build_notify_typed_data(*, job_id: int, context: str, expires_at: int, nonce: str, chain_id: int, verifying_contract: str) -> dict[str, object].
- Produces verify_notify_authorization(authorization: object, *, job_id: int, expected_client: str, chain_id: int, verifying_contract: str, now: int | None = None) -> JobContext.

- [ ] **Step 1: Write failing schema tests**

Create test_notify_security.py with table-driven cases for valid context and rejection of unknown fields, control characters, booleans masquerading as numbers, non-finite numbers, invalid tickers, unsupported indicators, excessive holdings, and invalid risk tolerance.

~~~python
class ContextTests(unittest.TestCase):
    def test_parses_valid_context_into_immutable_values(self):
        raw = json.dumps({
            "delivery_gateway_url": "https://buyer.trycloudflare.com",
            "delivery_gateway_token": "relay-token",
            "portfolio": [{
                "symbol": "AAPL", "shares": 10,
                "avgCost": 190.25, "currency": "USD",
            }],
            "risk_profile": {
                "tolerance": "moderate",
                "horizonMonths": 12,
                "preferredIndicators": ["RSI-14", "MACD"],
            },
        }, separators=(",", ":"))

        context = parse_signed_context(raw)

        self.assertEqual(context.gateway_url, "https://buyer.trycloudflare.com")
        self.assertEqual(context.portfolio[0].symbol, "AAPL")
        self.assertEqual(context.digest, hashlib.sha256(raw.encode()).hexdigest())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            context.gateway_url = "https://attacker.example"
~~~

Test exact limits: signed context at most 65,536 UTF-8 bytes; gateway URL at most 2,048 characters; token from 1 through 2,048 characters with no CR/LF; at most 50 holdings; symbol regex [A-Z][A-Z0-9.-]{0,9}; shares in (0, 1e12]; average cost in [0, 1e12]; currency regex [A-Z]{3,8}; horizon in [1, 600]; and indicators drawn from RSI-14, MACD, Bollinger Bands, MA50/200, ADX, OBV, ATR, and VaR without duplicates.

- [ ] **Step 2: Run the schema test and witness RED**

~~~bash
uv run --isolated --with 'eth-account>=0.13,<1' \
  python -m unittest stockanalyst/app/agent/tests/test_notify_security.py -v
~~~

Expected: import failure for notify_security or a missing parse_signed_context, not a syntax error.

- [ ] **Step 3: Implement immutable schema parsing**

Create exact-key allowlists. Reject bool for numeric fields, use math.isfinite, reject CR/LF in the token, and allocate immutable tuples.

~~~python
@dataclass(frozen=True)
class JobContext:
    digest: str
    gateway_url: str | None
    gateway_token: str | None
    portfolio: tuple[Holding, ...]
    risk_profile: RiskProfile | None
~~~

Implement portfolio_for_prompt and risk_profile_for_prompt as concrete methods
that allocate fresh dictionaries from every frozen field; neither method returns
the internal tuple or a caller-owned object.

All failures raise NotifySecurityError("invalid_context") without embedding raw values.

- [ ] **Step 4: Run schema tests and witness GREEN**

Run Step 2. Expected: all context tests pass.

- [ ] **Step 5: Write failing EIP-712 tests**

Use a fixed test-only key and eth-account. Test valid recovery, a different wallet, expiry, excessive future expiry, malformed nonce/signature, context mutation, and server-owned domain mutation.

~~~python
TEST_KEY = "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
account = Account.from_key(TEST_KEY)
typed = build_notify_typed_data(
    job_id=2**60 + 7,
    context=raw,
    expires_at=1_800_000_300,
    nonce="0x" + "11" * 32,
    chain_id=97,
    verifying_contract="0xa206c0517b6371c6638cd9e4a42cc9f02a33b0de",
)
signature = Account.sign_message(
    encode_typed_data(full_message=typed), TEST_KEY
).signature.hex()
result = verify_notify_authorization(
    {"context": raw, "expires_at": 1_800_000_300,
     "nonce": "0x" + "11" * 32, "signature": signature},
    job_id=2**60 + 7,
    expected_client=account.address,
    chain_id=97,
    verifying_contract="0xa206c0517b6371c6638cd9e4a42cc9f02a33b0de",
    now=1_800_000_000,
)
self.assertEqual(result.digest, hashlib.sha256(raw.encode()).hexdigest())
~~~

- [ ] **Step 6: Run authorization tests and witness RED**

Run Step 2. Expected: typed-data construction or authorization verification is missing.

- [ ] **Step 7: Implement EIP-712 verification**

Use domain name stockanalyst-notify-funded, version 1, server-provided chain ID and Commerce address, and primary fields jobId uint256, context string, expiresAt uint64, and nonce bytes32. Normalize addresses with eth_utils.to_checksum_address. Allow at most 30 seconds of past clock skew and reject expiry more than 600 seconds ahead. Stable error codes are authorization_required, authorization_expired, invalid_authorization, and caller_not_job_client.

Add the direct dependency to pyproject.toml:

~~~toml
"eth-account>=0.13,<1",
~~~

- [ ] **Step 8: Run security tests and witness GREEN**

Run Step 2. Expected: all tests pass without warnings.

- [ ] **Step 9: Freeze the cross-language vector**

Store the test key, expected address, domain, job ID as a decimal string, exact
context, expiry, nonce, and deterministic signature used by the passing Python
test in tests/fixtures/notify_auth_vector.json. Reload that JSON in the Python
test and verify its signature rather than retaining duplicated literals. This is
test-only public data and must not reference the project's wallet.

- [ ] **Step 10: Commit Task 1**

~~~bash
git add stockanalyst/app/agent/notify_security.py \
  stockanalyst/app/agent/tests/__init__.py \
  stockanalyst/app/agent/tests/fixtures/notify_auth_vector.json \
  stockanalyst/app/agent/tests/test_notify_security.py \
  stockanalyst/app/agent/pyproject.toml
git commit -m "feat: verify signed notify contexts"
~~~

---

### Task 2: Bind notifications to the on-chain client and immutable state

**Files:**
- Modify: stockanalyst/app/agent/signing.py:148-175
- Modify: stockanalyst/app/agent/seller_core.py:74-314
- Create: stockanalyst/app/agent/tests/test_seller_core_notify.py

**Interfaces:**
- Consumes verify_notify_authorization and JobContext from Task 1.
- Produces frozen signing.JobAuthorizationTarget(client: str, chain_id: int, verifying_contract: str).
- Produces signing.job_authorization_target(job_id: int) -> JobAuthorizationTarget.
- Replaces _job_gateways and _job_portfolios with _job_contexts: dict[int, JobContext].

- [ ] **Step 1: Write failing fail-closed and ownership tests**

Use unittest.IsolatedAsyncioTestCase, patch signing verification/target calls, and subclass SellerCore to record spawned jobs while closing the unused sweep coroutine.

~~~python
async def test_unsigned_named_notification_is_rejected_without_state(self):
    result = await self.core.notify_funded({"job_id": 42})
    self.assertEqual(result["status"], "rejected")
    self.assertEqual(result["reason"], "authorization_required")
    self.assertEqual(self.core._job_contexts, {})
    self.assertEqual(self.core.spawned_jobs, [])

~~~

Add four separate complete test methods. Each constructs its signed request with
the shared helper, patches exactly one boundary (wrong account, permanent
verification tuple, transient verification tuple, or asyncio timeout), calls
notify_funded, and asserts both dictionaries/task recordings remain empty.

Transient failures and timeouts return a retryable rejection and never preserve context or start work.

- [ ] **Step 2: Run focused seller tests and witness RED**

~~~bash
uv run --project stockanalyst/app/agent \
  python -m unittest stockanalyst/app/agent/tests/test_seller_core_notify.py -v
~~~

Expected: unsigned calls are accepted or _job_contexts is missing.

- [ ] **Step 3: Add the on-chain authorization target**

~~~python
@dataclass(frozen=True)
class JobAuthorizationTarget:
    client: str
    chain_id: int
    verifying_contract: str

def job_authorization_target(job_id: int) -> JobAuthorizationTarget:
    client = get_8183_client()
    job = client.get_job(job_id)
    return JobAuthorizationTarget(
        client=str(job.client),
        chain_id=int(client.network.chain_id),
        verifying_contract=str(client.commerce.address),
    )
~~~

Never accept domain values from the request.

- [ ] **Step 4: Implement fail-closed authorization before mutation**

For a named job: reject legacy top-level context; require authorization; run funded/seller-signature verification; reject transient failure or timeout as verification_unavailable; load the chain-owned target; verify the wallet signature; validate context; then and only then install state and spawn work. Bare sweep calls remain context-free.

- [ ] **Step 5: Run focused tests and witness GREEN**

Run Step 2. Expected: fail-closed and ownership tests pass.

- [ ] **Step 6: Write failing idempotency and lifecycle tests**

Write five separate complete tests named
test_identical_signed_context_is_idempotent,
test_conflicting_signed_context_is_rejected,
test_background_worker_reads_context_without_popping_it,
test_transient_delivery_keeps_context_for_retry, and
test_terminal_delivery_removes_context. Use AsyncMock for the work/submit
boundaries and assert the exact digest remains or is removed after each outcome.

Both conflicting contexts may be signed by the legitimate client; the first authorized digest wins and the second returns context_conflict.

- [ ] **Step 7: Run lifecycle tests and witness RED**

Run Step 2. Expected: current pop-at-start behavior loses context or allows replacement.

- [ ] **Step 8: Implement compare-and-set lifecycle**

Install by synchronous digest compare-and-set. _do_work_and_submit reads the stored context and converts it through the prompt helpers. _run_job removes context only for terminal success/permanent skip; transient errors and timeouts keep it for a later sweep. Never log context, token, portfolio, or signature.

- [ ] **Step 9: Run all current Python tests and witness GREEN**

~~~bash
uv run --project stockanalyst/app/agent \
  python -m unittest discover -s stockanalyst/app/agent/tests -p 'test_*.py' -v
~~~

Expected: all tests pass.

- [ ] **Step 10: Commit Task 2**

~~~bash
git add stockanalyst/app/agent/signing.py \
  stockanalyst/app/agent/seller_core.py \
  stockanalyst/app/agent/tests/test_seller_core_notify.py
git commit -m "fix: bind funded notifications to job buyers"
~~~

---

### Task 3: Enforce SSRF-safe origins and outbound HTTP

**Files:**
- Modify: stockanalyst/app/agent/notify_security.py
- Modify: stockanalyst/app/agent/seller_core.py:264-304
- Modify: stockanalyst/app/agent/uomp_storage.py
- Modify: stockanalyst/app/agent/tests/test_notify_security.py
- Create: stockanalyst/app/agent/tests/test_uomp_storage.py

**Interfaces:**
- Produces validate_gateway_url(url: str, *, allow_private: bool | None = None, resolver: Callable | None = None) -> str.
- UOMPGatewayStorageProvider consumes and rechecks a normalized origin; its download and exists methods accept only same-origin /v1/payload/<valid-id> URLs.

- [ ] **Step 1: Write failing gateway policy tests**

Reject production HTTP, credentials, non-root paths, query, fragment, non-default ports, localhost, IPv4/IPv6 loopback, RFC1918, link-local, unspecified, multicast, reserved, and DNS responses containing any non-global address. Patch DNS in every test.

~~~python
def test_development_flag_allows_only_http_loopback_origin(self):
    self.assertEqual(
        validate_gateway_url(
            "http://127.0.0.1:9444", allow_private=True,
            resolver=lambda *args: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 9444))
            ],
        ),
        "http://127.0.0.1:9444",
    )
    with self.assertRaises(NotifySecurityError):
        validate_gateway_url("http://169.254.169.254", allow_private=True)
~~~

Default allowed suffix is trycloudflare.com. DELIVERY_GATEWAY_ALLOWED_HOSTS may contain comma-separated operator-approved exact hosts or dot-delimited suffixes.

- [ ] **Step 2: Run gateway tests and witness RED**

Run Task 1 Step 2. Expected: validate_gateway_url is missing.

- [ ] **Step 3: Implement origin/address policy**

Use urlsplit, getaddrinfo, and ip_address. All production answers must be globally routable. Reject forbidden components instead of normalizing them away. Read ALLOW_PRIVATE_DELIVERY_GATEWAY only when the explicit argument is None.

- [ ] **Step 4: Run gateway tests and witness GREEN**

Run Task 1 Step 2. Expected: all security tests pass.

- [ ] **Step 5: Write failing UOMP transport tests**

Inject fake resolvers/openers. Prove redirects return None; default SSL context remains verified; constructor rejects unsafe origins; upload response is bounded to 64 KiB; response is JSON; payload ID matches [A-Za-z0-9_-]{1,128}; and returned URL uses only the validated origin.

~~~python
async def test_upload_rejects_invalid_payload_id(self):
    provider = UOMPGatewayStorageProvider(
        "https://buyer.trycloudflare.com", "token",
        resolver=PUBLIC_RESOLVER,
        opener=FakeOpener(b'{"payload_id":"../../admin"}'),
    )
    with self.assertRaises(ValueError):
        await provider.upload({"response": {"content": "report"}})
~~~

- [ ] **Step 6: Run UOMP tests and witness RED**

~~~bash
uv run --project stockanalyst/app/agent \
  python -m unittest stockanalyst/app/agent/tests/test_uomp_storage.py -v
~~~

Expected: redirect/TLS/constructor tests fail against current code.

- [ ] **Step 7: Implement the hardened client**

Remove CERT_NONE and hostname disabling. Build an opener with HTTPSHandler(default SSL context) and a redirect handler returning None. Read at most 65,537 bytes, reject overflow, parse JSON, and validate payload ID. Keep existing timeouts. Validate in SellerCore before context installation and again in the provider constructor. For download and exists, parse the requested URL, require the provider's exact scheme/host/port and a single valid payload ID under /v1/payload/, and use the same no-redirect opener.

- [ ] **Step 8: Run all Python tests and witness GREEN**

Run Task 2 Step 9. Expected: all pass without SSL warnings.

- [ ] **Step 9: Commit Task 3**

~~~bash
git add stockanalyst/app/agent/notify_security.py \
  stockanalyst/app/agent/seller_core.py \
  stockanalyst/app/agent/uomp_storage.py \
  stockanalyst/app/agent/tests/test_notify_security.py \
  stockanalyst/app/agent/tests/test_uomp_storage.py
git commit -m "fix: harden deliverable gateway requests"
~~~

---

### Task 4: Sign notifications in the buyer client

**Files:**
- Create: buyer-client/src/notify-auth.ts
- Create: buyer-client/src/notify-auth.test.ts
- Modify: buyer-client/src/negotiate.ts:189-235
- Modify: buyer-client/src/index.ts:142-149
- Modify: buyer-client/package.json
- Modify: buyer-client/package-lock.json

**Interfaces:**
- Produces buildNotifyContext(options: NotifyOptions) -> string.
- Produces createNotifyAuthorization(signer: Signer, jobId: bigint, context: string, options?: {nowSeconds?: number; nonce?: string}) -> Promise<NotifyAuthorization>.
- Changes notifyFunded(endpoint: string, signer: Signer, jobId: bigint, options?: NotifyOptions) -> Promise<string>.

- [ ] **Step 1: Write failing typed-data tests**

Use node:test, the shared notify_auth_vector.json fixture, ethers Wallet,
verifyTypedData, and the fixture's job ID converted directly to bigint. Assert
that ethers produces the same deterministic signature as Python and recovers
the fixture's expected address.

~~~typescript
test("signs exact context for a large job id", async () => {
  const wallet = new Wallet(vector.private_key);
  const context = buildNotifyContext({
    portfolio: [{ symbol: "AAPL", shares: 10, avgCost: 190.25, currency: "USD" }],
    riskProfile: {
      tolerance: "moderate", horizonMonths: 12,
      preferredIndicators: ["RSI-14"],
    },
  });
  const auth = await createNotifyAuthorization(wallet, BigInt(vector.job_id), context, {
    nowSeconds: vector.now,
    nonce: vector.nonce,
  });
  assert.equal(auth.signature, vector.signature);
  assert.equal(recoverNotifySigner(BigInt(vector.job_id), auth), vector.expected_address);
});
~~~

Add package script: "test": "npm run build && node --test dist/*.test.js".

- [ ] **Step 2: Install locked dependencies and witness RED**

From buyer-client:

~~~bash
npm ci --ignore-scripts
npm test
~~~

Expected: notify-auth module or exports are missing.

- [ ] **Step 3: Implement typed data and signer**

Use the Task 1 domain/fields. Import chain ID and Commerce address from erc8183.ts. Serialize once with JSON.stringify, generate a 32-byte nonce, and default expiry to now plus 300 seconds. Return only context, expires_at, nonce, and signature.

- [ ] **Step 4: Run buyer tests and witness GREEN**

Run npm test. Expected: typed-data tests pass.

- [ ] **Step 5: Write failing request-envelope test**

Stub fetch, call notifyFunded, parse the body, and assert:

~~~typescript
assert.equal(data.job_id, (2n ** 60n + 7n).toString());
assert.deepEqual(Object.keys(data).sort(), ["authorization", "job_id", "skill"]);
assert.equal(data.authorization.context, expectedContext);
assert.equal("portfolio" in data, false);
assert.equal("delivery_gateway_token" in data, false);
~~~

- [ ] **Step 6: Run request test and witness RED**

Run npm test. Expected: current code narrows jobId to Number and sends unsigned top-level fields.

- [ ] **Step 7: Update notifyFunded and its caller**

Move or re-export NotifyOptions without a circular import. Build/sign one context and send only the authorization envelope. Pass the existing decrypted wallet from index.ts.

~~~typescript
const notifyStatus = await notifyFunded(AGENT_ENDPOINT, wallet, buy.jobId, {
  gatewayUrl: relay?.publicUrl,
  gatewayToken: relay?.token,
  portfolio,
  riskProfile,
});
~~~

Do not log context, token, or signature.

- [ ] **Step 8: Run tests and build and witness GREEN**

~~~bash
npm test
npm run build
~~~

Expected: tests pass and tsc reports no errors.

- [ ] **Step 9: Commit Task 4**

~~~bash
git add buyer-client/src/notify-auth.ts \
  buyer-client/src/notify-auth.test.ts \
  buyer-client/src/negotiate.ts buyer-client/src/index.ts \
  buyer-client/package.json buyer-client/package-lock.json
git commit -m "feat: sign funded notifications with buyer wallet"
~~~

---

### Task 5: Documentation and complete verification

**Files:**
- Modify: stockanalyst/app/agent/agent_card.py:48-64
- Modify: README.md
- Modify: stockanalyst/README.md
- Modify: buyer-client/README.md
- Modify: stockanalyst/test_e2e.py:151-166

**Interfaces:**
- Consumes the strict signed protocol from Tasks 1-4.
- Produces discoverable protocol and local development documentation.

- [ ] **Step 1: Write a failing card-description assertion**

Add a focused card test asserting that the notify_funded description contains EIP-712, job client, and authorization and no longer describes {skill, job_id} alone as sufficient.

- [ ] **Step 2: Run it and witness RED**

Run Task 2 Step 9. Expected: the card advertises unsigned notification.

- [ ] **Step 3: Update public documentation**

Document strict buyer signatures, exact signed context, HTTPS trycloudflare.com production default, ALLOW_PRIVATE_DELIVERY_GATEWAY=true for local loopback, and DELIVERY_GATEWAY_ALLOWED_HOSTS for approved origins. Replace unsigned examples with the signing helper. Never add credentials or wallet material.

The Python E2E currently has no buyer typed-data signer and carries no
personalized off-chain context. Change its step 3 request to the context-free
bare sweep form {"skill": "notify_funded"}, update the step label to say it is
requesting a funded-job sweep, and keep the TypeScript buyer client as the E2E
exercise of the signed named-job protocol. Do not retain an unsigned named-job
example anywhere.

- [ ] **Step 4: Run all offline verification**

From repository root:

~~~bash
uv run --project stockanalyst/app/agent \
  python -m unittest discover -s stockanalyst/app/agent/tests -p 'test_*.py' -v
npm --prefix buyer-client test
npm --prefix buyer-client run build
git diff --check
~~~

Expected: all tests pass, TypeScript builds, and diff check is clean. Do not run live funded-chain E2E unless external services and test funds are already available.

- [ ] **Step 5: Review final security properties**

Confirm no state write precedes chain/signature validation; request domain values cannot affect recovery; conflict cannot overwrite; transient retry keeps context; UOMP uses validated origins, verified TLS, and no redirects; prompt input is normalized; and logs exclude context/token/portfolio/signature.

- [ ] **Step 6: Commit Task 5**

~~~bash
git add stockanalyst/app/agent/agent_card.py \
  stockanalyst/app/agent/tests \
  README.md stockanalyst/README.md buyer-client/README.md \
  stockanalyst/test_e2e.py
git commit -m "docs: describe authenticated delivery protocol"
~~~

- [ ] **Step 7: Record final status**

~~~bash
git status --short
git log -6 --oneline
~~~

Expected: no uncommitted implementation files and the design, plan, and four implementation commits are visible.
