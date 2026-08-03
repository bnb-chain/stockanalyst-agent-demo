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

Create a dedicated confidential Cognito app client with the minimum
client-credentials scope. This flow first performs read-only, exact-name
collision checks. Stop if either resource exists: never reuse, overwrite, or
delete a pre-existing client or secret. The client secret and full Cognito
response never enter shell variables or command arguments. They are held only
in 0600 temporary files long enough to create the Secrets Manager value.

```bash
export AWS_PROFILE=dev
set -eu
set +x
umask 077
export AWS_REGION=us-east-1
export COGNITO_USER_POOL_ID='<user-pool-id>'
export GATEWAY_CLIENT_NAME='stockanalyst-x402-gateway'
export OAUTH_SECRET_NAME='stockanalyst/x402-gateway/oauth'
export COGNITO_TOKEN_URL='https://<domain>.auth.us-east-1.amazoncognito.com/oauth2/token'
export COGNITO_SCOPE='<resource-server-identifier>/<scope>'
CLIENT_LIST_FILE="$(mktemp "${TMPDIR:-/tmp}/x402-cognito-client-list.XXXXXX")"
CLIENT_RESPONSE_FILE="$(mktemp "${TMPDIR:-/tmp}/x402-cognito-client-response.XXXXXX")"
OAUTH_SECRET_FILE="$(mktemp "${TMPDIR:-/tmp}/x402-oauth-secret.XXXXXX")"
SECRET_PROBE_ERROR_FILE="$(mktemp "${TMPDIR:-/tmp}/x402-secret-probe.XXXXXX")"
chmod 600 "$CLIENT_LIST_FILE" "$CLIENT_RESPONSE_FILE" "$OAUTH_SECRET_FILE" "$SECRET_PROBE_ERROR_FILE"
CREATED_CLIENT_ID=''
cleanup_gateway_oauth_provisioning() {
  original_status="$1"
  rm -f -- "$CLIENT_LIST_FILE" "$CLIENT_RESPONSE_FILE" "$OAUTH_SECRET_FILE" "$SECRET_PROBE_ERROR_FILE"
  if test "$original_status" -ne 0 && test -n "$CREATED_CLIENT_ID"; then
    aws cognito-idp delete-user-pool-client \
      --region "$AWS_REGION" \
      --user-pool-id "$COGNITO_USER_POOL_ID" \
      --client-id "$CREATED_CLIENT_ID" >/dev/null 2>&1 || :
  fi
}
trap 'exit_code=$?; cleanup_gateway_oauth_provisioning "$exit_code"; exit "$exit_code"' EXIT

# Read-only Cognito-name preflight. A paginated result fails closed.
aws cognito-idp list-user-pool-clients \
  --region "$AWS_REGION" \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --max-results 60 > "$CLIENT_LIST_FILE"
python3 - "$CLIENT_LIST_FILE" "$GATEWAY_CLIENT_NAME" <<'PY'
import json
import sys

clients = json.load(open(sys.argv[1], encoding="utf-8"))
if clients.get("NextToken"):
    raise SystemExit("Cognito client preflight is paginated; stop and resolve the name collision manually")
if any(item.get("ClientName") == sys.argv[2] for item in clients.get("UserPoolClients", [])):
    raise SystemExit("Cognito client name already exists; stop without changing it")
PY

# Read-only Secrets Manager preflight. Only ResourceNotFound may proceed.
if aws secretsmanager describe-secret \
  --region "$AWS_REGION" \
  --secret-id "$OAUTH_SECRET_NAME" \
  --query ARN --output text >/dev/null 2>"$SECRET_PROBE_ERROR_FILE"; then
  printf '%s\n' 'Gateway OAuth secret already exists; stop without changing it' >&2
  exit 1
fi
if ! rg -q 'ResourceNotFoundException' "$SECRET_PROBE_ERROR_FILE"; then
  printf '%s\n' 'Could not safely determine whether the gateway OAuth secret exists; stop' >&2
  exit 1
fi

# Only the new ID is retained. The create response's secret is not printed.
CREATED_CLIENT_ID="$(aws cognito-idp create-user-pool-client \
  --region "$AWS_REGION" \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --client-name "$GATEWAY_CLIENT_NAME" \
  --generate-secret \
  --allowed-o-auth-flows client_credentials \
  --allowed-o-auth-scopes "$COGNITO_SCOPE" \
  --allowed-o-auth-flows-user-pool-client \
  --query 'UserPoolClient.ClientId' --output text)"
test -n "$CREATED_CLIENT_ID" && test "$CREATED_CLIENT_ID" != 'None'
aws cognito-idp describe-user-pool-client \
  --region "$AWS_REGION" \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --client-id "$CREATED_CLIENT_ID" > "$CLIENT_RESPONSE_FILE"
python3 - "$CLIENT_RESPONSE_FILE" "$OAUTH_SECRET_FILE" "$COGNITO_TOKEN_URL" "$COGNITO_SCOPE" <<'PY'
import json
import sys

response_path, secret_path, token_url, scope = sys.argv[1:]
response = json.load(open(response_path, encoding="utf-8"))
client = response["UserPoolClient"]
with open(secret_path, "w", encoding="utf-8") as secret_file:
    json.dump({
        "client_id": client["ClientId"],
        "client_secret": client["ClientSecret"],
        "token_url": token_url,
        "scope": scope,
    }, secret_file, separators=(",", ":"))
PY
chmod 600 "$OAUTH_SECRET_FILE"
export OAUTH_SECRET_ARN="$(aws secretsmanager create-secret \
  --region "$AWS_REGION" \
  --name "$OAUTH_SECRET_NAME" \
  --secret-string "file://$OAUTH_SECRET_FILE" \
  --query ARN --output text)"
test -n "$OAUTH_SECRET_ARN" && test "$OAUTH_SECRET_ARN" != 'None'
CREATED_CLIENT_ID=''
cleanup_gateway_oauth_provisioning 0
trap - EXIT
```

