# x402 Lambda Gateway Operations

This runbook deploys the public REST adapter for the AgentCore x402 envelope
skill. It is for the testnet deployment only. Do not put an AgentCore invocation
URL in a buyer: `X402_ENDPOINT` is the API Gateway output
`X402GatewayBaseUrl`.

## Boundary and safety model

The only public routes are:

- `GET /x402/price`
- `POST /x402/analyze/async`
- `GET /x402/jobs/{jobId}`
- `POST /x402/jobs/{jobId}/resume`

The REST API invokes the Lambda `live` alias, never an unqualified function.
Lambda derives the public base only from the trusted API Gateway REST proxy
`requestContext.domainName` and `requestContext.stage`; caller `Host` and
forwarded headers are ignored. Lambda and the Agent each enforce the exact
256 KiB request limit. WAF provides the configured per-IP `/x402/` rate limit
and count-only AWS common rules; it does not enforce that exact byte limit.

The AgentCore runtime remains internal. The Lambda needs only
`AGENTCORE_INVOKE_URL` and `OAUTH_SECRET_ARN`. Use a dedicated Cognito app
client for this gateway and store its client-credentials material in one
dedicated Secrets Manager secret. Do not reuse the buyer client or print the
secret, access token, payment proof, job token, or wallet password.

The deployed BSC testnet seller is:

```dotenv
X402_SELLER_WALLET=0xd10BdDC20E4DC42A1a19a9653e994991e25b8153
```

The gateway OAuth secret has this shape; the placeholders are deliberately
redacted:

```json
{
  "client_id": "<gateway-cognito-client-id>",
  "client_secret": "<redacted>",
  "token_url": "https://<domain>.auth.<region>.amazoncognito.com/oauth2/token",
  "scope": "<resource-server-identifier>/<scope>"
}
```

## Preconditions

Choose the AgentCore invocation URL, S3 artifact bucket, Cognito user pool,
resource-server scope, and stack name outside this runbook. Confirm the shared
regional API Gateway CloudWatch role before creating the stack. The template
does not create `AWS::ApiGateway::Account`, because that role is shared across
REST APIs in the account and region.

```bash
export AWS_PROFILE=dev
set -eu
set +x
export AWS_REGION=us-east-1
aws apigateway get-account --region "$AWS_REGION" --query cloudwatchRoleArn --output text
```

Continue only when that command returns the intended role ARN, not `None`.
This preflight has no payment or data-plane charge.

## Create the gateway OAuth secret

Create a dedicated confidential Cognito app client with its own generated
secret and the minimum client-credentials scope. Assign the returned values to
shell variables while tracing is disabled; do not use `--output` in a way that
prints the client secret.

```bash
export AWS_PROFILE=dev
set -eu
set +x
export AWS_REGION=us-east-1
export COGNITO_USER_POOL_ID='<user-pool-id>'
export GATEWAY_CLIENT_JSON="$(aws cognito-idp create-user-pool-client \
  --region "$AWS_REGION" \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --client-name 'stockanalyst-x402-gateway' \
  --generate-secret \
  --allowed-o-auth-flows client_credentials \
  --allowed-o-auth-scopes '<resource-server-identifier>/<scope>' \
  --allowed-o-auth-flows-user-pool-client \
  --output json)"
export GATEWAY_CLIENT_ID="$(python3 -c 'import json,os; print(json.loads(os.environ["GATEWAY_CLIENT_JSON"])["UserPoolClient"]["ClientId"])')"
export GATEWAY_CLIENT_SECRET="$(python3 -c 'import json,os; print(json.loads(os.environ["GATEWAY_CLIENT_JSON"])["UserPoolClient"]["ClientSecret"])')"
```

Before running the next block, replace the placeholder token URL with the
Cognito domain issued for this user pool. The variable assignment suppresses
the secret from normal output; keep `set +x` in effect.

```bash
export AWS_PROFILE=dev
set -eu
set +x
export AWS_REGION=us-east-1
export COGNITO_TOKEN_URL='https://<domain>.auth.us-east-1.amazoncognito.com/oauth2/token'
export COGNITO_SCOPE='<resource-server-identifier>/<scope>'
GATEWAY_OAUTH_SECRET="$(python3 -c 'import json,os; print(json.dumps({"client_id":os.environ["GATEWAY_CLIENT_ID"],"client_secret":os.environ["GATEWAY_CLIENT_SECRET"],"token_url":os.environ["COGNITO_TOKEN_URL"],"scope":os.environ["COGNITO_SCOPE"]},separators=(",",":")))')"
export OAUTH_SECRET_ARN="$(aws secretsmanager create-secret \
  --region "$AWS_REGION" \
  --name 'stockanalyst/x402-gateway/oauth' \
  --secret-string "$GATEWAY_OAUTH_SECRET" \
  --query ARN --output text)"
unset GATEWAY_OAUTH_SECRET GATEWAY_CLIENT_JSON GATEWAY_CLIENT_SECRET
```

