# AgentCore Fixed Egress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every outbound call from the existing stockanalyst AgentCore Runtime through one NAT Gateway Elastic IP that B402 can allowlist.

**Architecture:** Reuse the default VPC in `us-east-1`, put a new AgentCore private subnet in `us-east-1a`, and route it through a NAT Gateway in the existing public subnet. Manage the network as a separate CloudFormation stack, then update the ignored local `agentcore.json` and deploy the existing Runtime in place with `bag deploy`.

**Tech Stack:** AWS CloudFormation, Amazon VPC, NAT Gateway, Elastic IP, S3 Gateway VPC Endpoint, Bedrock AgentCore, Cognito OAuth, AWS CLI, bnbagent-studio.

## Global Constraints

- Every AWS CLI sequence begins with `export AWS_PROFILE=dev`.
- AWS account is `201243086760`; region is `us-east-1`.
- Reuse VPC `vpc-0352b4298bb340895` and public subnet `subnet-0d048dae4a6c29540`.
- Create private subnet `172.31.96.0/24` in `us-east-1a`.
- Reuse Runtime `stockanalyst_stockanalyst-hrXlh1BUtQ`; never create a second seller Runtime.
- Preserve Cognito, seller wallet, contracts, invoke URL, S3 bucket, CloudFront distribution, and existing Runtime ARN.
- Do not create, fund, submit, settle, or otherwise mutate an ERC-8183 job.
- Never print or commit wallet passwords, private keys, OpenRouter keys, OAuth secrets, runtime secret values, keystores, `.env` files, or generated reports.
- Deploy the Runtime with `bag deploy`, never raw `agentcore deploy`.
- NAT Gateway and Elastic IP incur charges; delete them if the VPC migration is rolled back.
- Use a fresh AgentCore session after deployment.

---

### Task 1: Add the Fixed-Egress Network Template

**Files:**
- Create: `infra/agentcore-fixed-egress.yaml`

**Interfaces:**
- Consumes: existing default VPC, public subnet, Internet Gateway route
- Produces: CloudFormation outputs `EgressPublicIp`, `EgressAllocationId`, `NatGatewayId`, `PrivateSubnetId`, `RuntimeSecurityGroupId`, `PrivateRouteTableId`, `S3EndpointId`

- [ ] **Step 1: Create the CloudFormation template**

Create `infra/agentcore-fixed-egress.yaml` with:

