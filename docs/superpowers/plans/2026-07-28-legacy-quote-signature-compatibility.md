# Legacy Quote Signature Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verify quotes affected by the legacy SDK `success_criteria` string-normalization bug, update the existing AgentCore runtime, and finish funded job `398` without creating another job.

**Architecture:** Add a project-local recovery function that first delegates to the SDK's normal verifier and then applies one narrowly gated legacy canonicalization. Wire the funded-job snapshot to that function without changing its other checks. Deploy in place and resume only `notify_funded`, polling, delivery verification, rendering, and settlement for job `398`.

**Tech Stack:** Python 3.14, `bnbagent`, `bnbagent-studio-core`, `eth-account`, Web3.py, `unittest`, AWS AgentCore, Cognito, BSC Testnet, TypeScript/ethers, S3, CloudFront.

## Global Constraints

- The fallback is available only when `terms.success_criteria` is exactly `uomp_notify_context_required_v1`.
- Normal SDK signature recovery always runs first.
- The fallback must still require exact negotiation-hash equality and EIP-191 recovery to the configured seller.
- Do not weaken job status, provider, expiry, budget, buyer authorization, chain, contract, or gateway checks.
- Use the existing runtime `stockanalyst_stockanalyst-hrXlh1BUtQ`; do not create another runtime or use the managed bnbagent-studio platform.
- Every AWS CLI command must be preceded by `export AWS_PROFILE=dev`.
- Do not create or fund another job. Recovery applies only to job `398`.
- Never print wallet passwords, private keys, Cognito client secrets, or OAuth access tokens.

---

### Task 1: Add Strict Legacy Signature Recovery

**Files:**
- Create: `stockanalyst/app/agent/tests/test_signing_compat.py`
- Modify: `stockanalyst/app/agent/signing.py`

**Interfaces:**
- Consumes: `bnbagent_studio_core.erc8183.verify.recover_quote_signer(description: str) -> str | None`
- Produces: `recover_quote_signer_compat(description: str) -> str | None`

- [ ] **Step 1: Write the failing compatibility tests**

Create `stockanalyst/app/agent/tests/test_signing_compat.py` with helpers that
construct and sign real canonical descriptions:

```python
from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

from stockanalyst.app.agent import signing

MARKER = "uomp_notify_context_required_v1"


def _description(
    *,
    stored_marker: str = MARKER,
    signed_marker: object | None = None,
) -> tuple[str, str]:
    account = Account.create()
    content = {
        "version": 1,
        "negotiated_at": 1_785_224_000,
        "task": "analyse AAPL and NVDA",
        "terms": {
            "deliverables": "report",
            "quality_standards": "cite sources",
            "success_criteria": stored_marker,
        },
        "price": "1000000000000000000",
        "currency": "0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565",
        "quote_expires_at": 1_785_224_900,
        "chain_id": 97,
        "verifying_contract": "0xa206c0517B6371C6638CD9e4a42Cc9f02A33B0DE",
    }
    signed = copy.deepcopy(content)
    if signed_marker is not None:
        signed["terms"]["success_criteria"] = signed_marker
    canonical = json.dumps(signed, sort_keys=True, separators=(",", ":"))
    negotiation_hash = Web3.keccak(text=canonical).hex()
    signature = Account.sign_message(
        encode_defunct(text=negotiation_hash),
        private_key=account.key,
    ).signature.hex()
    content["negotiation_hash"] = negotiation_hash
    content["provider_sig"] = signature
    return json.dumps(content, sort_keys=True, separators=(",", ":")), account.address


class QuoteSignatureCompatibilityTests(unittest.TestCase):
    def test_normal_sdk_recovery_is_preferred(self) -> None:
        with patch(
            "bnbagent_studio_core.erc8183.verify.recover_quote_signer",
            return_value="0x1111111111111111111111111111111111111111",
        ) as normal:
            recovered = signing.recover_quote_signer_compat("{}")
        self.assertEqual(recovered, "0x1111111111111111111111111111111111111111")
        normal.assert_called_once_with("{}")

    def test_recovers_legacy_marker_character_list_signature(self) -> None:
        description, expected = _description(signed_marker=list(MARKER))
        self.assertEqual(
            signing.recover_quote_signer_compat(description).lower(),
            expected.lower(),
        )

    def test_rejects_tampered_signed_field(self) -> None:
        description, _ = _description(signed_marker=list(MARKER))
        parsed = json.loads(description)
        parsed["price"] = "2000000000000000000"
        self.assertIsNone(
            signing.recover_quote_signer_compat(
                json.dumps(parsed, sort_keys=True, separators=(",", ":"))
            )
        )

    def test_rejects_other_success_criterion(self) -> None:
        other = "other_criterion"
        description, _ = _description(
            stored_marker=other,
            signed_marker=list(other),
        )
        self.assertIsNone(signing.recover_quote_signer_compat(description))

    def test_rejects_malformed_description(self) -> None:
        self.assertIsNone(signing.recover_quote_signer_compat("not-json"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
cd stockanalyst/app/agent
.venv/bin/python -m unittest tests.test_signing_compat -v
```

