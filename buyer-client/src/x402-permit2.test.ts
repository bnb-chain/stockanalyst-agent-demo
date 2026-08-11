import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { TypedDataEncoder, Wallet, verifyTypedData } from "ethers";
import {
  BSC_MAINNET_CHAIN_ID,
  PAID_AMOUNT,
  PAYMENT_TOKENS,
  PERMIT2_ADDRESS,
  buildPaymentProof,
  type PaidPaymentChallenge,
  type PaymentTokenSymbol,
} from "./x402-payment.js";
import {
  PERMIT2_ALLOWANCE_TARGET,
  PERMIT2_PAYMENT_MINIMUM,
  approvePermit2Allowance,
  assertPermit2PaymentReady,
  buildPermit2PaymentProof,
  readPermit2Allowance,
  revokePermit2Allowance,
  runPermit2AllowanceCli,
  runPermit2AllowanceCliMain,
  type Permit2AllowanceContext,
  type Permit2CliDependencies,
  type Permit2TokenContract,
} from "./x402-permit2.js";

const NOW = 1_785_484_800;
const TTL_SECONDS = 600;
const PRIVATE_KEY = `0x${"31".repeat(32)}`;
const SELLER = "0xd10bddc20e4dc42a1a19a9653e994991e25b8153";
const SIGNER = "0x1111111111111111111111111111111111111111";
const SPENDER = "0x2222222222222222222222222222222222222222";

const PERMIT2_TYPES = {
  PermitWitnessTransferFrom: [
    { name: "permitted", type: "TokenPermissions" },
    { name: "spender", type: "address" },
    { name: "nonce", type: "uint256" },
    { name: "deadline", type: "uint256" },
    { name: "witness", type: "Witness" },
  ],
  TokenPermissions: [
    { name: "token", type: "address" },
    { name: "amount", type: "uint256" },
  ],
  Witness: [
    { name: "to", type: "address" },
    { name: "validAfter", type: "uint256" },
  ],
};

interface Permit2WireProof {
  x402Version: 2;
  resource: PaidPaymentChallenge["resource"];
  accepted: PaidPaymentChallenge["accepted"];
  payload: {
    signature: string;
    permit2Authorization: {
      permitted: { token: string; amount: string };
      from: string;
      spender: string;
      nonce: string;
      deadline: string;
      witness: { to: string; validAfter: string };
    };
    domain?: unknown;
    types?: unknown;
    primaryType?: unknown;
  };
}

function permit2Challenge(
  token: Extract<PaymentTokenSymbol, "USDC" | "USDT"> = "USDC",
): PaidPaymentChallenge {
  const metadata = PAYMENT_TOKENS[token];
  return {
    x402Version: 2,
    resource: {
      url: "https://agent.example/x402/analyze/async",
      description: "Stock analysis for AAPL",
      mimeType: "application/json",
    },
    accepted: {
      scheme: "exact",
      network: "eip155:56",
      amount: PAID_AMOUNT,
      asset: metadata.asset,
      payTo: SELLER,
      maxTimeoutSeconds: TTL_SECONDS,
      extra: {
        name: metadata.name,
        version: metadata.version,
        assetTransferMethod: "permit2-exact",
        signerAddress: SIGNER,
        spenderAddress: SPENDER,
      },
    },
    promotional: false,
  };
}

function decodeProof(proof: string): Permit2WireProof {
  return JSON.parse(Buffer.from(proof, "base64").toString("utf8")) as Permit2WireProof;
}

function canonicalDecimal(value: string): boolean {
  return value === "0" || /^[1-9][0-9]*$/.test(value);
}

