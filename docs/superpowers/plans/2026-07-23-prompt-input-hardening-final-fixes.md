# Prompt Input Hardening Final Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every adjudicated item in the final prompt-input-hardening review without changing public protocols, function signatures, renderer behavior, unknown-field handling, or existing collection lower bounds.

**Architecture:** Keep provider transport and provider-value normalization in `data_sources.py`, move the ADK system instruction into a pure constant module, and keep model-response budgeting/candidate selection in `report_pipeline.py`. Tests exercise public provider/report behavior with local fakes only and add deletion-sensitive schema/prompt invariants.

**Tech Stack:** Python 3.14, `unittest`, `requests`, Pydantic v2, Google ADK source wiring, Node 24 buyer regression tests.

## Global Constraints

- Model response budget is at most 2 MiB UTF-8 and at most 64 decoded JSON candidates.
- Provider response body budget is at most 1 MiB, Alpha feed is at most 20 articles, and each Alpha article inspects at most 100 ticker-sentiment entries.
- Provider fetches retain the existing 10-second timeout and use bounded streaming with `Content-Length` preflight when present.
- Provider HTTP/transport failures return stable codes and never log or return raw exception text, URLs, or credentials.
- Alpha `Information` and provider prose are normalized; tickers must be strings; scores must be finite and in `[-1, 1]`; GNews `totalArticles` must be a bounded non-negative integer or fall back to the sanitized headline count.
- Unknown report fields remain ignored. No new top-level collection lower bounds are added.
- Public protocols, function signatures, tool registration, ticker behavior, funded sweep, and renderer contract remain unchanged.
- No external network, live chain, wallet, tunnel, or browser is used.

---

### Task 1: Bound and sanitize provider responses

**Files:**
- Modify: `stockanalyst/app/agent/data_sources.py`
- Modify: `stockanalyst/app/agent/tests/test_untrusted_news.py`

**Interfaces:**
- Preserve `fetch_alpha_vantage_sentiment(symbol: str) -> dict[str, Any]`.
- Preserve `fetch_gnews_headlines(symbol: str, company_name: str = "") -> dict[str, Any]`.
- Add only private transport helpers/constants.

- [ ] **Step 1: Add RED provider tests**

Add a local streaming-response fake and tests that:

```python
result = fetch_alpha_vantage_sentiment("AAPL")
self.assertEqual(result["error"], "provider_http_error")
self.assertNotIn(alpha_key, str(result))
self.assertNotIn(alpha_key, captured_logs.output)
get.assert_called_once_with(..., timeout=10, stream=True)
```

Repeat for GNews 401/429, the Alpha tool-result shape, oversized
`Content-Length`, an incrementally oversized body, non-dict JSON roots,
normalized `Information`, feed item 21 exclusion, ticker entry 101 exclusion,
non-string tickers, NaN/Inf/out-of-range scores, and abnormal
`totalArticles`. Assert `MemoryError` is re-raised.

- [ ] **Step 2: Run the new tests and record RED**

Run:

```bash
stockanalyst/app/agent/.venv/bin/python -m unittest \
  stockanalyst.app.agent.tests.test_untrusted_news -v
```

Expected RED causes: no `stream=True`, raw key-bearing HTTP errors, unbounded
body parsing, untyped metadata, and non-finite/out-of-range scores.

- [ ] **Step 3: Implement the minimum provider boundary**

Add a private `_read_provider_json(...)` that calls `requests.get` with
`stream=True`, checks numeric `Content-Length` against `1_048_576`, copies
`iter_content()` chunks into a byte buffer only while within the cap, then
UTF-8-decodes and `json.loads` the bounded body. Convert HTTP, transport,
oversize, and malformed-response failures to private exceptions carrying only
stable codes. Catch those private exceptions in the two public providers,
logging provider/symbol/code only. Do not catch `MemoryError`.

Normalize Alpha `Information` to 300 characters. Require dict response roots,
list feed/articles containers, string tickers, numeric-or-string non-bool
scores whose parsed floats are finite and within `[-1, 1]`, and cap iteration
at 20 articles and 100 ticker entries. Accept only non-bool integer
`totalArticles` values from 0 through the private finite maximum; otherwise use
`len(headlines)`.

- [ ] **Step 4: Run focused provider tests GREEN**

Run the Task 1 command again. Expected: all provider tests pass and captured
logs/results contain no test credentials or raw exception text.

---

### Task 2: Unify the actual system JSON contract

**Files:**
- Create: `stockanalyst/app/agent/agent_instruction.py`
- Modify: `stockanalyst/app/agent/main.py`
- Modify: `stockanalyst/app/agent/tests/test_prompt_builder.py`

**Interfaces:**
- Produce `SYSTEM_INSTRUCTION: str`.
- Keep the `Agent(...)`, runner, and `_run_llm` public/runtime wiring unchanged.

- [ ] **Step 1: Add RED instruction and delimiter tests**

Assert both client-context delimiters surround personalized values. Parse
`main.py` and assert its `Agent` call binds `instruction=SYSTEM_INSTRUCTION`.
Assert the imported constant requires one raw JSON object, forbids Markdown
and fences, names the `StockReport` contract, preserves data/tool trust
boundaries, and does not request Markdown sections, tables, or a disclaimer.

- [ ] **Step 2: Run focused prompt tests and record RED**

Run:

```bash
stockanalyst/app/agent/.venv/bin/python -m unittest \
  stockanalyst.app.agent.tests.test_prompt_builder -v
```

Expected RED causes: `agent_instruction` does not exist and `main.py` embeds
the conflicting Markdown instruction.

- [ ] **Step 3: Add and wire the pure instruction**

