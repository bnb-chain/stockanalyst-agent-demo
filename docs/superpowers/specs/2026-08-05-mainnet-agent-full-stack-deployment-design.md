# Mainnet Agent Full-Stack Deployment Design

## Goal

Deploy an isolated stockanalyst mainnet environment in AWS account
`201243086760`, region `us-east-1`, using `AWS_PROFILE=prod`. The deployment
must preserve the existing testnet Runtime and gateway, reuse the reserved
mainnet Elastic IP `52.206.204.81`, and provide a separate mainnet AgentCore
Runtime, Cognito boundary, API Gateway, Lambda adapter, private job storage,
Secrets, WAF, logs, and alarms.

## Worktree and Source Isolation

Create a Git worktree at `.worktrees/mainnet-agentcore-runtime` on branch
`codex/mainnet-agentcore-runtime`, based on commit `c73e014`. The original
worktree remains the testnet reference and its uncommitted README and
`studio.toml` changes remain untouched.

Within the mainnet worktree, regenerate and replace the canonical
`stockanalyst/agentcore` directory with mainnet configuration. The identical
path allows `bag deploy` to use its expected layout while Git worktree isolation
keeps the original testnet AgentCore directory unchanged.

Copy `stockanalyst/.studio/.env.mainnet` into the new worktree with mode `0600`.
It remains ignored and must never be printed or committed. Do not copy wallet
keystores. Create a local, ignored symlink at
`stockanalyst/.studio/wallets` pointing to the original worktree's wallet
directory. Verify before deployment that neither the symlink nor its target is
inside `stockanalyst/app/agent`, the AgentCore CodeZip location, or the generated
artifact manifest.

## Selected Architecture

Use staged, independently reversible deployments instead of one monolithic
stack:

1. Upgrade the existing fixed-egress stack to Phase 2.
2. Create mainnet Cognito, Runtime Role, Secrets, and private storage.
3. Generate and deploy a new mainnet AgentCore Runtime.
4. Create a dedicated gateway OAuth client and deploy the mainnet API
   Gateway/Lambda/WAF stack.
5. Update the Runtime with the final mainnet gateway URL.
6. Run fixed-egress and end-to-end mainnet verification.

The resulting request path is:

```text
Internet
  -> mainnet API Gateway + WAF
  -> mainnet Lambda adapter
  -> OAuth-authenticated mainnet AgentCore Runtime
  -> private subnet 172.31.97.0/24
  -> mainnet NAT Gateway
  -> Elastic IP 52.206.204.81
```

Testnet resources, endpoints, Secrets, storage, and Runtime remain unchanged.

## Resource Names and Ownership

### Fixed egress

- Stack: `stockanalyst-mainnet-fixed-egress`
- Existing EIP: `52.206.204.81`
- Existing allocation: `eipalloc-0495ec4219e50cab5`
- Private subnet CIDR: `172.31.97.0/24`
- VPC: `vpc-0352b4298bb340895`
- Public subnet: `subnet-0d048dae4a6c29540`
- Availability Zone: `us-east-1a`

Update the existing stack with `ProvisionNetwork=true`. Before execution,
review a CloudFormation change set and stop if `EgressElasticIp` is replaced,
removed, or otherwise modified.

### AgentCore and identity

- Runtime name: `stockanalyst_stockanalyst_mainnet`
- AgentCore stack: `AgentCore-stockanalyst-mainnet`
- Cognito stack: `stockanalyst-mainnet-cognito`
- Cognito user pool: `stockanalyst-mainnet`
- Cognito domain prefix: `stockanalyst-mainnet-201243086760`
- OAuth scope: `stockanalyst-mainnet/invoke`
- Runtime Role: `bnbagent-stockanalyst-mainnet-runtime`
- Runtime secret: `bnbagent/stockanalyst/mainnet/runtime`
- Job-token secret: `bnbagent/stockanalyst/mainnet/x402-job-token`

Create two confidential Cognito M2M clients. The buyer client is distributed to
approved external A2A buyers. The gateway client is used only by the mainnet
Lambda adapter; its credentials are stored in a dedicated Secrets Manager
secret and are never logged or committed.

The deployment command runs as `AWS_PROFILE=prod`. The Runtime Role is a
separate least-privilege identity used only by the running mainnet Agent. It can
read the two mainnet Secrets, read and write the mainnet private job prefix,
write the public deliverable prefix, publish logs, and perform AgentCore Runtime
operations required by the generated deployment.

### Storage

- Public deliverable bucket: reuse `bnbagent-code-stock-analyst-agent`
- Public base: reuse `https://dcieih6gn5wdm.cloudfront.net`
- Public prefix: `deliverables`
- Private job bucket: `bnbagent-x402-jobs-stockanalyst-mainnet-201243086760`
- Private job prefix: `x402-jobs`

The private job bucket must have all four S3 Public Access Block settings
enabled, must never have Versioning enabled, and must expire `x402-jobs/`
objects after seven days. Do not put private jobs behind the public CloudFront
distribution.

The S3 Runtime provider uses `DELIVERABLE_S3_BUCKET`,
`DELIVERABLE_PUBLIC_BASE`, and `DELIVERABLE_S3_PREFIX`. The removed
`STORAGE_API_URL`, `STORAGE_API_KEY`, and `STORAGE_GATEWAY_URL` variables are
IPFS-only and are not restored.