If the secret creation fails, the EXIT trap removes every temporary file and
deletes only the just-created, undistributed Cognito client, retaining the
original failure status. It never deletes a resource found by the collision
preflight. Record the resulting ARN in protected operator configuration; do not
put it, a client secret, or a token in source control.

## Deploy the Agent and gateway

Deploy the Agent with `bag deploy` only. Do not use raw AgentCore deployment
commands: `bag deploy` preserves the wallet/secret deployment workflow.

```bash
(cd stockanalyst/app/agent && bag deploy)
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
umask 077
X402_PACKAGED_TEMPLATE="$(mktemp "${TMPDIR:-/tmp}/x402-gateway-packaged.XXXXXX")"
chmod 600 "$X402_PACKAGED_TEMPLATE"
cleanup_packaged_template() {
  original_status="$1"
  rm -f -- "$X402_PACKAGED_TEMPLATE"
  return "$original_status"
}
trap 'exit_code=$?; cleanup_packaged_template "$exit_code"; exit "$exit_code"' EXIT
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
cleanup_packaged_template 0
trap - EXIT
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
umask 077
PRICE_BODY_FILE="$(mktemp "${TMPDIR:-/tmp}/x402-price-body.XXXXXX")"
CHALLENGE_BODY_FILE="$(mktemp "${TMPDIR:-/tmp}/x402-challenge-body.XXXXXX")"
chmod 600 "$PRICE_BODY_FILE" "$CHALLENGE_BODY_FILE"
cleanup_no_spend_files() {
  original_status="$1"
  rm -f -- "$PRICE_BODY_FILE" "$CHALLENGE_BODY_FILE"
  return "$original_status"
}
trap 'exit_code=$?; cleanup_no_spend_files "$exit_code"; exit "$exit_code"' EXIT
price_status="$(curl --silent --show-error --output "$PRICE_BODY_FILE" --write-out '%{http_code}' \
  "$X402_ENDPOINT/x402/price")"
test "$price_status" = '200'
challenge_status="$(curl --silent --show-error --output "$CHALLENGE_BODY_FILE" --write-out '%{http_code}' \
  -X POST "$X402_ENDPOINT/x402/analyze/async" \
  -H 'content-type: application/json' \
  --data '{"symbols":["AAPL"]}')"
test "$challenge_status" = '402'
X402_ENDPOINT="$X402_ENDPOINT" python3 - "$CHALLENGE_BODY_FILE" <<'PY'
import json
import os
import sys

challenge = json.load(open(sys.argv[1], encoding="utf-8"))
if challenge.get("error") != "Payment Required":
    raise SystemExit("payment challenge did not contain the expected payment-required marker")
resource = challenge["paymentRequired"]["resource"]
expected = f'{os.environ["X402_ENDPOINT"]}/x402/analyze/async'
if resource != expected:
    raise SystemExit("payment challenge resource did not match the gateway endpoint")
PY
cleanup_no_spend_files 0
trap - EXIT
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
(cd buyer-client && X402_ENDPOINT="$X402_ENDPOINT" npm run x402:async)
```

## Rollback and retention

Rollback preserves already accepted jobs before public ingress is withdrawn:

1. Disable only new job creation in the Agent, then deploy with `bag deploy`.
   Existing job-status and resume paths must remain available.

   ```bash
   (cd stockanalyst/app/agent && \
     bag env set X402_ASYNC_ACCEPT_NEW_JOBS 0 && \
     bag deploy)
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