```yaml
AWSTemplateFormatVersion: "2010-09-09"
Description: Fixed outbound IPv4 for the stockanalyst AgentCore Runtime

Parameters:
  VpcId:
    Type: AWS::EC2::VPC::Id
    Default: vpc-0352b4298bb340895
  PublicSubnetId:
    Type: AWS::EC2::Subnet::Id
    Default: subnet-0d048dae4a6c29540
  PrivateSubnetCidr:
    Type: String
    Default: 172.31.96.0/24
    AllowedPattern: ^172\.31\.96\.0/24$
  AvailabilityZone:
    Type: AWS::EC2::AvailabilityZone::Name
    Default: us-east-1a

Resources:
  PrivateSubnet:
    Type: AWS::EC2::Subnet
    Properties:
      VpcId: !Ref VpcId
      AvailabilityZone: !Ref AvailabilityZone
      CidrBlock: !Ref PrivateSubnetCidr
      MapPublicIpOnLaunch: false
      Tags:
        - Key: Name
          Value: stockanalyst-agentcore-private-us-east-1a
        - Key: Project
          Value: stockanalyst-agent
        - Key: Purpose
          Value: fixed-egress
        - Key: ManagedBy
          Value: cloudformation
        - Key: Environment
          Value: testnet

  EgressElasticIp:
    Type: AWS::EC2::EIP
    Properties:
      Domain: vpc
      Tags:
        - Key: Name
          Value: stockanalyst-fixed-egress-eip
        - Key: Project
          Value: stockanalyst-agent
        - Key: Purpose
          Value: fixed-egress
        - Key: ManagedBy
          Value: cloudformation
        - Key: Environment
          Value: testnet

  NatGateway:
    Type: AWS::EC2::NatGateway
    Properties:
      AllocationId: !GetAtt EgressElasticIp.AllocationId
      ConnectivityType: public
      SubnetId: !Ref PublicSubnetId
      Tags:
        - Key: Name
          Value: stockanalyst-fixed-egress-nat
        - Key: Project
          Value: stockanalyst-agent
        - Key: Purpose
          Value: fixed-egress
        - Key: ManagedBy
          Value: cloudformation
        - Key: Environment
          Value: testnet

  PrivateRouteTable:
    Type: AWS::EC2::RouteTable
    Properties:
      VpcId: !Ref VpcId
      Tags:
        - Key: Name
          Value: stockanalyst-agentcore-private-rt
        - Key: Project
          Value: stockanalyst-agent
        - Key: Purpose
          Value: fixed-egress
        - Key: ManagedBy
          Value: cloudformation
        - Key: Environment
          Value: testnet

  PrivateDefaultRoute:
    Type: AWS::EC2::Route
    DependsOn: NatGateway
    Properties:
      RouteTableId: !Ref PrivateRouteTable
      DestinationCidrBlock: 0.0.0.0/0
      NatGatewayId: !Ref NatGateway

  PrivateSubnetRouteTableAssociation:
    Type: AWS::EC2::SubnetRouteTableAssociation
    Properties:
      RouteTableId: !Ref PrivateRouteTable
      SubnetId: !Ref PrivateSubnet

  RuntimeSecurityGroup:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: Outbound network access for stockanalyst AgentCore
      VpcId: !Ref VpcId
      SecurityGroupEgress:
        - Description: Agent outbound connectivity
          IpProtocol: "-1"
          CidrIp: 0.0.0.0/0
      Tags:
        - Key: Name
          Value: stockanalyst-agentcore-vpc-sg
        - Key: Project
          Value: stockanalyst-agent
        - Key: Purpose
          Value: fixed-egress
        - Key: ManagedBy
          Value: cloudformation
        - Key: Environment
          Value: testnet

  S3GatewayEndpoint:
    Type: AWS::EC2::VPCEndpoint
    Properties:
      VpcEndpointType: Gateway
      VpcId: !Ref VpcId
      ServiceName: !Sub com.amazonaws.${AWS::Region}.s3
      RouteTableIds:
        - !Ref PrivateRouteTable
      Tags:
        - Key: Name
          Value: stockanalyst-agentcore-s3-endpoint
        - Key: Project
          Value: stockanalyst-agent
        - Key: Purpose
          Value: fixed-egress
        - Key: ManagedBy
          Value: cloudformation
        - Key: Environment
          Value: testnet

Outputs:
  EgressPublicIp:
    Description: Public IPv4 address to provide to B402
    Value: !Ref EgressElasticIp
  EgressAllocationId:
    Value: !GetAtt EgressElasticIp.AllocationId
  NatGatewayId:
    Value: !Ref NatGateway
  PrivateSubnetId:
    Value: !Ref PrivateSubnet
  RuntimeSecurityGroupId:
    Value: !GetAtt RuntimeSecurityGroup.GroupId
  PrivateRouteTableId:
    Value: !Ref PrivateRouteTable
  S3EndpointId:
    Value: !Ref S3GatewayEndpoint
```

- [ ] **Step 2: Validate template syntax with AWS**

Run:

```bash
export AWS_PROFILE=dev
aws cloudformation validate-template \
  --region us-east-1 \
  --template-body file://infra/agentcore-fixed-egress.yaml
```

Expected: exit `0` and a parameter list containing `VpcId`,
`PublicSubnetId`, `PrivateSubnetCidr`, and `AvailabilityZone`.

- [ ] **Step 3: Confirm the template contains no replacement or destructive resources**

Run:

```bash
rg -n "AWS::EC2::(Subnet|EIP|NatGateway|RouteTable|Route|SubnetRouteTableAssociation|SecurityGroup|VPCEndpoint)" \
  infra/agentcore-fixed-egress.yaml
git diff --check -- infra/agentcore-fixed-egress.yaml
```

