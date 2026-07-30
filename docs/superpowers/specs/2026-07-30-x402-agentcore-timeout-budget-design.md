# x402 AgentCore Timeout Budget

## Context

Fresh AgentCore sessions now isolate Runtime deployments correctly, but
observed no-payment responses take approximately 11–15 seconds. The Lambda
adapter's AgentCore client still uses a 10-second timeout, so valid cold-session
responses are converted to the safe retryable
`503 settlement_status_unknown` outcome.

The Lambda timeout is 28 seconds and API Gateway remains the outer request
boundary. The upstream client needs a larger, but still bounded, share of that
budget.

## Design

- Change the `AgentCoreClient` default transport timeout from 10 seconds to
  25 seconds.
- Keep the Lambda function timeout at 28 seconds.
- Keep API Gateway, WAF, OAuth, request/response validation, session isolation,
  payment binding, body limits, and response-size limits unchanged.
- Continue mapping an actual upstream timeout to the existing retryable
  `503 settlement_status_unknown` response.

The three-second difference between the upstream timeout and Lambda timeout is
reserved for OAuth cache access, envelope validation, response validation, and
safe error serialization.

## Verification

- A deterministic unit test verifies that the default transport receives
  `timeout_seconds=25.0`.
- Existing explicit custom-timeout tests remain valid.
- Existing timeout mapping, strict response binding, session isolation, OAuth,
  handler, integration, and infrastructure tests remain green.
- After Lambda update, no-payment public verification must return:
  - `GET /x402/price` → `200`;
  - `POST /x402/analyze/async` without `X-Payment` → `402`.
- No `X-Payment`, B402 call, job creation, funding, or U spend is permitted.
