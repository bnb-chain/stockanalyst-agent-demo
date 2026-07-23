# Task 1: Prompt Input Hardening Report

## RED

Command:

```bash
stockanalyst/app/agent/.venv/bin/python -m unittest \
  stockanalyst.app.agent.tests.test_prompt_builder -v
```

Result: failed as expected before production changes with
`ModuleNotFoundError: No module named 'stockanalyst.app.agent.prompt_builder'`.

## GREEN

Focused command:

```bash
stockanalyst/app/agent/.venv/bin/python -m unittest \
  stockanalyst.app.agent.tests.test_prompt_builder -v
```

Result: `Ran 4 tests ... OK`.

Required related-suite command:

```bash
stockanalyst/app/agent/.venv/bin/python -m unittest \
  stockanalyst.app.agent.tests.test_prompt_builder \
  stockanalyst.app.agent.tests.test_notify_security \
  stockanalyst.app.agent.tests.test_seller_core_notify -v
```

Result: `Ran 54 tests ... OK`.

Additional checks:

```bash
stockanalyst/app/agent/.venv/bin/python -m py_compile \
  stockanalyst/app/agent/prompt_builder.py \
  stockanalyst/app/agent/notify_security.py \
  stockanalyst/app/agent/seller_core.py
git diff --check
```

Result: both completed successfully with no output. A direct local prompt assertion
also verified the client-context delimiters, normalized context rendering, and the
untrusted-data instruction.

## Modified files

- Created `stockanalyst/app/agent/prompt_builder.py`.
- Created `stockanalyst/app/agent/tests/test_prompt_builder.py`.
- Renamed the context parser functions to public `parse_portfolio` and
  `parse_risk_profile` in `stockanalyst/app/agent/notify_security.py` without
  changing their validation or exception semantics.
- Delegated `seller_core._build_stock_analysis_prompt` to the deployment-compatible
  prompt-builder import path and removed the old in-file implementation.

## Compatibility

Valid signed-context portfolio and risk-profile data retain the established prompt
wording, values, JSON schema, field rules, and `(prompt, symbols)` return contract.
The only intentional successful-path additions are the security instruction and
the `BEGIN CLIENT CONTEXT DATA` / `END CLIENT CONTEXT DATA` delimiters around
personalized data. Invalid context values now safely degrade to no context rather
than being stringified into the prompt.

## Self-review conclusion

The task scope is limited to prompt construction and existing context-parser
visibility. Job symbols are exact uppercase ticker tokens, are deduplicated and
bounded to ten, analysis type is allowlisted, and context rendering only consumes
validated `Holding` and `RiskProfile` objects. No wallet, signing, policy,
deployment, browser, chain, or network behavior was changed or exercised.