Expected: only the eight intended network resource types appear and whitespace
check passes.

- [ ] **Step 4: Commit the template**

```bash
git add infra/agentcore-fixed-egress.yaml
git commit -m "infra: define AgentCore fixed egress"
```

### Task 2: Deploy and Verify the Network Stack

**Files:**
- No source changes

**Interfaces:**
- Consumes: `infra/agentcore-fixed-egress.yaml`
- Produces: concrete private subnet ID, security group ID, NAT Gateway ID, and fixed public IPv4

- [ ] **Step 1: Reconfirm preconditions immediately before creating billable resources**

Run:

```bash
export AWS_PROFILE=dev
aws ec2 describe-subnets \
  --region us-east-1 \
  --filters Name=vpc-id,Values=vpc-0352b4298bb340895 \
  --query 'Subnets[].CidrBlock' \
  --output json
aws ec2 describe-nat-gateways \
  --region us-east-1 \
  --filter Name=tag:Project,Values=stockanalyst-agent Name=state,Values=pending,available \
  --query 'NatGateways[].NatGatewayId' \
  --output json
aws ec2 describe-addresses \
  --region us-east-1 \
  --filters Name=tag:Project,Values=stockanalyst-agent \
  --query 'Addresses[].AllocationId' \
  --output json
```

Expected: `172.31.96.0/24` is absent and no existing tagged NAT/EIP exists. If
tagged resources exist, stop and reconcile them instead of creating duplicates.

- [ ] **Step 2: Deploy the separate network stack**

Run:

```bash
export AWS_PROFILE=dev
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name AgentCore-stockanalyst-fixed-egress \
  --template-file infra/agentcore-fixed-egress.yaml \
  --parameter-overrides \
    VpcId=vpc-0352b4298bb340895 \
    PublicSubnetId=subnet-0d048dae4a6c29540 \
    PrivateSubnetCidr=172.31.96.0/24 \
    AvailabilityZone=us-east-1a \
  --tags \
    Project=stockanalyst-agent \
    Purpose=fixed-egress \
    ManagedBy=cloudformation \
    Environment=testnet
```

Expected: `Successfully created/updated stack -
AgentCore-stockanalyst-fixed-egress`.

- [ ] **Step 3: Record the exact stack outputs**

Run:

```bash
export AWS_PROFILE=dev
aws cloudformation describe-stacks \
  --region us-east-1 \
  --stack-name AgentCore-stockanalyst-fixed-egress \
  --query 'Stacks[0].{Status:StackStatus,Outputs:Outputs}' \
  --output json
```

Expected: `Status=CREATE_COMPLETE` and all seven outputs from Task 1 are
present. Copy `PrivateSubnetId`, `RuntimeSecurityGroupId`, and
`EgressPublicIp` into the Task 3 working notes.

- [ ] **Step 4: Verify routing and the static public address**

Load and validate the exact IDs returned by the stack, then run the read-only
queries:

```bash
export AWS_PROFILE=dev
STACK_OUTPUTS="$(aws cloudformation describe-stacks \
  --region us-east-1 \
  --stack-name AgentCore-stockanalyst-fixed-egress \
  --query 'Stacks[0].Outputs' \
  --output json)"
NAT_GATEWAY_ID="$(jq -er '.[] | select(.OutputKey == "NatGatewayId") | .OutputValue' <<<"$STACK_OUTPUTS")"
PRIVATE_ROUTE_TABLE_ID="$(jq -er '.[] | select(.OutputKey == "PrivateRouteTableId") | .OutputValue' <<<"$STACK_OUTPUTS")"
S3_ENDPOINT_ID="$(jq -er '.[] | select(.OutputKey == "S3EndpointId") | .OutputValue' <<<"$STACK_OUTPUTS")"
EGRESS_PUBLIC_IP="$(jq -er '.[] | select(.OutputKey == "EgressPublicIp") | .OutputValue' <<<"$STACK_OUTPUTS")"
test -n "$NAT_GATEWAY_ID"
test -n "$PRIVATE_ROUTE_TABLE_ID"
test -n "$S3_ENDPOINT_ID"
test -n "$EGRESS_PUBLIC_IP"
aws ec2 describe-nat-gateways \
  --region us-east-1 \
  --nat-gateway-ids "$NAT_GATEWAY_ID" \
  --query 'NatGateways[0].{State:State,SubnetId:SubnetId,PublicIp:NatGatewayAddresses[0].PublicIp}' \
  --output json
aws ec2 describe-route-tables \
  --region us-east-1 \
  --route-table-ids "$PRIVATE_ROUTE_TABLE_ID" \
  --query 'RouteTables[0].{Associations:Associations,Routes:Routes}' \
  --output json
aws ec2 describe-vpc-endpoints \
  --region us-east-1 \
  --vpc-endpoint-ids "$S3_ENDPOINT_ID" \
  --query 'VpcEndpoints[0].{State:State,ServiceName:ServiceName,RouteTableIds:RouteTableIds}' \
  --output json
```

