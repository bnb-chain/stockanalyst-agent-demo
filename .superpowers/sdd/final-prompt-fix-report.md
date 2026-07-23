# Prompt Input Hardening — Final Fix Report

Baseline: `86ab9527bbff8f7b0dd5dcea29497a7195cfe280`

Branch: `fix-bugs`

## Outcome

All adjudicated prompt-input-hardening findings were closed. The final
read-only review verdict was **APPROVED**, with no remaining High, Medium, or
Low blocker or concern.

The implementation:

- bounds Alpha Vantage and GNews bodies at 1 MiB using streaming reads, retains
  the 10-second timeout, closes responses on every path, and exposes only
  stable HTTP/transport/oversize/invalid-response codes;
- caps Alpha processing at 20 articles and 100 ticker entries per article,
  accepts only string tickers and finite scores in `[-1, 1]`, and normalizes
  provider prose;
- bounds and type-checks GNews totals, falling back to the sanitized headline
  count;
- binds the running ADK agent to one shared JSON-only `StockReport` system
  instruction;
- rejects model responses above 2 MiB UTF-8, scans at most 64 decoded JSON
  candidates using decoder offsets, and logs only bounded sanitized validation
  locations/types or stable parse codes; and
- preserves the approved fixed failure report after the correction retry.

## TDD Evidence

### Provider boundary

Initial RED:

```zsh
stockanalyst/app/agent/.venv/bin/python -B -m unittest \
  stockanalyst.app.agent.tests.test_untrusted_news -v
```

The initial 12-test provider/text suite reported nine failures and five
subtest errors. The failures proved that requests were not streamed, response
bodies were unbounded, raw key-bearing HTTP errors reached results/logs,
abnormal provider shapes were not normalized, feed/ticker iteration was not
fully capped, and invalid score/total types were accepted. `MemoryError` was
also swallowed by the former broad exception handling.

Additional focused RED cycles were run before each transport correction:

- exact/oversized `Content-Length`, incremental overflow, and response closing:
  five failures and one error in the 14-test focused suite;
- deeply nested provider/model JSON: two uncaught `RecursionError` errors;
- key-bearing close failures: raw request exception text appeared in two
  subtests;
- invocation inside an already handled caller exception: close failure did not
  produce the stable transport code;
- close/stream `OSError`, private exception cause/context, and close-time
  `MemoryError`: two errors plus two redaction failures; and
- a 5,000-digit JSON integer in provider input: uncaught Python 3.14
  `ValueError`.

Each cycle was followed by the minimum implementation change and the exact
focused methods were rerun GREEN. The final provider/text suite result is:

```text
Ran 22 tests
OK
```

Deletion-sensitive test improvements also prove separately that:

- every invalid Alpha score, including `1.0001` and `-1.0001`, contributes no
  score;
- `True` is not accepted as a GNews integer total;
- non-string Alpha `Information` becomes empty text;
- missing GNews totals with seven articles fall back to the capped count of
  five; and
- non-string GNews title/source/date values and non-object articles become
  explicit empty fields.

### Actual system instruction

RED:

```zsh
stockanalyst/app/agent/.venv/bin/python -B -m unittest \
  stockanalyst.app.agent.tests.test_prompt_builder -v
```

Result: one error and one failure because `agent_instruction.py` did not exist
and `main.py` still embedded a conflicting Markdown/table/disclaimer
instruction.

GREEN after creating and wiring `SYSTEM_INSTRUCTION`:

```text
Ran 6 tests
OK
```

### Model response pipeline

RED:

```zsh
stockanalyst/app/agent/.venv/bin/python -B -m unittest \
  stockanalyst.app.agent.tests.test_report_pipeline.ReportPipelineTests -v
```

Result: six failures. Invalid model values appeared in validation logs, a
later valid bare object was not tried, the 2 MiB budget and 64-candidate budget
were absent, suffix slicing was still used, and the candidate path did not
prove `MemoryError` propagation.

Later focused RED cycles proved:

- 500,000-level nested model JSON raised uncaught `RecursionError`; and
- a 5,000-digit model JSON integer raised uncaught Python 3.14 `ValueError`.

The `MemoryError` test now patches `JSONDecoder.raw_decode` directly, so it
would fail if the decoder exception classification swallowed resource
exhaustion.

Final report/schema pipeline result:

```text
Ran 25 tests
OK
```

The schema boundary tests remained GREEN without production schema changes.
They prove 8,192/8,193 string boundaries, every approved collection maximum,
the existing catalyst/risk minimum of three, empty top-level collections, and
ignored unknown top-level/nested fields.

## Final Verification

Focused suites:

```text
Prompt builder/instruction:  6/6
Provider/untrusted text:    22/22
Report/schema pipeline:     25/25
Focused total:              53/53
```

Related Python suites:

```zsh
stockanalyst/app/agent/.venv/bin/python -B -m unittest \
  stockanalyst.app.agent.tests.test_prompt_builder \
  stockanalyst.app.agent.tests.test_report_pipeline \
  stockanalyst.app.agent.tests.test_notify_security \
  stockanalyst.app.agent.tests.test_seller_core_notify \
  stockanalyst.app.agent.tests.test_untrusted_news
```

Result: **103/103 passed**.

Full Python discovery:

```zsh
stockanalyst/app/agent/.venv/bin/python -B -m unittest discover \
  -s stockanalyst/app/agent/tests -p 'test_*.py'
```

Result: **133/133 passed**.

Buyer regression:

```zsh
/Users/zhaoyu/.nvm/versions/node/v24.10.0/bin/node \
  ../../apex-contracts/node_modules/tsx/dist/cli.mjs \
  --tsconfig tsconfig.codex-check.json --test \
  src/gateway.test.ts src/notify-auth.test.ts \
  src/pdf-renderer.test.ts src/pdf-report.test.ts
```

Result: **31/31 passed**, 0 failed, under Node 24.10.0. The first sandboxed
attempt was denied when `tsx` tried to create its local IPC pipe; the approved
local rerun passed. The temporary mapping pointed `ethers` to the existing
sibling ESM entry and was deleted immediately after the run. No dependency was
downloaded.

Compilation:

```zsh
stockanalyst/app/agent/.venv/bin/python -B -m py_compile \
  stockanalyst/app/agent/agent_instruction.py \
  stockanalyst/app/agent/data_sources.py \
  stockanalyst/app/agent/main.py \
  stockanalyst/app/agent/report_pipeline.py \
  stockanalyst/app/agent/tests/test_prompt_builder.py \
  stockanalyst/app/agent/tests/test_report_pipeline.py \
  stockanalyst/app/agent/tests/test_untrusted_news.py
```

Result: exit 0.

`git diff --check`: exit 0.

## Safety and Compatibility Review

- The complete implementation/test diff from `86ab952` was inspected.
- An added-line scan for raw exception formatting, tracebacks,
  `model_validate_json`, raw model returns, and key-bearing URL fragments found
  only the intended GNews stable-code log using `error.code`.
- Exact Alpha/GNews function-range scans found only stable-code logs/results.
  A broader file scan still sees pre-existing raw FRED/EDGAR error handling;
  those unchanged endpoints are outside this adjudicated provider scope.
- Report validation uses
  `errors(include_input=False, include_url=False)` and emits no raw model
  value, Pydantic message, or documentation URL.
- The raw-model call-path scan confirms `_run_llm` returns only through
  `generate_validated_report`; invalid output reaches the fixed
  `SAFE_FAILURE_REPORT`.
- A tracked credential-pattern scan for OpenAI/GitHub/AWS/private-key forms
  returned no matches.
- `git diff --exit-code 86ab952` is clean for `tools.py`, `report_schema.py`,
  `report_renderer.py`, `seller_core.py`, `prompt_builder.py`,
  `untrusted_text.py`, and `buyer-client/src`.
- The public Alpha, GNews, and report-generation function signatures are
  unchanged. Only private helpers changed.
- Tool registration, ticker behavior, funded sweep, signing, public protocol,
  renderer behavior, unknown-field handling, and existing collection lower
  bounds are unchanged.
- `buyer-client/tsconfig.codex-check.json` is absent.
- No external network, live chain, wallet, tunnel, or browser action was used.

## Final Review

The final independent read-only review checked the provider transport and
redaction paths, decoder error classification, resource-error propagation,
candidate budgets, system instruction binding, schema compatibility, and
deletion-sensitive provider tests.

Verdict: **APPROVED — no remaining High/Medium/Low blocker or concern.**

Remaining concerns: **none**.