Expected: tests fail with
`AttributeError: module 'stockanalyst.app.agent.signing' has no attribute 'recover_quote_signer_compat'`.

- [ ] **Step 3: Implement the minimal compatibility function**

Add the constant and function to `stockanalyst/app/agent/signing.py`:

```python
_CONTEXT_REQUIRED_CRITERION = "uomp_notify_context_required_v1"


def recover_quote_signer_compat(description: str) -> str | None:
    from bnbagent_studio_core.erc8183.verify import recover_quote_signer

    recovered = recover_quote_signer(description)
    if recovered is not None:
        return recovered

    import json

    try:
        parsed = json.loads(description)
        terms = parsed["terms"]
        if (
            not isinstance(parsed, dict)
            or not isinstance(terms, dict)
            or terms.get("success_criteria") != _CONTEXT_REQUIRED_CRITERION
        ):
            return None
        negotiation_hash = parsed["negotiation_hash"]
        provider_sig = parsed["provider_sig"]
        if not isinstance(negotiation_hash, str) or not isinstance(provider_sig, str):
            return None

        content = {
            key: value
            for key, value in parsed.items()
            if key not in ("negotiation_hash", "provider_sig")
        }
        content["terms"] = dict(terms)
        content["terms"]["success_criteria"] = list(_CONTEXT_REQUIRED_CRITERION)

        from web3 import Web3

        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
        recomputed = Web3.keccak(text=canonical).hex()
        if recomputed.lower() != negotiation_hash.lower():
            return None

        from eth_account import Account
        from eth_account.messages import encode_defunct

        return Account.recover_message(
            encode_defunct(text=negotiation_hash),
            signature=provider_sig,
        )
    except (KeyError, TypeError, ValueError):
        return None
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
cd stockanalyst/app/agent
.venv/bin/python -m unittest tests.test_signing_compat -v
```

Expected: all five tests pass.

- [ ] **Step 5: Commit the independently tested recovery function**

```bash
git add stockanalyst/app/agent/signing.py \
  stockanalyst/app/agent/tests/test_signing_compat.py
git commit -m "fix: verify legacy signed quote markers"
```

---

### Task 2: Use Compatibility Recovery in Funded-Job Verification

**Files:**
- Modify: `stockanalyst/app/agent/tests/test_job_snapshot.py`
- Modify: `stockanalyst/app/agent/signing.py`

**Interfaces:**
- Consumes: `recover_quote_signer_compat(description: str) -> str | None`
- Produces: `verify_signed_job_snapshot(job_id: int)` using compatible recovery while preserving its existing return contract

- [ ] **Step 1: Change the snapshot test to require the project-local verifier**

In
`test_verifies_and_extracts_authorization_and_spec_from_one_chain_read`, replace:

```python
patch(
    "bnbagent_studio_core.erc8183.verify.recover_quote_signer",
    return_value=PROVIDER,
),
```

with:

```python
patch.object(
    signing,
    "recover_quote_signer_compat",
    return_value=PROVIDER,
) as recover,
```

and add after the call:

```python
recover.assert_called_once_with("signed-description")
```

- [ ] **Step 2: Run the snapshot test and verify RED**

Run:

```bash
cd stockanalyst/app/agent
.venv/bin/python -m unittest tests.test_job_snapshot -v
```

Expected: the main snapshot test fails because
`verify_signed_job_snapshot` still calls the SDK verifier directly.

- [ ] **Step 3: Wire the compatibility verifier**

In `verify_signed_job_snapshot`, keep `JobDescription` imported from the SDK but
remove the local `recover_quote_signer` import:

```python
from bnbagent_studio_core.erc8183.verify import JobDescription
```

Replace:

```python
signer = recover_quote_signer(job.description)
```

with:

```python
signer = recover_quote_signer_compat(job.description)
```

- [ ] **Step 4: Run focused and full validation**

Run:

```bash
cd stockanalyst/app/agent
.venv/bin/python -m unittest \
  tests.test_signing_compat \
  tests.test_job_snapshot -v
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m ruff check .
cd ../../../buyer-client
PATH=/Users/zhaoyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:$PATH npm test
```

Expected: all Python tests pass, Ruff reports no errors, and all 134 buyer tests
pass.

- [ ] **Step 5: Commit the integration**

