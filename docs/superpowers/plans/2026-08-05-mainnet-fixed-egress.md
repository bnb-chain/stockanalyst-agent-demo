# Mainnet Fixed Egress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a staged CloudFormation template and deploy Phase 1 to reserve a dedicated mainnet Elastic IP without creating a NAT Gateway.

**Architecture:** A dedicated mainnet stack always owns one VPC Elastic IP. The remaining fixed-egress network is guarded by `ProvisionNetwork`, allowing a later stack update to attach the same allocation to a NAT Gateway and AgentCore private subnet.

**Tech Stack:** AWS CloudFormation, Amazon VPC, Elastic IP, NAT Gateway, AWS CLI

## Global Constraints

- Use AWS account `201243086760` through `AWS_PROFILE=dev`.
- Deploy in `us-east-1`.
- Reuse VPC `vpc-0352b4298bb340895` and public subnet `subnet-0d048dae4a6c29540`.
- Reserve `172.31.97.0/24` for the future mainnet private subnet after rechecking that it is unused.
- Do not modify or reuse testnet EIP `52.73.72.22` or subnet `172.31.96.0/24`.
- Phase 1 must create no NAT Gateway, private subnet, route, security group, or VPC endpoint.

---

### Task 1: Add and validate the staged CloudFormation template

**Files:**
- Create: `infra/agentcore-mainnet-fixed-egress.yaml`

**Interfaces:**
- Consumes: VPC ID, public subnet ID, private subnet CIDR, availability zone, and `ProvisionNetwork` parameter.
- Produces: unconditional `EgressPublicIp` and `EgressAllocationId` outputs; conditional network resource outputs.

- [ ] **Step 1: Confirm the proposed private CIDR is still unused**

Run:

```bash
export AWS_PROFILE=dev
aws ec2 describe-subnets \
  --filters Name=vpc-id,Values=vpc-0352b4298bb340895 \
  --region us-east-1 \
  --query 'Subnets[].CidrBlock' \
  --output text
```

Expected: the output contains `172.31.96.0/24` and does not contain
`172.31.97.0/24`.

- [ ] **Step 2: Create the template with conditional network resources**

Create `infra/agentcore-mainnet-fixed-egress.yaml` with one unconditional
`AWS::EC2::EIP`, a `CreateNetwork` condition equal to
`ProvisionNetwork=true`, and conditional subnet, NAT Gateway, route table,
route, association, security group, and S3 gateway endpoint resources. Tag
every resource with `Project=stockanalyst-agent`, `Environment=mainnet`,
`Purpose=fixed-egress`, and `ManagedBy=cloudformation` where supported.

- [ ] **Step 3: Run static template checks**

Run:

```bash
ruby -e 'require "yaml"; YAML.parse_file("infra/agentcore-mainnet-fixed-egress.yaml"); puts "YAML_OK"'
git diff --check -- infra/agentcore-mainnet-fixed-egress.yaml
```

Expected: `YAML_OK`, no diff errors, and exit status 0.

- [ ] **Step 4: Validate the template with CloudFormation**

Run:

```bash
export AWS_PROFILE=dev
aws cloudformation validate-template \
  --template-body file://infra/agentcore-mainnet-fixed-egress.yaml \
  --region us-east-1
```

Expected: JSON listing all five parameters and no validation error.

- [ ] **Step 5: Commit the template**

```bash
git add infra/agentcore-mainnet-fixed-egress.yaml
git commit -m "infra: add staged mainnet fixed egress stack"
```

Expected: only the new template is included in the commit; the pre-existing
`stockanalyst/app/agent/studio.toml` modification remains unstaged.

### Task 2: Deploy Phase 1 and verify the reserved Elastic IP

**Files:**
- No repository file changes.

**Interfaces:**
- Consumes: `infra/agentcore-mainnet-fixed-egress.yaml` and stack name `stockanalyst-mainnet-fixed-egress`.
- Produces: an AWS-managed mainnet EIP and its public IP/allocation ID.

- [ ] **Step 1: Create the Phase 1 stack**

Run:

```bash
export AWS_PROFILE=dev
aws cloudformation create-stack \
  --stack-name stockanalyst-mainnet-fixed-egress \
  --template-body file://infra/agentcore-mainnet-fixed-egress.yaml \
  --parameters ParameterKey=ProvisionNetwork,ParameterValue=false \
  --tags Key=Project,Value=stockanalyst-agent Key=Environment,Value=mainnet \
  --region us-east-1
```

Expected: a stack ID for `stockanalyst-mainnet-fixed-egress`.

- [ ] **Step 2: Wait for completion**

Run:

```bash
export AWS_PROFILE=dev
aws cloudformation wait stack-create-complete \
  --stack-name stockanalyst-mainnet-fixed-egress \
  --region us-east-1
```

Expected: exit status 0 and stack status `CREATE_COMPLETE`.

- [ ] **Step 3: Read and verify outputs**

Run:

```bash
export AWS_PROFILE=dev
aws cloudformation describe-stacks \
  --stack-name stockanalyst-mainnet-fixed-egress \
  --region us-east-1 \
  --query 'Stacks[0].{Status:StackStatus,Outputs:Outputs}' \
  --output json
```

Expected: `CREATE_COMPLETE` with only `EgressPublicIp` and
`EgressAllocationId` resource outputs.

- [ ] **Step 4: Confirm the EIP is unassociated and tagged mainnet**

Run:

```bash
export AWS_PROFILE=dev
aws ec2 describe-addresses \
  --filters Name=tag:aws:cloudformation:stack-name,Values=stockanalyst-mainnet-fixed-egress \
  --region us-east-1 \
  --query 'Addresses[0].{PublicIp:PublicIp,AllocationId:AllocationId,AssociationId:AssociationId,Tags:Tags}' \
  --output json
```

Expected: the public IP matches the stack output, `AssociationId` is absent,
and tags include `Environment=mainnet`.

- [ ] **Step 5: Confirm no Phase 2 resources exist**

Run:

```bash
export AWS_PROFILE=dev
aws cloudformation list-stack-resources \
  --stack-name stockanalyst-mainnet-fixed-egress \
  --region us-east-1 \
  --query 'StackResourceSummaries[].ResourceType' \
  --output text
```

Expected: exactly one resource type, `AWS::EC2::EIP`.