Expected:

- NAT Gateway state is `available`;
- NAT public IP equals `EgressPublicIp`;
- private route table has `0.0.0.0/0` through the NAT Gateway;
- the private subnet is associated with the private route table;
- S3 endpoint state is `available` and references the private route table.

### Task 3: Configure and Synthesize AgentCore VPC Mode

**Files:**
- Modify local ignored file: `stockanalyst/agentcore/agentcore.json`
- Generated only: `stockanalyst/agentcore/cdk/cdk.out/`

**Interfaces:**
- Consumes: `PrivateSubnetId` and `RuntimeSecurityGroupId` outputs
- Produces: synthesized `AWS::BedrockAgentCore::Runtime` with VPC networking

- [ ] **Step 1: Back up the non-secret generated configuration**

Run:

```bash
cp stockanalyst/agentcore/agentcore.json \
  /private/tmp/stockanalyst-agentcore-before-fixed-egress.json
```

Do not copy `.env.local`, keystores, or secret files.

- [ ] **Step 2: Patch the Runtime entry**

Use `apply_patch` to replace:

```json
"networkMode": "PUBLIC",
```

with:

```json
"networkMode": "VPC",
"networkConfig": {
  "subnets": ["the exact PrivateSubnetId output from Task 2"],
  "securityGroups": ["the exact RuntimeSecurityGroupId output from Task 2"]
},
```

The two output descriptions above are instructions to insert the literal AWS
IDs; do not leave descriptive strings in `agentcore.json`.

- [ ] **Step 3: Validate the AgentCore project**

Run from `stockanalyst`:

```bash
export AWS_PROFILE=dev
export PATH=/Users/zhaoyu/.nvm/versions/node/v24.10.0/bin:/Users/zhaoyu/.local/bin:/usr/local/bin:/usr/bin:/bin
agentcore validate --json
```

Expected: project validation succeeds with no schema error for
`networkConfig`.

- [ ] **Step 4: Synthesize CloudFormation without deploying**

Run from `stockanalyst/agentcore/cdk`:

```bash
export AWS_PROFILE=dev
export PATH=/Users/zhaoyu/.nvm/versions/node/v24.10.0/bin:/Users/zhaoyu/.local/bin:/usr/local/bin:/usr/bin:/bin
./node_modules/.bin/cdk synth
```

Expected: exit `0`.

- [ ] **Step 5: Inspect the generated Runtime network configuration**

Run:

```bash
jq '.Resources.ApplicationAgentStockanalystRuntimeA85B85C9.Properties.NetworkConfiguration' \
  stockanalyst/agentcore/cdk/cdk.out/AgentCore-stockanalyst-default.template.json
```

Expected:

```json
{
  "NetworkMode": "VPC",
  "NetworkModeConfig": {
    "SecurityGroups": ["the exact RuntimeSecurityGroupId output from Task 2"],
    "Subnets": ["the exact PrivateSubnetId output from Task 2"]
  }
}
```

Do not proceed if the generated template contains `PUBLIC`, a public subnet, or
an unexpected security group.

### Task 4: Update the Existing Runtime In Place

**Files:**
- Runtime deployment state may update ignored local files under `stockanalyst/agentcore/`
- `stockanalyst/app/agent/studio.toml` may update only deployment timestamps and the unchanged Runtime ARN

