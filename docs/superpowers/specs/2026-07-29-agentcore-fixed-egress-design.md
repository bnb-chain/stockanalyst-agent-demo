# AgentCore Fixed Egress Design

Date: 2026-07-29

## Goal

Give the existing `stockanalyst` AgentCore Runtime one stable public IPv4
address for outbound B402 calls, without changing its runtime identity,
Cognito authentication, seller wallet, chain contracts, S3/CloudFront
delivery, or public invoke URL.

## Current State

- AWS account: `201243086760`
- Region: `us-east-1`
- Runtime ID: `stockanalyst_stockanalyst-hrXlh1BUtQ`
- Runtime ARN:
  `arn:aws:bedrock-agentcore:us-east-1:201243086760:runtime/stockanalyst_stockanalyst-hrXlh1BUtQ`
- Runtime network mode: `PUBLIC`
- Existing VPC: `vpc-0352b4298bb340895` (`172.31.0.0/16`)
- Existing public subnet selected for NAT:
  `subnet-0d048dae4a6c29540` in `us-east-1a` / `use1-az1`
- Default VPC main route table: `rtb-07d96562411b9e265`, with active
  `0.0.0.0/0` route through `igw-0686a4d1007f62fe6`
- Private subnet CIDR: `172.31.96.0/24`; it does not overlap any existing
  subnet in the VPC
- No existing NAT Gateway or Elastic IP is available.

## Considered Approaches

### 1. Reuse the default VPC with one NAT Gateway and Elastic IP

This is the selected approach. It creates the fewest resources, preserves the
existing Runtime and authentication surfaces, and gives B402 one address to
allowlist.

Trade-offs:

- one Availability Zone is sufficient for testnet and minimizes cost;
- the NAT Gateway is a single point of failure;
- the NAT Gateway has hourly and data-processing charges.

### 2. Create a dedicated multi-AZ production VPC

This gives stronger isolation and availability, but requires two NAT Gateways
and two allowlisted IP addresses. It is outside the current testnet scope.

### 3. Add an outbound proxy with an Elastic IP

This could reduce NAT cost at very low traffic, but adds proxy credentials,
patching, monitoring, and application proxy configuration. It is less
convenient and introduces another trusted component, so it is rejected.

## Architecture

```text
Existing AgentCore Runtime
  networkMode = VPC
          |
          v
Private subnet 172.31.96.0/24 (us-east-1a)
          |
          | 0.0.0.0/0
          v
NAT Gateway in subnet-0d048dae4a6c29540
          |
          v
Elastic IP (the B402 allowlist address)
          |
          v
Internet Gateway -> B402 / OpenRouter / BSC RPC
```

The private subnet uses a dedicated route table. The AgentCore security group
has no inbound rules and permits outbound traffic required by the agent.
Outbound is initially unrestricted because the runtime currently uses HTTPS
services plus BSC RPC endpoints that may use port `8545`; domain names cannot
be expressed in security-group rules. Network egress can be narrowed after
the exact production endpoints are finalized.

## Resource Names and Tags

All created resources use the tags:

```text
Project=stockanalyst-agent
Purpose=fixed-egress
ManagedBy=codex
Environment=testnet
```

Names:

```text
stockanalyst-fixed-egress-eip
stockanalyst-fixed-egress-nat
stockanalyst-agentcore-private-us-east-1a
stockanalyst-agentcore-private-rt
stockanalyst-agentcore-vpc-sg
stockanalyst-agentcore-s3-endpoint
```

## AgentCore Configuration

The source-of-truth file `stockanalyst/agentcore/agentcore.json` changes from:

```json
"networkMode": "PUBLIC"
```

to VPC configuration using the created private subnet and security group. The
source configuration uses:

```json
{
  "networkMode": "VPC",
  "networkConfig": {
    "subnets": ["the subnet tagged Name=stockanalyst-agentcore-private-us-east-1a"],
    "securityGroups": ["the security group tagged Name=stockanalyst-agentcore-vpc-sg"]
  }
}
```

The generated CDK template must map those values to
`NetworkConfiguration.NetworkMode=VPC` and
`NetworkConfiguration.NetworkModeConfig`. A dedicated S3 Gateway VPC endpoint
is associated with the private route table so AgentCore can retrieve its
CodeZip and use S3 without depending on the NAT path.

Deployment continues to use `bag deploy`, never raw `agentcore deploy`, so
runtime secrets and wallet handling remain unchanged. Every AWS CLI sequence
begins with `export AWS_PROFILE=dev`.

## Deployment Safety

- Reuse the existing Runtime and CloudFormation stack; do not create another
  seller runtime.
- Do not create, fund, submit, or settle any ERC-8183 job.
- Do not print wallet passwords, private keys, OpenRouter keys, OAuth secrets,
  or runtime secret values.
- Use a fresh AgentCore session after deployment so the invocation cannot stay
  pinned to a pre-update microVM.
- Preserve the current Cognito Client ID, discovery URL, invoke URL, seller
  address, S3 bucket, CloudFront distribution, and contract addresses.

## Verification

Before changing the Runtime:

1. Confirm the public subnet route table has an Internet Gateway default route.
2. Confirm the selected private CIDR does not overlap an existing subnet.
3. Create the Elastic IP, NAT Gateway, private subnet, route table, security
   group, and S3 Gateway VPC endpoint.
4. Wait until the NAT Gateway is `available`.

After deployment:

1. CloudFormation reaches `UPDATE_COMPLETE`.
2. Runtime reaches `READY` and reports `networkMode=VPC`.
3. The Runtime ID and ARN remain unchanged.
4. A fresh authenticated `negotiate` request succeeds.
5. EC2 reports the NAT Gateway public address as exactly the allocated Elastic
   IP; this is the address delivered to B402 for allowlisting.
6. OpenRouter, BSC Testnet RPC, and S3/CloudFront connectivity remain
   functional.
7. After B402 applies the allowlist, a B402 operation from a fresh Runtime
   session succeeds and its CloudWatch trace contains no IP-denial response.

## Rollback

If VPC deployment or connectivity verification fails:

1. Restore `networkMode=PUBLIC` through the existing `bag deploy` path.
2. Wait for the Runtime to return to `READY`.
3. Verify a fresh authenticated `negotiate` call.
4. Delete the NAT Gateway and wait until deletion completes.
5. Release the Elastic IP.
6. Delete the private route, route table association, route table, security
   group, and private subnet.

The Runtime is restored before deleting network resources so active VPC-mode
sessions are not stranded.
