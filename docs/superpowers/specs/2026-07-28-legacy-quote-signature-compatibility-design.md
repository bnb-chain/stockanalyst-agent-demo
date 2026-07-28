# Legacy Quote Signature Compatibility Design

## Context

ERC-8183 job `398` was created and funded from buyer
`0x964694e83527f2aD24101499dC83e335D2B353C7` for seller
`0xd10BdDC20E4DC42A1a19a9653e994991e25b8153`.
The seller rejected `notify_funded` because the quote signature could not be
recovered from the on-chain job description.

The deployed Python SDK signed the canonical quote with the string-valued
`success_criteria` transformed into a list of its characters. The TypeScript
buyer correctly preserved the string in the on-chain description. The embedded
hash therefore cannot be reproduced by the SDK's normal verifier even though
the quote was signed by the configured seller.

## Goal

Allow the seller to verify this specific legacy SDK representation while
preserving every existing authorization boundary. The repair must allow job
`398` to continue without creating or funding another job.

## Selected Approach

Add a project-local compatibility verifier in `stockanalyst/app/agent/signing.py`.

1. Run the installed SDK's normal `recover_quote_signer` first.
2. Only when normal recovery fails, parse the structured description and require
   `terms.success_criteria` to equal the exact marker
   `uomp_notify_context_required_v1`.
3. Reconstruct the canonical content exactly as the legacy SDK signed it by
   replacing that marker string with its character list.
4. Require the recomputed hash to equal the embedded `negotiation_hash`.
5. Recover the EIP-191 signer from the embedded signature.
6. Continue requiring the recovered signer and the on-chain provider to equal
   the configured seller.

The fallback is project-local rather than a modification to installed
site-packages, so dependency reinstalls cannot silently remove it and its scope
is limited to this seller.

## Security Boundaries

The compatibility path does not bypass or weaken:

- `FUNDED` job status;
- on-chain provider equality;
- job expiry;
- structured description parsing;
- exact negotiation-hash equality;
- EIP-191 signature recovery;
- seller-address equality;
- funded budget versus signed price;
- buyer EIP-712 `notify_funded` authorization;
- chain ID and verifying-contract binding;
- delivery gateway validation.

Any modification to task, price, currency, chain, contract, terms, signature, or
provider still fails verification. The fallback is unavailable for arbitrary
success criteria.

## Testing

Tests are written before production code and must demonstrate:

- normal SDK-compatible descriptions still use the normal path;
- a legacy marker string signed using the character-list canonical form
  recovers the expected seller;
- a wrong signer is rejected;
- tampering with any other signed field is rejected;
- a different success criterion cannot enter the fallback;
- malformed descriptions and signatures remain rejected.

After the focused tests pass, run the full Python and buyer suites.

## Deployment and Recovery

Deploy the fix in place to the existing AWS AgentCore runtime with `bag deploy`
and `AWS_PROFILE=dev`. Do not create another runtime or use the managed
bnbagent-studio platform.

After the runtime is ready:

1. Recreate the original signed UOMP context with the same buyer wallet.
2. Retry only `notify_funded` for job `398`.
3. Poll job `398` until `SUBMITTED`.
4. Fetch the CloudFront manifest and verify its canonical Keccak commitment
   against the on-chain deliverable.
5. Save the report as HTML/PDF and verify CloudWatch, S3/CloudFront, and chain
   evidence refer to the same job.

No second job may be created as part of recovery.