**Interfaces:**
- Consumes: validated VPC-mode `agentcore.json`
- Produces: same Runtime ID/ARN in `READY` state with `networkMode=VPC`

- [ ] **Step 1: Record pre-deploy identity**

Run:

```bash
export AWS_PROFILE=dev
aws bedrock-agentcore-control get-agent-runtime \
  --region us-east-1 \
  --agent-runtime-id stockanalyst_stockanalyst-hrXlh1BUtQ \
  --query '{Id:agentRuntimeId,Arn:agentRuntimeArn,Status:status,Network:networkConfiguration}' \
  --output json
```

Expected: the known Runtime ID/ARN and `NetworkMode=PUBLIC`.

- [ ] **Step 2: Deploy through bnbagent-studio**

Run from `stockanalyst/app/agent`. The password is read into process memory
without printing:

```bash
export AWS_PROFILE=dev
export PATH=/Users/zhaoyu/.nvm/versions/node/v24.10.0/bin:/Users/zhaoyu/.local/bin:/usr/local/bin:/usr/bin:/bin
export WALLET_PASSWORD="$(../../.venv/bin/python -c 'from pathlib import Path; p=Path("../../.studio/.env.local"); line=next(x for x in p.read_text().splitlines() if x.startswith("WALLET_PASSWORD=")); value=line.split("=",1)[1].strip(); print(value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\047\042" else value, end="")')"
/Users/zhaoyu/corp/bnbchain/chain_middleware/bnbchain-studio/.venv/bin/bag deploy agent \
  --project-root /Users/zhaoyu/corp/bnbchain/chain_middleware/stockanalyst-agent-demo/stockanalyst/app/agent \
  --skip-prepare \
  --accept-risk \
  --force \
  --force-deploy-broken-storage
```

Expected: stack `AgentCore-stockanalyst-default` reaches `UPDATE_COMPLETE`.
Secret values must not appear in output.

- [ ] **Step 3: Verify the Runtime identity and VPC mode**

Run:

```bash
export AWS_PROFILE=dev
aws cloudformation describe-stacks \
  --region us-east-1 \
  --stack-name AgentCore-stockanalyst-default \
  --query 'Stacks[0].{Status:StackStatus,Updated:LastUpdatedTime}' \
  --output json
aws bedrock-agentcore-control get-agent-runtime \
  --region us-east-1 \
  --agent-runtime-id stockanalyst_stockanalyst-hrXlh1BUtQ \
  --query '{Id:agentRuntimeId,Arn:agentRuntimeArn,Status:status,Network:networkConfiguration}' \
  --output json
```

Expected:

- stack is `UPDATE_COMPLETE`;
- Runtime is `READY`;
- Runtime ID and ARN are unchanged;
- `Network.networkMode=VPC`;
- the exact private subnet and security group appear in
  `Network.networkModeConfig`.

### Task 5: Smoke Test and Handoff the Allowlist IP

**Files:**
- No source changes
- Do not create reports or jobs

**Interfaces:**
- Consumes: deployed VPC-mode Runtime and Cognito client credentials
- Produces: successful authenticated invocation, fixed EIP handoff, and
  rollback decision

- [ ] **Step 1: Obtain OAuth credentials transiently**

Run in a shell with tracing disabled:

```bash
set +x
export AWS_PROFILE=dev
CLIENT_ID="7592i53k46tpd4ecslp1g1gls8"
CLIENT_SECRET="$(aws cognito-idp describe-user-pool-client \
  --region us-east-1 \
  --user-pool-id us-east-1_aH4tfhMnq \
  --client-id "$CLIENT_ID" \
  --query 'UserPoolClient.ClientSecret' \
  --output text)"
ACCESS_TOKEN="$(curl --silent --show-error --fail \
  --request POST \
  --user "${CLIENT_ID}:${CLIENT_SECRET}" \
  --header 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'grant_type=client_credentials' \
  --data-urlencode 'scope=bnbagent-seller/invoke' \
  'https://bnbagent-seller-201243086760.auth.us-east-1.amazoncognito.com/oauth2/token' |
  jq -er '.access_token')"
test -n "$ACCESS_TOKEN"
```

