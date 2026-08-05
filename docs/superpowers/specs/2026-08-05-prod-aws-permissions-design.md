# Prod AWS Permissions Design

## Goal

Allow the existing IAM user `BNBAgentStudio-StockAgent`, used by
`AWS_PROFILE=prod`, to manage EC2 resources and CloudFormation stacks in AWS
account `201243086760`. This includes the existing mainnet Elastic IP
`52.206.204.81` and its stack `stockanalyst-mainnet-fixed-egress`.

## Selected Approach

Attach these AWS managed policies directly to the existing IAM user:

- `arn:aws:iam::aws:policy/AmazonEC2FullAccess`
- `arn:aws:iam::aws:policy/AWSCloudFormationFullAccess`

Do not attach `AdministratorAccess`. Do not create a user or role, transfer the
Elastic IP, change the Elastic IP association, or modify the CloudFormation
stack as part of this permission change.

## Operational Flow

1. Verify `AWS_PROFILE=prod` resolves to account `201243086760` and IAM user
   `BNBAgentStudio-StockAgent`.
2. Use an IAM administrator identity to attach the two selected AWS managed
   policies to that user.
3. Verify both policies appear in the user's attached-policy list.
4. Verify `prod` can describe the existing Elastic IP and CloudFormation stack.
5. Verify no Elastic IP, stack, subnet, NAT Gateway, or IAM role was created,
   deleted, transferred, or modified.

## Security Boundary

These policies grant broad control over EC2 and CloudFormation resources in the
account, not only the stockanalyst resources. They do not grant general IAM
administration. A future AgentCore deployment may separately require permission
to pass an existing runtime execution role; that permission is outside this
change and will be handled only when deployment is requested.

## Failure and Rollback

If either attachment fails, inspect the IAM error and do not substitute a broader
policy. Rollback consists of detaching the two policy ARNs from
`BNBAgentStudio-StockAgent`. Detaching them does not delete or alter EC2 or
CloudFormation resources.

## Success Criteria

- Both selected AWS managed policies are attached to `BNBAgentStudio-StockAgent`.
- `AWS_PROFILE=prod` can read `52.206.204.81` and
  `stockanalyst-mainnet-fixed-egress`.
- `AdministratorAccess` is not attached.
- The Elastic IP and CloudFormation stack remain unchanged.
