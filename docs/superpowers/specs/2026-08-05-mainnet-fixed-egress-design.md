# Mainnet Fixed Egress Design

## Goal

Reserve a dedicated, stable public IPv4 address for the stock analyst mainnet
agent before the AgentCore Runtime is deployed. The address can be submitted to
the B402 allowlist immediately and must remain unchanged when the Runtime is
connected later.

## Constraints

- AWS account: `201243086760`, operated with `AWS_PROFILE=dev`.
- Region: `us-east-1`, matching the existing AgentCore deployment pattern.
- Existing VPC: `vpc-0352b4298bb340895` (`172.31.0.0/16`).
- Existing testnet private subnet: `172.31.96.0/24`.
- Mainnet must not reuse or modify the testnet EIP `52.73.72.22` or its network
  resources.
- The mainnet Runtime does not exist yet, so creating a NAT Gateway now would
  add cost without carrying traffic.

## Considered Approaches

### 1. Staged single CloudFormation template (selected)

One mainnet template always creates the Elastic IP. A boolean
`ProvisionNetwork` parameter conditionally creates the NAT Gateway, private
subnet, route table, Runtime security group, and S3 gateway endpoint. It
defaults to `false` for the first deployment. Updating the same stack with
`ProvisionNetwork=true` later preserves the EIP and completes the network.

This keeps one ownership boundary, avoids manual EIP import, and avoids NAT
Gateway charges until the Runtime is ready.

### 2. Create the complete network immediately

This is simpler operationally but starts NAT Gateway charges before the
mainnet Runtime exists. It is unnecessary for the current goal.

### 3. Separate EIP and network stacks

This isolates lifecycles but requires cross-stack exports or explicit EIP
allocation parameters and creates more deployment surface for one agent.

## Template Design

Create `infra/agentcore-mainnet-fixed-egress.yaml` with these parameters:

- `VpcId`, defaulting to the existing VPC.
- `PublicSubnetId`, defaulting to the existing public subnet in `us-east-1a`.
- `PrivateSubnetCidr`, defaulting to the currently unused
  `172.31.97.0/24`.
- `AvailabilityZone`, defaulting to `us-east-1a`.
- `ProvisionNetwork`, defaulting to `false`.

The EIP is unconditional and tagged with `Environment=mainnet`. All other
resources are conditional on `ProvisionNetwork=true`. Outputs always include
`EgressPublicIp` and `EgressAllocationId`; conditional network outputs return
the created resource identifiers only after the network is enabled.

Resource names and tags include `mainnet` so they cannot be confused with the
existing testnet resources.

## Deployment Flow

### Phase 1: reserve the address

1. Validate the template.
2. Create a dedicated stack with `ProvisionNetwork=false`.
3. Wait for `CREATE_COMPLETE`.
4. Read `EgressPublicIp` and `EgressAllocationId` from stack outputs.
5. Confirm the EIP has no association and is owned by the stack.
6. Provide `<EgressPublicIp>/32` to B402 for allowlisting.

### Phase 2: connect the mainnet Runtime

1. Update the same stack with `ProvisionNetwork=true`.
2. Wait for the NAT Gateway and routes to become available.
3. Deploy or update the mainnet AgentCore Runtime in VPC mode using the stack's
   private subnet and security group outputs.
4. Invoke an external IP echo endpoint from the Runtime and verify it reports
   exactly the reserved EIP.
5. Run the B402 mainnet smoke test and correlate its response with CloudWatch
   logs.

## Failure and Safety Behavior

- CloudFormation owns the EIP; it must not be manually associated or released.
- The template never references the testnet stack or EIP.
- If Phase 1 fails, inspect stack events before retrying; do not create a second
  unmanaged EIP.
- A later network update must not replace `EgressElasticIp`. A change set should
  be reviewed before execution if the EIP resource definition changes.
- Stack deletion releases the EIP. Production deletion protection should be
  enabled after successful creation where account permissions permit it.

## Verification

- Run `aws cloudformation validate-template` before deployment.
- Confirm the selected CIDR does not overlap any VPC subnet.
- Confirm stack status is `CREATE_COMPLETE`.
- Confirm the stack output, EC2 EIP record, and B402 allowlist value match.
- Confirm Phase 1 creates no NAT Gateway or private subnet.
- When Phase 2 is enabled, confirm the NAT Gateway uses the original
  `EgressAllocationId`.