test("buildPermit2PaymentProof emits the exact local Permit2 wire proof", async () => {
  const wallet = new Wallet(PRIVATE_KEY);
  const challenge = permit2Challenge("USDC");
  const encoded = await buildPermit2PaymentProof(
    wallet,
    challenge,
    TTL_SECONDS,
    () => NOW,
  );
  const proof = decodeProof(encoded);
  const authorization = proof.payload.permit2Authorization;

  assert.deepEqual(proof.resource, challenge.resource);
  assert.deepEqual(proof.accepted, challenge.accepted);
  assert.deepEqual(Object.keys(proof.payload), ["signature", "permit2Authorization"]);
  assert.deepEqual(Object.keys(authorization), [
    "permitted",
    "from",
    "spender",
    "nonce",
    "deadline",
    "witness",
  ]);
  assert.deepEqual(authorization.permitted, {
    token: PAYMENT_TOKENS.USDC.asset,
    amount: PAID_AMOUNT,
  });
  assert.equal(authorization.from, wallet.address.toLowerCase());
  assert.equal(authorization.spender, challenge.accepted.extra.spenderAddress);
  assert.equal(authorization.witness.to, challenge.accepted.payTo);
  assert.equal(authorization.witness.validAfter, String(NOW));
  assert.equal(authorization.deadline, String(NOW + TTL_SECONDS));
  assert.equal(proof.payload.domain, undefined);
  assert.equal(proof.payload.types, undefined);
  assert.equal(proof.payload.primaryType, undefined);

  for (const value of [
    authorization.permitted.amount,
    authorization.nonce,
    authorization.deadline,
    authorization.witness.validAfter,
  ]) {
    assert.equal(canonicalDecimal(value), true, value);
    assert.ok(BigInt(value) < (1n << 256n), value);
  }

  const domain = {
    name: "Permit2",
    chainId: BSC_MAINNET_CHAIN_ID,
    verifyingContract: PERMIT2_ADDRESS,
  };
  assert.equal("version" in domain, false);
  assert.equal(domain.chainId, 56);
  assert.equal(domain.verifyingContract, "0x000000000022D473030F116dDEE9F6B43aC78BA3");
  assert.equal(
    TypedDataEncoder.from(PERMIT2_TYPES).primaryType,
    "PermitWitnessTransferFrom",
  );
  assert.equal(
    verifyTypedData(
      domain,
      PERMIT2_TYPES,
      {
        permitted: {
          token: authorization.permitted.token,
          amount: BigInt(authorization.permitted.amount),
        },
        spender: authorization.spender,
        nonce: BigInt(authorization.nonce),
        deadline: BigInt(authorization.deadline),
        witness: {
          to: authorization.witness.to,
          validAfter: BigInt(authorization.witness.validAfter),
        },
      },
      proof.payload.signature,
    ),
    wallet.address,
  );
});

test("Permit2 proofs use fresh independent 256-bit nonce values", async () => {
  const wallet = new Wallet(PRIVATE_KEY);
  const challenge = permit2Challenge("USDT");
  const first = decodeProof(await buildPermit2PaymentProof(
    wallet,
    challenge,
    TTL_SECONDS,
    () => NOW,
  ));
  const second = decodeProof(await buildPermit2PaymentProof(
    wallet,
    challenge,
    TTL_SECONDS,
    () => NOW,
  ));
  const firstNonce = first.payload.permit2Authorization.nonce;
  const secondNonce = second.payload.permit2Authorization.nonce;

  assert.notEqual(firstNonce, secondNonce);
  assert.equal(canonicalDecimal(firstNonce), true);
  assert.equal(canonicalDecimal(secondNonce), true);
  assert.ok(BigInt(firstNonce) < (1n << 256n));
  assert.ok(BigInt(secondNonce) < (1n << 256n));
});

test("buildPermit2PaymentProof independently recovers the USDT signer", async () => {
  const wallet = new Wallet(PRIVATE_KEY);
  const challenge = permit2Challenge("USDT");
  const proof = decodeProof(await buildPermit2PaymentProof(
    wallet,
    challenge,
    TTL_SECONDS,
    () => NOW,
  ));
  const authorization = proof.payload.permit2Authorization;
  const domain = {
    name: "Permit2",
    chainId: 56,
    verifyingContract: PERMIT2_ADDRESS,
  };

  assert.equal(proof.accepted.asset, PAYMENT_TOKENS.USDT.asset);
  assert.equal(proof.accepted.extra.name, PAYMENT_TOKENS.USDT.name);
  assert.equal(proof.accepted.extra.version, PAYMENT_TOKENS.USDT.version);
  assert.equal(proof.accepted.extra.assetTransferMethod, "permit2-exact");
  assert.equal(authorization.permitted.token, PAYMENT_TOKENS.USDT.asset);
  assert.deepEqual(domain, {
    name: "Permit2",
    chainId: BSC_MAINNET_CHAIN_ID,
    verifyingContract: "0x000000000022D473030F116dDEE9F6B43aC78BA3",
  });
  assert.equal(
    verifyTypedData(
      domain,
      PERMIT2_TYPES,
      {
        permitted: {
          token: authorization.permitted.token,
          amount: BigInt(authorization.permitted.amount),
        },
        spender: authorization.spender,
        nonce: BigInt(authorization.nonce),
        deadline: BigInt(authorization.deadline),
        witness: {
          to: authorization.witness.to,
          validAfter: BigInt(authorization.witness.validAfter),
        },
      },
      proof.payload.signature,
    ),
    wallet.address,
  );
});