Record the ARN in protected operator configuration. Do not put it, a client
secret, or a token in source control. If an existing dedicated client and
secret are being reused, retrieve only their identifiers into shell variables
with tracing disabled; never print `SecretString`.

## Deploy the Agent and gateway

Deploy the Agent with `bag deploy` only. Do not use raw AgentCore deployment
commands: `bag deploy` preserves the wallet/secret deployment workflow.

```bash
cd stockanalyst/app/agent
bag deploy
```

Package CloudFormation into a temporary template, then deploy that packaged
file. Keep the artifact bucket private and selected by the operator.

```bash
export AWS_PROFILE=dev
set -eu
set +x
export AWS_REGION=us-east-1
export X402_STACK_NAME='stockanalyst-x402-testnet'
export X402_ARTIFACT_BUCKET='<private-artifact-bucket>'
export AGENTCORE_INVOKE_URL='https://bedrock-agentcore.<region>.amazonaws.com/<runtime-invocation-path>'
export X402_PACKAGED_TEMPLATE="$(mktemp "${TMPDIR:-/tmp}/x402-gateway-packaged.XXXXXX.yaml")"
aws cloudformation package \
  --region "$AWS_REGION" \
  --template-file infra/x402-lambda-gateway.yaml \
  --s3-bucket "$X402_ARTIFACT_BUCKET" \
  --output-template-file "$X402_PACKAGED_TEMPLATE"
aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name "$X402_STACK_NAME" \
  --template-file "$X402_PACKAGED_TEMPLATE" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    StageName=testnet \
    AgentCoreInvokeUrl="$AGENTCORE_INVOKE_URL" \
    OAuthSecretArn="$OAUTH_SECRET_ARN"
rm -f -- "$X402_PACKAGED_TEMPLATE"
unset X402_PACKAGED_TEMPLATE
```

Retrieve the public endpoint only from the stack output. It is the value buyers
must use; it is never the raw `AGENTCORE_INVOKE_URL`.

```bash
export AWS_PROFILE=dev
set -eu
set +x
export AWS_REGION=us-east-1
export X402_STACK_NAME='stockanalyst-x402-testnet'
export X402_ENDPOINT="$(aws cloudformation describe-stacks \
  --region "$AWS_REGION" \
  --stack-name "$X402_STACK_NAME" \
  --query 'Stacks[0].Outputs[?OutputKey==`X402GatewayBaseUrl`].OutputValue | [0]' \
  --output text)"
test -n "$X402_ENDPOINT" && test "$X402_ENDPOINT" != 'None'
```

## Verification

First run no-spend checks. They request public metadata and a missing-payment
challenge only; neither includes `X-Payment` or creates a paid job.

```bash
set -eu
curl --fail-with-body "$X402_ENDPOINT/x402/price"
curl --fail-with-body -X POST "$X402_ENDPOINT/x402/analyze/async" \
  -H 'content-type: application/json' \
  --data '{"symbols":["AAPL"]}'
```

The first response is `200`; the second is `402 Payment Required` with the
gateway resource URL. Verify API access logs contain only route/status metadata
and Lambda logs contain only the safe request summary.

### Paid test — explicit approval required

Do not proceed without named approval from the payment owner for one testnet
payment plus testnet gas. Confirm the endpoint is the `describe-stacks` output,
the seller wallet is exactly
`0xd10BdDC20E4DC42A1a19a9653e994991e25b8153`, and the buyer has sufficient
testnet balances. Run the buyer only after that approval; never paste or log a
proof, private key, password, job token, OAuth token, or secret.

```bash
set -eu
test "${X402_PAYMENT_APPROVED:-}" = 'yes'
cd buyer-client
X402_ENDPOINT="$X402_ENDPOINT" npm run x402:async
```

## Rollback and retention

Rollback preserves already accepted jobs before public ingress is withdrawn:

1. Disable only new job creation in the Agent, then deploy with `bag deploy`.
   Existing job-status and resume paths must remain available.

   ```bash
   cd stockanalyst/app/agent
   bag env set X402_ASYNC_ACCEPT_NEW_JOBS 0
   bag deploy
   ```

2. Verify accepted jobs can still be retrieved with their private job tokens.
   Keep the job bucket, `X402_JOB_TOKEN_SECRET`, Agent permissions, and gateway
   routes intact for every accepted job's access window (job expiry plus any
   already issued download URL lifetime).
3. After those access windows have elapsed, disable API ingress through the
   approved change process (for example, a WAF default-block update or stack
   removal). Do not delete the job data or secret while accepted-job access is
   still promised. Any AWS CLI change block used for that action must begin
   with `export AWS_PROFILE=dev` and use the current WAF lock token.

Do not roll back by exposing the raw AgentCore URL. Re-enable new jobs only
after the gateway and its live alias are healthy again.