### Public gateway

- Stack: `stockanalyst-x402-mainnet`
- API stage: `mainnet`
- Lambda function: `stockanalyst-x402-mainnet-adapter`

Reuse the existing SAM template structure and Lambda adapter source, but pass
the new mainnet AgentCore invoke URL and a dedicated mainnet gateway OAuth
secret. Retain the existing six `/x402/*` method/path pairs, WAF rate limits,
API Gateway throttling, 30-day log retention, X-Ray tracing, metrics, and
alarms. The existing testnet stack `Stockanalyst-x402-gateway` and API
`zibqiulyu6` are not updated.

## Mainnet Configuration

The mainnet worktree uses:

- `[network].default = "bsc-mainnet"`
- `[storage].kind = "s3"`
- the existing wallet address
  `0xd10BdDC20E4DC42A1a19a9653e994991e25b8153`
- the mainnet private subnet and security group outputs from the fixed-egress
  stack
- the mainnet Cognito discovery URL and buyer client ID
- only mainnet B402 credentials from `.env.mainnet`
- no `X402_DEMO_MODE`
- no testnet API Gateway URL, testnet job bucket, testnet secret, testnet
  subnet, or testnet security group

The wallet private key remains outside the deploy CodeZip. Reusing the wallet
is an explicit operator choice; testnet and mainnet therefore share the same
on-chain signing identity.

## Deployment Data Flow

The deployment requires two Runtime passes because the gateway requires the
Runtime invoke URL while the Runtime needs the final public gateway URL:

1. Create network, identity, Role, Secrets, and storage prerequisites.
2. Deploy the new Runtime without `X402_GATEWAY_PUBLIC_BASE_URL`.
3. Obtain the Runtime invoke URL.
4. Create the dedicated gateway Cognito client and OAuth secret.
5. Deploy `stockanalyst-x402-mainnet` using the Runtime invoke URL.
6. Read the new API Gateway base URL from stack outputs.
7. Write that URL to the mainnet Runtime configuration and redeploy the Runtime.

Use `bag deploy` for Runtime deployment. Do not use raw `agentcore deploy`,
because it would skip the repository's Runtime secret and IAM policy wiring.

## Deployment Permissions

Before mutation, simulate the `BNBAgentStudio-StockAgent` policy for every
required action. It already has EC2 and CloudFormation full access but lacks at
least some Secrets Manager write, S3 write, and `iam:PassRole` actions.

Create or attach only the additional policy required for these named mainnet
resources and the SAM/AgentCore deployment. Do not attach
`AdministratorAccess`. Scope `iam:PassRole` to
`bnbagent-stockanalyst-mainnet-runtime` and scope S3 and Secrets Manager actions
to the named mainnet resources. Stop rather than substitute broader permissions
when a preflight action is denied.

## Safety and Failure Handling

- Perform exact-name collision checks before creating every named resource.
- A fixed-egress change set that replaces or removes the EIP is rejected.
- Phase 2 network failure rolls back only conditional network resources and
  preserves the Phase 1 EIP.
- Cognito, Runtime prerequisites, Runtime, and gateway are separate rollback
  boundaries.
- A failed gateway deployment does not delete the Runtime.
- A failed Runtime verification blocks gateway and B402 smoke tests.
- Secrets and client credentials are handled in `0600` temporary files, never
  shell arguments or printed output, and removed on success or failure.
- Automated rollback may remove only resources newly created by the current
  mainnet phase. It never deletes testnet resources or releases the mainnet EIP.
- The private bucket is emptied and deleted only during an explicitly approved
  full mainnet rollback.

The complete rollback order is gateway, Runtime, Cognito and mainnet Secrets,
private job bucket, then Phase 2 networking. The EIP and its Phase 1 stack
remain.

## Verification and Success Criteria

Each phase must pass its own static validation, permission preflight, change-set
review, deployment wait, and resource-state checks. Final success requires:

1. `stockanalyst-mainnet-fixed-egress` is `UPDATE_COMPLETE` with the original
   allocation ID and the mainnet NAT, subnet, route, security group, and S3
   endpoint.
2. A request made from the mainnet Runtime to an external IP echo service
   returns exactly `52.206.204.81`.
3. The new Runtime is `READY` and its configuration contains no testnet resource
   identifiers.
4. Mainnet Cognito buyer authentication and Lambda gateway authentication both
   succeed.
5. API Gateway `/mainnet/x402/price`, free analysis, paid async creation, job
   polling, and resume behavior pass their smoke tests.
6. B402 mainnet verification and settlement pass with demo mode disabled.
7. Mainnet private jobs can be written, read through the intended presigned
   path, and expire under the seven-day Lifecycle configuration.
8. CloudWatch logs and API responses contain no wallet password, private key,
   B402 credential, OAuth client secret, access token, payment proof, or job
   token.
9. The existing testnet Runtime remains `READY`, its gateway URL remains
   unchanged, and no testnet stack is updated.

Before any paid smoke test, verify the shared wallet has sufficient BNB gas for
mainnet operations and confirm that `52.206.204.81/32` is present in the B402
mainnet allowlist.