test("buildPaymentProof dispatches Permit2 without provider or typed-data metadata on wire", async () => {
  const wallet = new Wallet(PRIVATE_KEY);
  assert.equal(wallet.provider, null);
  const proof = decodeProof(await buildPaymentProof(
    wallet,
    permit2Challenge("USDT"),
    TTL_SECONDS,
  ));

  assert.equal("authorization" in proof.payload, false);
  assert.equal("domain" in proof.payload, false);
  assert.equal("types" in proof.payload, false);
  assert.equal("primaryType" in proof.payload, false);
  assert.equal(
    proof.payload.permit2Authorization.permitted.token,
    PAYMENT_TOKENS.USDT.asset,
  );

  const paymentModule = readFileSync(
    new URL("./x402-payment.js", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(paymentModule, /\.approve\s*\(/);
});

const WALLET = "0x3333333333333333333333333333333333333333";
const RPC_URL = "https://bsc.example.test";

interface FakeAllowanceRuntime {
  context: Permit2AllowanceContext;
  approvals: Array<{ spender: string; amount: bigint }>;
  prompts: string[];
  logs: string[];
}

function fakeAllowanceRuntime(options: {
  allowance?: unknown;
  chainId?: bigint | number;
  receiptStatuses?: Array<bigint | number | null>;
  answer?: string;
  yes?: boolean;
  token?: string;
} = {}): FakeAllowanceRuntime {
  const approvals: Array<{ spender: string; amount: bigint }> = [];
  const prompts: string[] = [];
  const logs: string[] = [];
  const statuses = [...(options.receiptStatuses ?? [1])];
  const contract: Permit2TokenContract = {
    allowance: async () => options.allowance ?? 0n,
    approve: async (spender, amount) => {
      approvals.push({ spender, amount });
      const status = statuses.shift() ?? 1;
      return { wait: async () => ({ status }) };
    },
  };
  return {
    approvals,
    prompts,
    logs,
    context: {
      token: options.token ?? "USDC",
      walletAddress: WALLET,
      rpcUrl: RPC_URL,
      provider: {
        getNetwork: async () => ({ chainId: options.chainId ?? 56n }),
      },
      contract,
      yes: options.yes ?? false,
      confirm: async (question) => {
        prompts.push(question);
        return options.answer ?? "yes";
      },
      log: (message) => logs.push(message),
    },
  };
}

test("Permit2 allowance policy uses exact 0.21 minimum and 50-token target", () => {
  assert.equal(PERMIT2_PAYMENT_MINIMUM, 210000000000000000n);
  assert.equal(PERMIT2_ALLOWANCE_TARGET, 50000000000000000000n);
  assert.throws(
    () => assertPermit2PaymentReady(PERMIT2_PAYMENT_MINIMUM - 1n),
    /below 0\.21/,
  );
  assert.doesNotThrow(() => assertPermit2PaymentReady(PERMIT2_PAYMENT_MINIMUM));
  assert.doesNotThrow(() => assertPermit2PaymentReady(49790000000000000000n));
  assert.doesNotThrow(() => assertPermit2PaymentReady(PERMIT2_ALLOWANCE_TARGET));
  assert.throws(
    () => assertPermit2PaymentReady(PERMIT2_ALLOWANCE_TARGET + 1n),
    /exceeds 50/,
  );
});

test("readPermit2Allowance checks chain 56 and canonical Permit2 spender", async () => {
  const runtime = fakeAllowanceRuntime({ allowance: PERMIT2_ALLOWANCE_TARGET });
  let actualOwner = "";
  let actualSpender = "";
  runtime.context.contract.allowance = async (owner, spender) => {
    actualOwner = owner;
    actualSpender = spender;
    return PERMIT2_ALLOWANCE_TARGET;
  };

  assert.equal(await readPermit2Allowance(runtime.context), PERMIT2_ALLOWANCE_TARGET);
  assert.equal(actualOwner, WALLET);
  assert.equal(actualSpender, PERMIT2_ADDRESS);
});

test("readPermit2Allowance rejects non-BSC networks, invalid RPC URLs, and non-bigint reads", async () => {
  await assert.rejects(
    readPermit2Allowance(fakeAllowanceRuntime({ chainId: 97n }).context),
    /chain ID 56/,
  );
  const badUrl = fakeAllowanceRuntime().context;
  badUrl.rpcUrl = "not a URL";
  await assert.rejects(readPermit2Allowance(badUrl), /BSC_RPC_URL/);
  await assert.rejects(
    readPermit2Allowance(fakeAllowanceRuntime({ allowance: "50000000000000000000" }).context),
    /bigint/,
  );
});

test("allowance commands reject U and USD1", async () => {
  for (const token of ["U", "USD1"]) {
    await assert.rejects(
      readPermit2Allowance(fakeAllowanceRuntime({ token }).context),
      /exactly USDC or USDT/,
    );
  }
});

test("approve sends one exact 50-token approval from zero", async () => {
  const runtime = fakeAllowanceRuntime({ allowance: 0n, yes: true });

  await approvePermit2Allowance(runtime.context);

  assert.deepEqual(runtime.approvals, [{
    spender: PERMIT2_ADDRESS,
    amount: PERMIT2_ALLOWANCE_TARGET,
  }]);
  assert.match(runtime.logs.join("\n"), /Transaction count: 1/);
});

test("approve resets non-zero 49.79 allowance to zero before exact 50", async () => {
  const runtime = fakeAllowanceRuntime({
    allowance: 49790000000000000000n,
    yes: true,
    receiptStatuses: [1n, 1n],
  });

  await approvePermit2Allowance(runtime.context);

  assert.deepEqual(runtime.approvals, [
    { spender: PERMIT2_ADDRESS, amount: 0n },
    { spender: PERMIT2_ADDRESS, amount: PERMIT2_ALLOWANCE_TARGET },
  ]);
  assert.match(runtime.logs.join("\n"), /Transaction count: 2/);
});

test("approve resets an above-50 allowance instead of extending it", async () => {
  const runtime = fakeAllowanceRuntime({
    allowance: PERMIT2_ALLOWANCE_TARGET + 1n,
    yes: true,
  });

  await approvePermit2Allowance(runtime.context);

  assert.deepEqual(runtime.approvals.map(({ amount }) => amount), [
    0n,
    PERMIT2_ALLOWANCE_TARGET,
  ]);
});

test("approve is a no-op when allowance is exactly 50", async () => {
  const runtime = fakeAllowanceRuntime({ allowance: PERMIT2_ALLOWANCE_TARGET });

  await approvePermit2Allowance(runtime.context);

  assert.deepEqual(runtime.approvals, []);
  assert.deepEqual(runtime.prompts, []);
  assert.match(runtime.logs.join("\n"), /Transaction count: 0/);
});

test("approve refuses a failed transaction receipt and does not continue", async () => {
  const runtime = fakeAllowanceRuntime({
    allowance: 1n,
    yes: true,
    receiptStatuses: [0],
  });

  await assert.rejects(approvePermit2Allowance(runtime.context), /receipt status 1/);
  assert.deepEqual(runtime.approvals.map(({ amount }) => amount), [0n]);
});

test("approve requires the exact interactive answer yes", async () => {
  for (const answer of ["y", "YES", " yes ", "no"]) {
    const runtime = fakeAllowanceRuntime({ allowance: 0n, answer });
    await assert.rejects(approvePermit2Allowance(runtime.context), /declined/);
    assert.deepEqual(runtime.approvals, []);
  }
});

test("approve --yes bypasses the interactive prompt", async () => {
  const runtime = fakeAllowanceRuntime({ allowance: 0n, answer: "no", yes: true });

  await approvePermit2Allowance(runtime.context);

  assert.deepEqual(runtime.prompts, []);
  assert.equal(runtime.approvals.length, 1);
});

test("revoke prints the complete zero-target summary and sends only zero", async () => {
  const runtime = fakeAllowanceRuntime({ allowance: PERMIT2_ALLOWANCE_TARGET, yes: true });

  await revokePermit2Allowance(runtime.context);

  assert.deepEqual(runtime.approvals, [{ spender: PERMIT2_ADDRESS, amount: 0n }]);
  assert.deepEqual(runtime.logs, [
    "Chain: 56",
    `Wallet: ${WALLET}`,
    "Token: USDC",
    `Token contract: ${PAYMENT_TOKENS.USDC.asset}`,
    `Canonical Permit2: ${PERMIT2_ADDRESS}`,
    `Current allowance: ${PERMIT2_ALLOWANCE_TARGET}`,
    "Target allowance: 0",
    "Transaction count: 1",
  ]);
});

test("CLI dispatches allowance, approve --yes, and revoke with injected mocks", async () => {
  const actions: string[] = [];
  const dependencies: Permit2CliDependencies = {
    createContext: async ({ token, yes }) => {
      actions.push(`${token}:${yes}`);
      return fakeAllowanceRuntime({ token, yes, allowance: 0n }).context;
    },
  };

  await runPermit2AllowanceCli(["allowance", "USDC"], {}, dependencies);
  await runPermit2AllowanceCli(["approve", "USDT", "--yes"], {}, dependencies);
  await runPermit2AllowanceCli(["revoke", "USDC", "--yes"], {}, dependencies);

  assert.deepEqual(actions, ["USDC:false", "USDT:true", "USDC:true"]);
});

test("CLI main redacts unknown provider and transaction errors and exits nonzero", async () => {
  const maliciousProviderError = new Error([
    "SERVER_ERROR requestUrl=https://buyer:API_KEY_MARKER@bsc.example/v1",
    '{"body":{"nested":"NESTED_JSON_MARKER"}}',
  ].join("\n"));
  const maliciousTransactionError = {
    message: "eth_sendRawTransaction RAW_TX_MARKER 0x02f8deadbeef",
    error: { body: "SIGNATURE_MARKER", nested: { apiKey: "API_KEY_MARKER" } },
    toString: () => "NESTED_JSON_MARKER",
  };
  const cases: Array<{
    args: string[];
    configure(runtime: FakeAllowanceRuntime): void;
  }> = [
    {
      args: ["allowance", "USDC"],
      configure: (runtime) => {
        runtime.context.provider.getNetwork = async () => {
          throw maliciousProviderError;
        };
      },
    },
    {
      args: ["approve", "USDC", "--yes"],
      configure: (runtime) => {
        runtime.context.contract.approve = async () => {
          throw maliciousTransactionError;
        };
      },
    },
  ];

  for (const testCase of cases) {
    const runtime = fakeAllowanceRuntime({ allowance: 0n, yes: true });
    testCase.configure(runtime);
    const stderr: string[] = [];
    const exitCodes: number[] = [];
    const dependencies: Permit2CliDependencies = {
      createContext: async () => runtime.context,
    };

    await runPermit2AllowanceCliMain(
      testCase.args,
      {},
      dependencies,
      {
        writeError: (message) => stderr.push(message),
        setExitCode: (code) => exitCodes.push(code),
      },
    );

    assert.deepEqual(stderr, [
      "Permit2 allowance command failed; verify RPC configuration, wallet funds, and transaction status",
    ]);
    assert.deepEqual(exitCodes, [1]);
    assert.doesNotMatch(
      `${runtime.logs.join("\n")}\n${stderr.join("\n")}`,
      /API_KEY_MARKER|RAW_TX_MARKER|SIGNATURE_MARKER|NESTED_JSON_MARKER|requestUrl|eth_sendRawTransaction/,
    );
  }
});

test("CLI formatter reads an allowlisted Error message exactly once", async () => {
  const statefulError = new Error();
  let messageReads = 0;
  Object.defineProperty(statefulError, "message", {
    configurable: true,
    get: () => {
      messageReads += 1;
      if (messageReads === 1) {
        return "BSC_RPC_URL is required for Permit2 allowance operations";
      }
      return [
        "https://buyer:API_KEY_MARKER@bsc.example/v1",
        "eth_sendRawTransaction RAW_TX_MARKER",
        "SIGNATURE_MARKER",
      ].join("\n");
    },
  });
  const stderr: string[] = [];
  const exitCodes: number[] = [];

  await runPermit2AllowanceCliMain(
    ["allowance", "USDC"],
    {},
    { createContext: async () => { throw statefulError; } },
    {
      writeError: (message) => stderr.push(message),
      setExitCode: (code) => exitCodes.push(code),
    },
  );

  assert.equal(messageReads, 1);
  assert.deepEqual(stderr, [
    "BSC_RPC_URL is required for Permit2 allowance operations",
  ]);
  assert.deepEqual(exitCodes, [1]);
  assert.doesNotMatch(
    stderr.join("\n"),
    /API_KEY_MARKER|RAW_TX_MARKER|SIGNATURE_MARKER|eth_sendRawTransaction/,
  );
});

test("CLI formatter safely handles an Error message getter that throws", async () => {
  const throwingError = new Error();
  Object.defineProperty(throwingError, "message", {
    configurable: true,
    get: () => {
      throw new Error("API_KEY_MARKER RAW_TX_MARKER SIGNATURE_MARKER");
    },
  });
  const stderr: string[] = [];
  const exitCodes: number[] = [];

  await runPermit2AllowanceCliMain(
    ["allowance", "USDC"],
    {},
    { createContext: async () => { throw throwingError; } },
    {
      writeError: (message) => stderr.push(message),
      setExitCode: (code) => exitCodes.push(code),
    },
  );

  assert.deepEqual(stderr, [
    "Permit2 allowance command failed; verify RPC configuration, wallet funds, and transaction status",
  ]);
  assert.deepEqual(exitCodes, [1]);
  assert.doesNotMatch(stderr.join("\n"), /API_KEY_MARKER|RAW_TX_MARKER|SIGNATURE_MARKER/);
});