```bash
git add stockanalyst/app/agent/signing.py \
  stockanalyst/app/agent/tests/test_job_snapshot.py
git commit -m "fix: accept verified legacy quotes in funded jobs"
```

---

### Task 3: Update the Existing Runtime and Resume Job 398

**Files:**
- No committed production files
- Generated reports remain untracked under `buyer-client/`

**Interfaces:**
- Consumes: existing AgentCore runtime, Cognito client, buyer keystore, funded job `398`
- Produces: accepted notify, S3/CloudFront manifest, on-chain `SUBMITTED`/`COMPLETED` state, HTML/PDF report, and matching CloudWatch evidence

- [ ] **Step 1: Deploy in place**

Run from `stockanalyst/app/agent`, using the same accepted deployment flags:

```bash
export AWS_PROFILE=dev
export WALLET_PASSWORD="$(../../.venv/bin/python -c 'from pathlib import Path; p=Path("../../.studio/.env.local"); line=next(x for x in p.read_text().splitlines() if x.startswith("WALLET_PASSWORD=")); value=line.split("=",1)[1].strip(); print(value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\\047\\042" else value, end="")')"
uvx --offline --from bnbagent-studio==0.0.5 bag deploy agent \
  --project-root /Users/zhaoyu/corp/bnbchain/chain_middleware/stockanalyst-agent-demo/stockanalyst/app/agent \
  --skip-prepare \
  --accept-risk \
  --force \
  --force-deploy-broken-storage
```

Expected: CloudFormation reaches `UPDATE_COMPLETE` and runtime
`stockanalyst_stockanalyst-hrXlh1BUtQ` reaches `READY`.

- [ ] **Step 2: Retry only notify for job 398**

Start `guard-mock.mjs`, obtain the Cognito secret transiently with
`AWS_PROFILE=dev`, decrypt the buyer keystore in memory, and call:

```typescript
const memory = new GuardUserMemory();
const { portfolio, riskProfile } = await buildTaskFromMemory(memory);
const status = await notifyFunded(
  process.env.AGENT_ENDPOINT!,
  buyerWallet,
  398n,
  { portfolio, riskProfile },
);
```

Use:

```text
AGENT_ENDPOINT=https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A201243086760%3Aruntime%2Fstockanalyst_stockanalyst-hrXlh1BUtQ/invocations?qualifier=DEFAULT
AGENT_TOKEN_URL=https://bnbagent-seller-201243086760.auth.us-east-1.amazoncognito.com/oauth2/token
AGENT_OAUTH_SCOPE=bnbagent-seller/invoke
AGENT_CLIENT_ID=7592i53k46tpd4ecslp1g1gls8
AGENT_SESSION_ID=stockanalyst-e2e-20260728-buyer-964694-0001
```

Expected: `status=accepted`. Do not call `ERC8183Buyer.buy`.

- [ ] **Step 3: Poll and verify the existing job**

Use `ERC8183Buyer.pollUntilSubmitted(398n)` and the receipt block from funding
transaction
`0x8e98b04424dc4f311ac3080f9267b8d8db93bd941b541087a0868c4530e11e3a`.
Then call `completeSubmittedJob` with:

```typescript
{
  jobId: 398n,
  fundTxBlock: fundingReceipt.blockNumber,
  chainId: 97n,
  contracts: {
    commerce: CONTRACTS.COMMERCE,
    router: CONTRACTS.ROUTER,
    policy: CONTRACTS.POLICY,
  },
}
```

Expected: manifest verification succeeds before settlement, and HTML/PDF are
saved using decimal job ID `398`.

- [ ] **Step 4: Collect cross-system evidence**

With `export AWS_PROFILE=dev`, query:

```bash
aws logs filter-log-events \
  --region us-east-1 \
  --log-group-name /aws/bedrock-agentcore/runtimes/stockanalyst_stockanalyst-hrXlh1BUtQ-DEFAULT \
  --filter-pattern '"398"'
```

Fetch the on-chain deliverable URL anonymously and record:

- buyer and seller public addresses;
- create, register, set-budget, approve, fund, submit, and settle transaction hashes;
- final job status;
- exact CloudFront URL;
- HTTP status `200`;
- manifest `job_id`, `chain_id`, and contract addresses;
- canonical manifest Keccak and on-chain commitment;
- generated HTML/PDF paths;
- CloudWatch lines for job `398`, S3/CloudFront upload, and submit transaction.

- [ ] **Step 5: Final verification and commit**

Run the complete Python, Ruff, and buyer commands from Task 2 again. Confirm
generated reports and secrets are not staged. Commit only any evidence document
that is intentionally tracked; do not commit `.env`, `.env.local`, keystores,
tokens, or generated reports.