Do not print either secret variable.

- [ ] **Step 2: Invoke `negotiate` with a fresh session**

Use a new session ID beginning with:

```text
stockanalyst-fixed-egress-20260729-
```

Send the A2A `message/send` payload as a `kind=data` part:

```json
{
  "skill": "negotiate",
  "task_description": "Connectivity smoke test for fixed egress",
  "terms": {
    "deliverables": "Signed quote only",
    "quality_standards": "No job creation or funding"
  }
}
```

Expected: a signed quote is returned. Do not call `notify_funded` and do not
create or fund a job.

Exact invocation:

```bash
SESSION_ID="stockanalyst-fixed-egress-20260729-0001"
AGENT_ENDPOINT="https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A201243086760%3Aruntime%2Fstockanalyst_stockanalyst-hrXlh1BUtQ/invocations?qualifier=DEFAULT"
curl --silent --show-error --fail \
  --request POST \
  --header "Authorization: Bearer ${ACCESS_TOKEN}" \
  --header 'Content-Type: application/json' \
  --header "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: ${SESSION_ID}" \
  --data '{
    "jsonrpc": "2.0",
    "id": "fixed-egress-smoke-1",
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "messageId": "fixed-egress-smoke-message-1",
        "parts": [
          {
            "kind": "data",
            "data": {
              "skill": "negotiate",
              "task_description": "Connectivity smoke test for fixed egress",
              "terms": {
                "deliverables": "Signed quote only",
                "quality_standards": "No job creation or funding"
              }
            }
          }
        ]
      }
    }
  }' \
  "$AGENT_ENDPOINT" |
  jq .
```

- [ ] **Step 3: Correlate CloudWatch startup and invocation**

Run:

```bash
export AWS_PROFILE=dev
aws logs filter-log-events \
  --region us-east-1 \
  --log-group-name /aws/bedrock-agentcore/runtimes/stockanalyst_stockanalyst-hrXlh1BUtQ-DEFAULT \
  --start-time "$(( $(date +%s) * 1000 - 600000 ))"
```

Expected: the fresh session invocation completes without Runtime startup, DNS,
or connection errors. The signed quote response is the primary smoke-test
evidence; the task description is not required to appear in logs.

- [ ] **Step 4: Report the fixed IP**

Read `EgressPublicIp` from stack `AgentCore-stockanalyst-fixed-egress` and
provide it to the user as the sole B402 allowlist IP with `/32`.

- [ ] **Step 5: Preserve the verification boundary**

Record that B402 end-to-end verification is pending until the external B402
operator applies the allowlist. Do not claim B402 success before that external
change. Once they confirm, run one separately authorized B402 test and inspect
CloudWatch for an IP-denial response.

- [ ] **Step 6: Final repository audit**

Run:

```bash
git status --short
git diff --cached --name-only
```

Expected: generated job reports remain untracked, secrets and generated
AgentCore files remain ignored, and no unintended files are staged.

### Conditional Rollback: Restore Public Mode

Run this only if the Runtime cannot reach `READY` or the authenticated smoke
test fails because of VPC connectivity.

- [ ] **Step 1: Restore the temporary `agentcore.json` backup**

Restore only `stockanalyst/agentcore/agentcore.json`, then validate that it
contains `networkMode=PUBLIC` and no `networkConfig`.

- [ ] **Step 2: Redeploy through the same `bag deploy` command**

Expected: existing Runtime returns to `READY` with `networkMode=PUBLIC`.

- [ ] **Step 3: Delete the billable network stack**

Only after the Runtime is back in public mode:

```bash
export AWS_PROFILE=dev
aws cloudformation delete-stack \
  --region us-east-1 \
  --stack-name AgentCore-stockanalyst-fixed-egress
aws cloudformation wait stack-delete-complete \
  --region us-east-1 \
  --stack-name AgentCore-stockanalyst-fixed-egress
```

Expected: NAT Gateway is deleted and Elastic IP is released by
CloudFormation.
