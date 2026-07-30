# Optional Lambda Reserved Concurrency

## Context

The x402 gateway stack currently defaults `ReservedConcurrency` to `10` and
always assigns it to the adapter Lambda. The target AWS account has only the
minimum required unreserved concurrency remaining, so Lambda rejects every
positive reservation and CloudFormation rolls the stack back.

API Gateway throttling and the WAF rate-based rule already bound public request
volume. Reserved Lambda concurrency is therefore useful as an optional
account-level isolation control, but it is not required for the testnet
deployment.

## Design

- Change `ReservedConcurrency` to accept integers from `0` through `100`.
- Change its default to `0`.
- Define a CloudFormation condition that is true only when the parameter is
  greater than zero.
- Set `ReservedConcurrentExecutions` on the adapter function only when that
  condition is true; use `AWS::NoValue` when the parameter is `0`.
- Deploy testnet with the default value, so the Lambda uses the account's
  unreserved concurrency pool.
- Preserve the existing behavior for operators who explicitly provide a
  positive value.

No API route, authentication, payment, runtime envelope, WAF rule, API Gateway
throttle, or AgentCore behavior changes.

## Failure and rollback behavior

CloudFormation continues to own the Lambda and all gateway resources. A
positive reservation that exceeds available account capacity will still fail
normally and roll back. The default deployment avoids that account-capacity
dependency by omitting the reservation.

## Verification

- A static infrastructure test verifies the parameter default and minimum are
  `0`.
- A static infrastructure test verifies the conditional
  `ReservedConcurrentExecutions` property uses `AWS::NoValue` at zero.
- Existing infrastructure, Lambda adapter, integration, and documentation
  tests continue to pass.
- The generated change set must still contain only the approved 16 gateway
  resources before execution.
- Deployment verification remains no-spend: no `X-Payment`, B402 request, job
  creation, or funding is permitted.
