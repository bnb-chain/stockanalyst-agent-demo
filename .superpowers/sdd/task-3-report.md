# Task 3 — Stateless Lambda Adapter: implementation evidence

## Scope delivered

Created the Lambda-local source and test suite under `gateway/x402_lambda/`:

- `src/envelope.py` converts API Gateway REST proxy events into the Agent bridge's exact v1 envelope. It permits only the four bridge routes, removes a real REST stage prefix, rejects any query, enforces empty GET bodies, validates base64 and the 256 KiB input cap, and derives stable `x402gw_` request IDs from the required canonical digest input.
- Header forwarding is allow-list-only (`accept`, `content-type`, `x-payment`, `x-job-token`). Caller `Host`, `Authorization`, forwarding, and hop-by-hop headers are rejected in both `headers` and `multiValueHeaders`; they can never enter an envelope. Header values must also be safe for the bridge's Latin-1 ASGI encoding.
- `src/oauth_client.py` reads exactly one injected Secrets Manager secret through its injected reader, requires exactly nonblank `client_id`, `client_secret`, `token_url`, and `scope` JSON fields, uses Basic client credentials, and warm-caches secret/token state until `expires_in - 30s`. Errors use stable codes and omit token response bodies.
- `src/agentcore_client.py` posts a JSON-RPC `message/send` A2A request whose only application part is `{skill: x402_http_envelope, envelope: ...}`. Its session ID is stable (`x402-gateway-session-<requestId>`). It accepts exactly one `result.parts[*].kind == data` response envelope, validates its request ID/body/headers, and maps 401/403, 429, timeout, and 5xx to typed exceptions without exposing upstream bodies.
- `src/handler.py` uses only safe warm state (the OAuth client); the default secret reader imports Lambda-provided `boto3` lazily and calls `get_secret_value(SecretId=<configured X402_OAUTH_SECRET_ARN>)`. Successful responses preserve actual application status, headers, and base64 body. Invalid bridge output is a safe 502. OAuth/configuration/upstream availability failures are safe 503s. A timeout returns only `{"errorCode":"settlement_status_unknown","retryable":true}`; it contains no instruction to create a new proof.
- Lambda logs are JSON and contain only `requestId`, `route`, `status`, `outcome`, and `durationMilliseconds`. No event, header, body, or digest input is logged.

No AWS calls, network calls, deployment changes, wallet/B402/S3/OpenRouter changes, or Task 1/2 changes were made.

## TDD evidence

The required test-first cycle was followed for each module.

| Cycle | RED command/result | GREEN result |
| --- | --- | --- |
| Envelope | `PYTHONPATH=gateway/x402_lambda/src python3 -m unittest gateway/x402_lambda/tests/test_envelope.py -v` failed with `ModuleNotFoundError: No module named 'envelope'`. | Eight initial envelope tests passed after the minimal implementation. |
| OAuth | `... test_oauth_client.py -v` failed with `ModuleNotFoundError: No module named 'oauth_client'`. | Twelve cumulative tests passed after implementation. |
| AgentCore | `... test_agentcore_client.py -v` failed with `ModuleNotFoundError: No module named 'agentcore_client'`. | Sixteen cumulative tests passed after implementation. |
| Handler | `... test_handler.py -v` failed with `ModuleNotFoundError: No module named 'handler'`. | Twenty-one cumulative tests passed after implementation. |
| Review hardening | Added tests for forbidden `multiValueHeaders` and missing config; the run failed respectively with an expected assertion failure and uncaught `GatewayConfigurationError`. Added tests for non-Latin-1 headers and blank secret values; both failed before the fixes. | The final complete suite is green (below). |

## Final verification

Command (no AWS credentials or network required):

```bash
PYTHONPATH=gateway/x402_lambda/src \
python3 -m unittest discover -s gateway/x402_lambda/tests -p 'test_*.py' -v
```

Result:

```text
Ran 24 tests in 0.003s

OK
```

Also ran `git diff --check` successfully.

## Tests covered

- Exact route/envelope v1 construction, deterministic request ID, stage normalization, invalid routes/jobs, queries, GET body, base64 decoding, body cap, public-base validation, forbidden single/multivalue headers, and bridge-encoding-safe headers.
- OAuth secret shape, single warm secret read, Basic client credentials request, token refresh margin, and response-body secrecy.
- A2A data part shape, stable AgentCore session ID, response request ID binding, upstream status/error typing, and transport timeout mapping.
- Lambda 402 reconstruction, safe OAuth/config 503, malformed-agent 502, and retry-safe indeterminate timeout 503.

## Residual deployment concern

Deployment must set `X402_GATEWAY_PUBLIC_BASE_URL`, `X402_AGENTCORE_RUNTIME_URL` (or `AGENTCORE_RUNTIME_URL`), and `X402_OAUTH_SECRET_ARN` to production values, and grant the Lambda role `secretsmanager:GetSecretValue` for that one ARN. This task deliberately did not deploy or query AWS.