Define a concise `SYSTEM_INSTRUCTION` that retains the stock-analysis/tool
workflow and untrusted-data rule but requires the final response to be exactly
one raw JSON object matching `StockReport`, with no Markdown, fences, comments,
or surrounding prose. Import it deployment-compatibly in `main.py` and pass
the constant directly to `Agent`.

- [ ] **Step 4: Run focused prompt tests GREEN**

Run the Task 2 command again. Expected: all prompt/instruction tests pass.

---

### Task 3: Budget candidate parsing and sanitize validation logs

**Files:**
- Modify: `stockanalyst/app/agent/report_pipeline.py`
- Modify: `stockanalyst/app/agent/tests/test_report_pipeline.py`

**Interfaces:**
- Preserve `generate_validated_report(...) -> str`.
- Preserve the exact public `SAFE_FAILURE_REPORT` literal.
- Private candidate iteration may return decoded Python objects instead of JSON slices.

- [ ] **Step 1: Add RED pipeline tests**

Add tests for two secret-bearing invalid ratings and assert neither secret
appears in the return value nor `assertLogs` output; a braced metadata object
followed by a valid bare report; 2 MiB accepted / over-2-MiB rejected based on
UTF-8 bytes; candidate 64 accepted / candidate 65 ignored; two actual fenced
reports where the first is invalid and the second valid; and an exact literal
assertion:

```python
self.assertEqual(
    SAFE_FAILURE_REPORT,
    "# Report generation unavailable\n\n"
    "The analysis engine could not produce a valid structured report. No unvalidated\n"
    "model output was delivered. Please retry with a new job.",
)
```

- [ ] **Step 2: Run focused pipeline tests and record RED**

Run:

```bash
stockanalyst/app/agent/.venv/bin/python -m unittest \
  stockanalyst.app.agent.tests.test_report_pipeline.ReportPipelineTests -v
```

Expected RED causes: Pydantic includes `input_value` in logs, only the first
bare object is attempted, and response/candidate budgets do not exist.

- [ ] **Step 3: Implement offset-based bounded parsing**

Reject non-string or UTF-8-invalid/over-`2_097_152` responses before scanning.
Use one `json.JSONDecoder` and `raw_decode(text, offset)` at fence/object
offsets instead of decoding sliced suffixes. Preserve fence-first ordering,
deduplicate offsets, and stop after 64 successfully decoded candidates. Try
each candidate against `StockReport`.

For Pydantic failures, log only a bounded tuple of sanitized
`(location, error_type)` pairs; for response/JSON failures, log only stable
codes. Never format raw exceptions, candidate objects, or model text. Catch
only expected parse/validation exceptions and re-raise `MemoryError`.

- [ ] **Step 4: Run focused pipeline tests GREEN**

Run the Task 3 command again. Expected: all parser, budget, retry, and
redaction tests pass.

---

### Task 4: Make all approved schema boundaries deletion-sensitive

**Files:**
- Modify: `stockanalyst/app/agent/tests/test_report_pipeline.py`
- Modify production schema only if a newly required invariant is genuinely RED.

**Interfaces:**
- Keep every model class and renderer-facing field unchanged.

- [ ] **Step 1: Add boundary tests**

Use valid entry factories to prove:

- nested and list-element strings accept 8,192 and reject 8,193 characters;
- analyses accept 10/reject 11;
- portfolio actions accept 50/reject 51;
- stop losses accept 50/reject 51;
- both catalyst/risk prose lists accept 10/reject 11 while retaining the
  existing minimum of 3;
- watchlist accepts 5/reject 6;
- risk factors accept 5/reject 6;
- all top-level collections still accept empty lists; and
- unknown top-level/nested fields remain ignored.

- [ ] **Step 2: Run schema and pipeline tests**

Run:

```bash
stockanalyst/app/agent/.venv/bin/python -m unittest \
  stockanalyst.app.agent.tests.test_report_pipeline -v
```

Expected: all existing invariants remain green. If a required boundary is RED,
apply only the matching `Field(max_length=...)` or recursive-value correction,
then rerun.

---

### Task 5: Full verification, evidence, review, and commit

**Files:**
- Create: `.superpowers/sdd/final-prompt-fix-report.md`
- Delete after use: `buyer-client/tsconfig.codex-check.json`

- [ ] **Step 1: Run focused and related Python suites**

Run the prompt, provider, report, notify-security, and seller-notify suites
directly with `-v`, followed by full discovery:

```bash
stockanalyst/app/agent/.venv/bin/python -m unittest discover \
  -s stockanalyst/app/agent/tests -p 'test_*.py' -v
```

- [ ] **Step 2: Run buyer 31 with the temporary sibling mapping**

Create only `buyer-client/tsconfig.codex-check.json` mapping `ethers` to
`../../apex-contracts/node_modules/ethers/lib.esm/index.js`, run Node 24 +
sibling `tsx` across the four buyer test files, confirm exactly 31 pass, then
delete the temporary mapping.

- [ ] **Step 3: Compile and scan**

Run `py_compile` on every changed Python module, `git diff --check`, inspect
the complete diff from `86ab952`, scan changed provider/pipeline/tool code for
raw exception/model/key leakage, scan tracked files for credential-like
literals, and prove the temporary mapping is absent.

- [ ] **Step 4: Write evidence and self-review**

Record every RED command/failure cause, GREEN command/count, full Python and
buyer results, compilation/scans, compatibility checks, scope exclusions, and
remaining concerns in `.superpowers/sdd/final-prompt-fix-report.md`.

- [ ] **Step 5: Commit**

Stage only the adjudicated fix/test/plan/report files and commit once with:

```bash
git commit -m "fix: close prompt hardening review"
```
