import assert from "node:assert/strict";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { EventEmitter } from "node:events";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Wallet, verifyTypedData } from "ethers";
import {
  AsyncJobClientError,
  createAsyncAnalysis,
  downloadAsyncReport,
  fetchPaymentChallenge,
  pollAsyncAnalysis,
  resumeAsyncAnalysis,
  type AsyncJobReceipt,
  type AsyncJobStatus,
  type FetchImpl,
} from "./x402-async-client.js";
import {
  BSC_TESTNET_CHAIN_ID,
  U_TOKEN_ADDRESS,
  U_TOKEN_DOMAIN_NAME,
  U_TOKEN_DOMAIN_VERSION,
  buildPaymentProof,
  resolveX402SellerWallet,
  type PaidPaymentChallenge,
} from "./x402-payment.js";
import {
  acquireExclusiveCliLock,
  bindPendingToJob,
  cleanupPrivateStateTemps,
  createPendingRecord,
  createReceiptFromPending,
  finalizeAsyncReport,
  installGracefulLockCleanup,
  loadAsyncJobReceipt,
  loadPendingCreate,
  pendingCreateFailureMessage,
  pollAsyncAnalysisWithPendingRecovery,
  quarantineRemoveOwnedPending,
  persistAsyncJobReceipt,
  persistPendingCreate,
} from "./x402-async.js";

const JOB_ID = `x402_${"a".repeat(32)}`;
const JOB_TOKEN = "secret-token";
const ENDPOINT = "https://agent.example";
const EXPIRES_AT = 1_800_000_000_000;
const REPORT_URL = "https://reports-bucket.s3.us-east-1.amazonaws.com/report.md?X-Amz-Signature=test";
const OLD_REPORT_URL = "https://reports-bucket.s3.us-east-1.amazonaws.com/old.md?X-Amz-Signature=old";
const NEW_REPORT_URL = "https://reports-bucket.s3.us-east-1.amazonaws.com/new.md?X-Amz-Signature=new";
const SELLER = "0xd10BdDC20E4DC42A1a19a9653e994991e25b8153";
const SIGNER = "0x1111111111111111111111111111111111111111";
const B402_U_TOKEN = "0x330949Aed7d00FCe0558C64ED6FeC9792616cC39";
const ONE_U_ATOMIC = "1000000";

function paymentChallenge(
  overrides: {
    resource?: Record<string, unknown>;
    accepted?: Record<string, unknown>;
    extra?: Record<string, unknown>;
  } = {},
): PaidPaymentChallenge {
  const resource = {
    url: `${ENDPOINT}/x402/analyze/async`,
    description: "Stock analysis for AAPL",
    mimeType: "application/json",
    ...overrides.resource,
  };
  const extra = {
    name: "U",
    version: "1",
    assetTransferMethod: "eip3009",
    signerAddress: SIGNER,
    ...overrides.extra,
  };
  const accepted = {
    scheme: "exact",
    network: "eip155:97",
    amount: ONE_U_ATOMIC,
    asset: U_TOKEN_ADDRESS,
    payTo: SELLER.toLowerCase(),
    maxTimeoutSeconds: 600,
    extra,
    ...overrides.accepted,
  };
  return {
    x402Version: 2,
    resource,
    accepted,
  } as PaidPaymentChallenge;
}

function receipt(overrides: Partial<AsyncJobReceipt> = {}): AsyncJobReceipt {
  return {
    jobId: JOB_ID,
    jobToken: JOB_TOKEN,
    status: "queued",
    statusUrl: `/x402/jobs/${JOB_ID}`,
    expiresAt: EXPIRES_AT,
    ...overrides,
  };
}

function status(
  jobStatus: AsyncJobStatus["status"],
  overrides: Partial<AsyncJobStatus> = {},
): AsyncJobStatus {
  return {
    jobId: JOB_ID,
    status: jobStatus,
    expiresAt: EXPIRES_AT,
    ...overrides,
  };
}

function json(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...Object.fromEntries(new Headers(init.headers)),
    },
  });
}

function fakeClock(start = 1_000): {
  now: () => number;
  sleep: (milliseconds: number) => Promise<void>;
  waits: number[];
} {
  let current = start;
  const waits: number[] = [];
  return {
    now: () => current,
    sleep: async (milliseconds) => {
      waits.push(milliseconds);
      current += milliseconds;
    },
    waits,
  };
}

function proofExpiringAt(validBeforeSeconds: number): string {
  return Buffer.from(JSON.stringify({
    x402Version: 2,
    payload: {
      authorization: {
        validBefore: String(validBeforeSeconds),
      },
    },
  })).toString("base64");
}

function decodeProof(proof: string): {
  payload: { authorization: { to: string } };
} {
  return JSON.parse(Buffer.from(proof, "base64").toString("utf8")) as {
    payload: { authorization: { to: string } };
  };
}

test("requires an explicit x402 seller wallet", () => {
  assert.throws(
    () => resolveX402SellerWallet({}),
    /X402_SELLER_WALLET/,
  );
});

test("uses the B402-supported six-decimal U token", () => {
  assert.equal(U_TOKEN_ADDRESS, B402_U_TOKEN);
  assert.equal(paymentChallenge().accepted.amount, ONE_U_ATOMIC);
});

test("signs the authorization to the configured seller wallet", async () => {
  const proof = decodeProof(
    await buildPaymentProof(
      new Wallet(Wallet.createRandom().privateKey),
      paymentChallenge(),
    ),
  );

  assert.equal(proof.payload.authorization.to, SELLER.toLowerCase());
});

test("buildPaymentProof creates the official B402 V2 proof", async () => {
  const wallet = new Wallet(Wallet.createRandom().privateKey);
  const challenge = paymentChallenge({
    accepted: { amount: "42" },
  });
  const proof = JSON.parse(
    Buffer.from(await buildPaymentProof(wallet, challenge, 300), "base64").toString("utf8"),
  ) as {
    x402Version: number;
    resource: PaidPaymentChallenge["resource"];
    accepted: PaidPaymentChallenge["accepted"];
    payload: {
      signature: string;
      authorization: {
        from: string;
        to: string;
        value: string;
        validAfter: string;
        validBefore: string;
        nonce: string;
      };
    };
  };

  assert.equal(proof.x402Version, 2);
  assert.deepEqual(proof.resource, challenge.resource);
  assert.deepEqual(proof.accepted, challenge.accepted);
  assert.equal("scheme" in proof, false);
  assert.equal("network" in proof, false);
  assert.equal(proof.payload.authorization.from, wallet.address.toLowerCase());
  assert.equal(proof.payload.authorization.to, SELLER.toLowerCase());
  assert.equal(proof.payload.authorization.value, "42");
  assert.equal(proof.payload.authorization.validAfter, "0");
  assert.match(proof.payload.authorization.nonce, /^0x[0-9a-f]{64}$/);

  const recovered = verifyTypedData(
    {
      name: U_TOKEN_DOMAIN_NAME,
      version: U_TOKEN_DOMAIN_VERSION,
      chainId: BSC_TESTNET_CHAIN_ID,
      verifyingContract: U_TOKEN_ADDRESS,
    },
    {
      TransferWithAuthorization: [
        { name: "from", type: "address" },
        { name: "to", type: "address" },
        { name: "value", type: "uint256" },
        { name: "validAfter", type: "uint256" },
        { name: "validBefore", type: "uint256" },
        { name: "nonce", type: "bytes32" },
      ],
    },
    {
      ...proof.payload.authorization,
      value: BigInt(proof.payload.authorization.value),
      validAfter: BigInt(proof.payload.authorization.validAfter),
      validBefore: BigInt(proof.payload.authorization.validBefore),
    },
    proof.payload.signature,
  );
  assert.equal(recovered, wallet.address);
});

test("fetchPaymentChallenge validates the B402 payment challenge", async () => {
  const expected = paymentChallenge();
  const calls: Array<{ url: string; headers: Headers }> = [];
  const fetchImpl: FetchImpl = async (input, init) => {
    calls.push({
      url: String(input),
      headers: new Headers(init?.headers),
    });
    return json({
      error: "Payment Required",
      paymentRequired: {
        x402Version: 2,
        accepts: [expected.accepted],
        resource: expected.resource,
      },
    }, { status: 402 });
  };

  const actual = await fetchPaymentChallenge(
    ENDPOINT,
    { symbols: ["AAPL"] },
    SELLER,
    fetchImpl,
  );

  assert.deepEqual(actual, expected);
  assert.equal(calls.length, 1);
  assert.equal(calls[0]?.url, `${ENDPOINT}/x402/analyze/async`);
  assert.equal(calls[0]?.headers.has("X-Payment"), false);
});

test("fetchPaymentChallenge preserves an API Gateway stage prefix", async () => {
  const endpoint = `${ENDPOINT}/testnet`;
  const expected = paymentChallenge({
    resource: {
      url: `${endpoint}/x402/analyze/async`,
    },
  });
  let requestedUrl = "";
  const actual = await fetchPaymentChallenge(
    endpoint,
    { symbols: ["AAPL"] },
    SELLER,
    async (input) => {
      requestedUrl = String(input);
      return json({
        paymentRequired: {
          x402Version: 2,
          accepts: [expected.accepted],
          resource: expected.resource,
        },
      }, { status: 402 });
    },
  );

  assert.deepEqual(actual, expected);
  assert.equal(requestedUrl, `${endpoint}/x402/analyze/async`);
});

test("fetchPaymentChallenge rejects unsafe B402 payment challenges", async () => {
  const invalid: Array<[string, PaidPaymentChallenge]> = [
    ["network", paymentChallenge({ accepted: { network: "eip155:56" } })],
    ["asset", paymentChallenge({ accepted: { asset: "0x" + "22".repeat(20) } })],
    ["seller", paymentChallenge({ accepted: { payTo: "0x" + "33".repeat(20) } })],
    ["amount", paymentChallenge({ accepted: { amount: "999" } })],
    ["method", paymentChallenge({ extra: { assetTransferMethod: "permit2-exact" } })],
    ["signer", paymentChallenge({ extra: { signerAddress: "invalid" } })],
    ["resource", paymentChallenge({ resource: { url: "https://evil.example/x402" } })],
  ];

  for (const [field, challenge] of invalid) {
    await assert.rejects(
      fetchPaymentChallenge(
        ENDPOINT,
        { symbols: ["AAPL"] },
        SELLER,
        async () => json({
          paymentRequired: {
            x402Version: 2,
            accepts: [challenge.accepted],
            resource: challenge.resource,
          },
        }, { status: 402 }),
      ),
      (error: unknown) => {
        assert.equal((error as AsyncJobClientError).code, "invalid_payment_challenge", field);
        return true;
      },
    );
  }
});

test("creates, polls, and downloads an asynchronous report", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  let jobReads = 0;
  const fetchImpl: FetchImpl = async (input, init) => {
    const url = String(input);
    calls.push({ url, init });
    if (url.endsWith("/x402/analyze/async")) {
      return json(receipt(), { status: 202 });
    }
    if (url.endsWith(`/x402/jobs/${JOB_ID}`)) {
      jobReads += 1;
      if (jobReads === 1) {
        return json(status("running"), {
          headers: { "Retry-After": "0" },
        });
      }
      return json(status("succeeded", {
        downloadUrl: REPORT_URL,
        downloadUrlExpiresAt: EXPIRES_AT - 1,
      }));
    }
    if (url === REPORT_URL) {
      return new Response("# report");
    }
    throw new Error(`unexpected URL: ${url}`);
  };
  const clock = fakeClock();

  const created = await createAsyncAnalysis(
    ENDPOINT,
    "proof",
    { symbols: ["AAPL"] },
    fetchImpl,
  );
  const complete = await pollAsyncAnalysis(
    ENDPOINT,
    created,
    fetchImpl,
    { ...clock, timeoutMs: 5_000 },
  );
  const report = await downloadAsyncReport(
    ENDPOINT,
    created,
    complete,
    fetchImpl,
    { now: clock.now },
  );

  assert.equal(complete.status, "succeeded");
  assert.equal(report, "# report");
  assert.deepEqual(clock.waits, [1_000]);
  for (const call of calls.filter(({ url }) => url.includes(`/x402/jobs/${JOB_ID}`))) {
    assert.equal(new Headers(call.init?.headers).get("X-Job-Token"), JOB_TOKEN);
  }
  assert.equal(
    new Headers(calls.at(-1)?.init?.headers).has("X-Job-Token"),
    false,
    "the job token must not be sent to the presigned S3 origin",
  );
});

test("create validates every receipt field and requires HTTP 202", async () => {
  const valid = receipt();
  for (const [field, value] of [
    ["jobId", "not-a-job"],
    ["jobToken", ""],
    ["status", 4],
    ["statusUrl", "https://evil.example/job"],
    ["expiresAt", "later"],
  ] as const) {
    const fetchImpl: FetchImpl = async () => json({ ...valid, [field]: value }, { status: 202 });
    await assert.rejects(
      createAsyncAnalysis(ENDPOINT, "proof", { symbols: ["AAPL"] }, fetchImpl),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "invalid_response",
      field,
    );
  }
  for (const statusUrl of [
    `${ENDPOINT}/x402/jobs/${JOB_ID}?token=leak`,
    `${ENDPOINT}/x402/jobs/${JOB_ID}#fragment`,
    `/x402/jobs/${JOB_ID}/`,
    `//evil.example/x402/jobs/${JOB_ID}`,
  ]) {
    await assert.rejects(
      createAsyncAnalysis(
        ENDPOINT,
        "proof",
        { symbols: ["AAPL"] },
        async () => json({ ...valid, statusUrl }, { status: 202 }),
      ),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "invalid_response",
      statusUrl,
    );
  }

  await assert.rejects(
    createAsyncAnalysis(
      ENDPOINT,
      "proof",
      { symbols: ["AAPL"] },
      async () => json({ errorCode: "payment_rejected" }, { status: 402 }),
    ),
    (error: unknown) => error instanceof AsyncJobClientError
      && error.code === "payment_rejected"
      && error.httpStatus === 402,
  );
});

test("canonicalizes an absolute same-origin status URL before persistence and restart", async () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-status-url-"));
  const receiptPath = join(directory, "receipt.json");
  try {
    const created = await createAsyncAnalysis(
      ENDPOINT,
      "proof",
      { symbols: ["AAPL"] },
      async () => json(receipt({
        statusUrl: `${ENDPOINT}/x402/jobs/${JOB_ID}`,
      }), { status: 202 }),
    );

    assert.equal(created.statusUrl, `/x402/jobs/${JOB_ID}`);
    persistAsyncJobReceipt(receiptPath, created);
    assert.equal(
      JSON.parse(readFileSync(receiptPath, "utf8")).statusUrl,
      `/x402/jobs/${JOB_ID}`,
    );
    assert.equal(
      loadAsyncJobReceipt(receiptPath).statusUrl,
      `/x402/jobs/${JOB_ID}`,
    );
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("stored receipts reject status path, query, fragment, and origin variants", () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-status-url-invalid-"));
  const receiptPath = join(directory, "receipt.json");
  try {
    for (const statusUrl of [
      `/x402/jobs/${JOB_ID}/`,
      `/x402/jobs/${JOB_ID}?token=leak`,
      `/x402/jobs/${JOB_ID}#fragment`,
      `${ENDPOINT}/x402/jobs/${JOB_ID}`,
      `https://evil.example/x402/jobs/${JOB_ID}`,
    ]) {
      writeFileSync(receiptPath, JSON.stringify({
        jobId: JOB_ID,
        jobToken: JOB_TOKEN,
        statusUrl,
        expiresAt: EXPIRES_AT,
      }), { mode: 0o600 });
      assert.throws(
        () => loadAsyncJobReceipt(receiptPath),
        /Stored x402 receipt is invalid/,
        statusUrl,
      );
    }
    assert.throws(
      () => persistAsyncJobReceipt(receiptPath, receipt({
        statusUrl: `${ENDPOINT}/x402/jobs/${JOB_ID}`,
      })),
      /Stored x402 receipt is invalid/,
    );
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("authenticated calls reject same-origin and cross-origin redirects without following", async () => {
  for (const location of [
    `/x402/jobs/${JOB_ID}`,
    "https://evil.example/collect",
  ]) {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const redirected: FetchImpl = async (input, init) => {
      calls.push({ url: String(input), init });
      return new Response(null, {
        status: 302,
        headers: { Location: location },
      });
    };

    await assert.rejects(
      createAsyncAnalysis(
        ENDPOINT,
        "private-proof",
        { symbols: ["AAPL"] },
        redirected,
      ),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "http_302",
    );
    assert.equal(calls.length, 1);
    assert.equal(calls[0]?.init?.redirect, "error");

    calls.length = 0;
    await assert.rejects(
      pollAsyncAnalysis(
        ENDPOINT,
        receipt(),
        redirected,
        { ...fakeClock(), timeoutMs: 5_000 },
      ),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "http_302",
    );
    assert.equal(calls.length, 1);
    assert.equal(calls[0]?.init?.redirect, "error");
    assert.equal(
      new Headers(calls[0]?.init?.headers).get("X-Job-Token"),
      JOB_TOKEN,
    );

    calls.length = 0;
    await assert.rejects(
      resumeAsyncAnalysis(ENDPOINT, receipt(), redirected),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "http_302",
    );
    assert.equal(calls.length, 1);
    assert.equal(calls[0]?.init?.redirect, "error");
  }
});

test("unsafe server error codes are replaced with stable generic codes", async () => {
  const secret = "token-from-untrusted-body";
  await assert.rejects(
    createAsyncAnalysis(
      ENDPOINT,
      "proof",
      { symbols: ["AAPL"] },
      async () => json({
        errorCode: `bad\r\n${secret}`,
        retryable: true,
      }, { status: 503 }),
    ),
    (error: unknown) => error instanceof AsyncJobClientError
      && error.code === "http_503"
      && !error.message.includes(secret)
      && !/[\r\n]/.test(error.message),
  );

  await assert.rejects(
    pollAsyncAnalysis(
      ENDPOINT,
      receipt(),
      async () => json(status("failed", {
        errorCode: `bad\r\n${secret}`,
        retryable: false,
      })),
      { ...fakeClock(), timeoutMs: 5_000 },
    ),
    (error: unknown) => error instanceof AsyncJobClientError
      && error.code === "analysis_failed"
      && !error.message.includes(secret),
  );
});

test("network errors never expose payment, job-token, or presigned URL secrets", async () => {
  const proof = "private-payment-proof";
  await assert.rejects(
    createAsyncAnalysis(
      ENDPOINT,
      proof,
      { symbols: ["AAPL"] },
      async () => {
        throw new Error(`transport included ${proof}`);
      },
    ),
    (error: unknown) => error instanceof AsyncJobClientError
      && error.code === "network_error"
      && error.retryable
      && !error.message.includes(proof),
  );

  const privateUrl = "https://reports-bucket.s3.us-east-1.amazonaws.com/report.md?X-Amz-Signature=private";
  await assert.rejects(
    downloadAsyncReport(
      ENDPOINT,
      receipt(),
      status("succeeded", {
        downloadUrl: privateUrl,
        downloadUrlExpiresAt: EXPIRES_AT,
      }),
      async () => {
        throw new Error(`transport included ${privateUrl} and ${JOB_TOKEN}`);
      },
    ),
    (error: unknown) => error instanceof AsyncJobClientError
      && error.code === "download_network_error"
      && !error.message.includes(privateUrl)
      && !error.message.includes(JOB_TOKEN),
  );
});

test("poll retries a network interruption and keeps authenticating job reads", async () => {
  let calls = 0;
  const headers: Headers[] = [];
  const fetchImpl: FetchImpl = async (_input, init) => {
    calls += 1;
    headers.push(new Headers(init?.headers));
    if (calls === 1) throw new TypeError("socket reset");
    return json(status("succeeded", {
      downloadUrl: REPORT_URL,
      downloadUrlExpiresAt: EXPIRES_AT,
    }));
  };
  const clock = fakeClock();

  const complete = await pollAsyncAnalysis(
    ENDPOINT,
    receipt(),
    fetchImpl,
    { ...clock, timeoutMs: 5_000, defaultPollMilliseconds: 20 },
  );

  assert.equal(complete.status, "succeeded");
  assert.equal(calls, 2);
  assert.deepEqual(clock.waits, [1_000]);
  assert.ok(headers.every((value) => value.get("X-Job-Token") === JOB_TOKEN));
});

test("authoritative non-settling GET invokes pending cleanup exactly once", async () => {
  const clock = fakeClock();
  const states = ["queued", "running", "succeeded"] as const;
  let reads = 0;
  let cleanups = 0;
  const completed = await pollAsyncAnalysis(
    ENDPOINT,
    receipt({ status: "settling" }),
    async () => {
      const current = states[Math.min(reads, states.length - 1)]!;
      reads += 1;
      return json(
        current === "succeeded"
          ? status(current, {
            downloadUrl: REPORT_URL,
            downloadUrlExpiresAt: EXPIRES_AT - 1,
          })
          : status(current),
        { headers: { "Retry-After": "1" } },
      );
    },
    {
      ...clock,
      timeoutMs: 5_000,
      onAuthoritativeNonSettling: async () => {
        cleanups += 1;
      },
    },
  );
  assert.equal(completed.status, "succeeded");
  assert.equal(cleanups, 1);
});

test("failed and 410 job states each invoke pending cleanup before throwing", async () => {
  for (const response of [
    json(status("failed", {
      errorCode: "analysis_failed",
      retryable: false,
    })),
    json({ errorCode: "job_expired" }, { status: 410 }),
  ]) {
    let cleanups = 0;
    await assert.rejects(
      pollAsyncAnalysis(
        ENDPOINT,
        receipt({ status: "settling" }),
        async () => response,
        {
          ...fakeClock(),
          timeoutMs: 5_000,
          onAuthoritativeNonSettling: async () => {
            cleanups += 1;
          },
        },
      ),
      AsyncJobClientError,
    );
    assert.equal(cleanups, 1);
  }
});

test("network ambiguity and continued settling never invoke pending cleanup", async () => {
  for (const fetchImpl of [
    async () => {
      throw new TypeError("network unavailable");
    },
    async () => json(status("settling"), {
      headers: { "Retry-After": "1" },
    }),
  ] satisfies FetchImpl[]) {
    let cleanups = 0;
    await assert.rejects(
      pollAsyncAnalysis(
        ENDPOINT,
        receipt({ status: "settling" }),
        fetchImpl,
        {
          ...fakeClock(),
          timeoutMs: 500,
          defaultPollMilliseconds: 1_000,
          onAuthoritativeNonSettling: async () => {
            cleanups += 1;
          },
        },
      ),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "poll_timeout",
    );
    assert.equal(cleanups, 0);
  }
});

test("poll resumes a retryable failure at most once", async () => {
  let reads = 0;
  let resumes = 0;
  const fetchImpl: FetchImpl = async (input, init) => {
    const url = String(input);
    assert.equal(new Headers(init?.headers).get("X-Job-Token"), JOB_TOKEN);
    if (url.endsWith("/resume")) {
      resumes += 1;
      return json(status("queued"), {
        status: 202,
        headers: { "Retry-After": "0" },
      });
    }
    reads += 1;
    if (reads === 1) {
      return json(status("failed", {
        errorCode: "analysis_timeout",
        retryable: true,
      }));
    }
    return json(status("succeeded", {
      downloadUrl: REPORT_URL,
      downloadUrlExpiresAt: EXPIRES_AT,
    }));
  };
  const clock = fakeClock();

  const complete = await pollAsyncAnalysis(
    ENDPOINT,
    receipt(),
    fetchImpl,
    { ...clock, timeoutMs: 5_000 },
  );

  assert.equal(complete.status, "succeeded");
  assert.equal(resumes, 1);
  assert.deepEqual(clock.waits, [1_000]);
});

test("poll treats a resume conflict as a concurrent recovery and continues", async () => {
  let reads = 0;
  let resumes = 0;
  const fetchImpl: FetchImpl = async (input) => {
    const url = String(input);
    if (url.endsWith("/resume")) {
      resumes += 1;
      return json({ errorCode: "job_conflict" }, { status: 409 });
    }
    reads += 1;
    if (reads === 1) {
      return json(status("failed", {
        errorCode: "analysis_timeout",
        retryable: true,
      }));
    }
    return json(status("succeeded", {
      downloadUrl: REPORT_URL,
      downloadUrlExpiresAt: EXPIRES_AT,
    }));
  };
  const clock = fakeClock();

  const complete = await pollAsyncAnalysis(
    ENDPOINT,
    receipt(),
    fetchImpl,
    { ...clock, timeoutMs: 5_000 },
  );

  assert.equal(complete.status, "succeeded");
  assert.equal(resumes, 1);
});

test("poll periodically resumes a running job until a stale owner is recovered", async () => {
  let reads = 0;
  let resumes = 0;
  const fetchImpl: FetchImpl = async (input) => {
    const url = String(input);
    if (url.endsWith("/resume")) {
      resumes += 1;
      if (resumes === 1) {
        return json({ errorCode: "job_conflict" }, { status: 409 });
      }
      return json(status("queued"), {
        status: 202,
        headers: { "Retry-After": "1" },
      });
    }
    reads += 1;
    if (reads <= 5) {
      return json(status("running"), {
        headers: { "Retry-After": "1" },
      });
    }
    return json(status("succeeded", {
      downloadUrl: REPORT_URL,
      downloadUrlExpiresAt: EXPIRES_AT,
    }));
  };
  const clock = fakeClock();

  const complete = await pollAsyncAnalysis(
    ENDPOINT,
    receipt(),
    fetchImpl,
    {
      ...clock,
      timeoutMs: 10_000,
      runningRecoveryMilliseconds: 2_000,
    },
  );

  assert.equal(complete.status, "succeeded");
  assert.equal(resumes, 2);
  assert.equal(reads, 6);
  assert.deepEqual(clock.waits, [1_000, 1_000, 1_000, 1_000, 1_000]);
});

for (const failure of ["network", "503"] as const) {
  test(`poll continues GET polling after an ambiguous resume ${failure} failure`, async () => {
    let reads = 0;
    let resumes = 0;
    const fetchImpl: FetchImpl = async (input) => {
      const url = String(input);
      if (url.endsWith("/resume")) {
        resumes += 1;
        if (failure === "network") throw new TypeError("connection reset");
        return json({
          errorCode: "job_service_unavailable",
          retryable: true,
        }, { status: 503 });
      }
      reads += 1;
      if (reads === 1) {
        return json(status("failed", {
          errorCode: "analysis_timeout",
          retryable: true,
        }));
      }
      if (reads === 2) {
        return json(status("running"), {
          headers: { "Retry-After": "0" },
        });
      }
      return json(status("succeeded", {
        downloadUrl: REPORT_URL,
        downloadUrlExpiresAt: EXPIRES_AT,
      }));
    };
    const clock = fakeClock();

    const complete = await pollAsyncAnalysis(
      ENDPOINT,
      receipt(),
      fetchImpl,
      {
        ...clock,
        timeoutMs: 5_000,
        defaultPollMilliseconds: 1_000,
      },
    );

    assert.equal(complete.status, "succeeded");
    assert.equal(resumes, 1);
    assert.equal(reads, 3);
    assert.deepEqual(clock.waits, [1_000, 1_000]);
  });
}

test("attempt exhaustion is terminal and never triggers another resume", async () => {
  let resumes = 0;
  const fetchImpl: FetchImpl = async (input) => {
    if (String(input).endsWith("/resume")) {
      resumes += 1;
      return json({ errorCode: "attempts_exhausted" }, { status: 409 });
    }
    return json(status("failed", {
      errorCode: "analysis_timeout",
      retryable: true,
    }));
  };

  await assert.rejects(
    pollAsyncAnalysis(
      ENDPOINT,
      receipt(),
      fetchImpl,
      { ...fakeClock(), timeoutMs: 5_000 },
    ),
    (error: unknown) => error instanceof AsyncJobClientError
      && error.code === "attempts_exhausted"
      && error.httpStatus === 409,
  );
  assert.equal(resumes, 1);
});

test("resume exposes 410 expiry and poll does not retry it", async () => {
  let calls = 0;
  const fetchImpl: FetchImpl = async () => {
    calls += 1;
    return json({ errorCode: "job_expired" }, { status: 410 });
  };

  await assert.rejects(
    resumeAsyncAnalysis(ENDPOINT, receipt(), fetchImpl),
    (error: unknown) => error instanceof AsyncJobClientError
      && error.code === "job_expired"
      && error.httpStatus === 410,
  );
  await assert.rejects(
    pollAsyncAnalysis(
      ENDPOINT,
      receipt(),
      fetchImpl,
      { ...fakeClock(), timeoutMs: 5_000 },
    ),
    (error: unknown) => error instanceof AsyncJobClientError
      && error.code === "job_expired",
  );
  assert.equal(calls, 2);
});

test("poll stops at the caller supplied timeout", async () => {
  const clock = fakeClock();
  let reads = 0;
  const fetchImpl: FetchImpl = async () => {
    reads += 1;
    return json(status("running"), { headers: { "Retry-After": "10" } });
  };

  await assert.rejects(
    pollAsyncAnalysis(
      ENDPOINT,
      receipt(),
      fetchImpl,
      { ...clock, timeoutMs: 2_500 },
    ),
    (error: unknown) => error instanceof AsyncJobClientError
      && error.code === "poll_timeout",
  );
  assert.equal(reads, 1);
  assert.deepEqual(clock.waits, [2_500]);
});

test("poll aborts a hung job read at the caller supplied timeout", {
  timeout: 500,
}, async () => {
  let requestSignal: AbortSignal | null = null;
  const neverReturningFetch: FetchImpl = async (_input, init) => {
    requestSignal = init?.signal ?? null;
    return new Promise<Response>(() => undefined);
  };

  await assert.rejects(
    pollAsyncAnalysis(
      ENDPOINT,
      receipt(),
      neverReturningFetch,
      { timeoutMs: 25 },
    ),
    (error: unknown) => error instanceof AsyncJobClientError
      && error.code === "poll_timeout",
  );
  assert.equal((requestSignal as AbortSignal | null)?.aborted, true);
});

test("download accepts recognized AWS S3 virtual-hosted and path-style URLs", async () => {
  const validUrls = [
    "https://reports-bucket.s3.amazonaws.com/report.md?X-Amz-Signature=test",
    "https://reports-bucket.s3.us-east-1.amazonaws.com/report.md?X-Amz-Signature=test",
    "https://s3-reports.s3.us-east-1.amazonaws.com/report.md?X-Amz-Signature=test",
    "https://reports-bucket.s3-us-west-2.amazonaws.com/report.md?X-Amz-Signature=test",
    "https://reports-bucket.s3.dualstack.eu-west-1.amazonaws.com/report.md?X-Amz-Signature=test",
    "https://s3.us-east-1.amazonaws.com/reports-bucket/report.md?X-Amz-Signature=test",
    "https://reports-bucket.s3.cn-north-1.amazonaws.com.cn/report.md?X-Amz-Signature=test",
  ];
  for (const downloadUrl of validUrls) {
    let requested = "";
    const report = await downloadAsyncReport(
      ENDPOINT,
      receipt(),
      status("succeeded", {
        downloadUrl,
        downloadUrlExpiresAt: EXPIRES_AT,
      }),
      async (input, init) => {
        requested = String(input);
        assert.equal(init?.redirect, "error");
        assert.equal(
          new Headers(init?.headers).has("X-Job-Token"),
          false,
        );
        return new Response("# safe");
      },
    );
    assert.equal(requested, downloadUrl);
    assert.equal(report, "# safe");
  }
});

test("download rejects non-S3, internal, insecure, credentialed, and nonstandard-port URLs", async () => {
  const maliciousUrls = [
    "http://reports-bucket.s3.us-east-1.amazonaws.com/report.md",
    "https://evil.example/report.md",
    "https://reports-bucket.s3.us-east-1.amazonaws.com.evil.example/report.md",
    "https://127.0.0.1/report.md",
    "https://localhost/report.md",
    "https://169.254.169.254/latest/meta-data/",
    "https://user@reports-bucket.s3.us-east-1.amazonaws.com/report.md",
    "https://reports-bucket.s3.us-east-1.amazonaws.com:444/report.md",
    "https://s3.internal/report.md",
    "https://s3.amazonaws.com.cn/reports-bucket/report.md",
  ];
  for (const downloadUrl of maliciousUrls) {
    let fetched = false;
    await assert.rejects(
      downloadAsyncReport(
        ENDPOINT,
        receipt(),
        status("succeeded", {
          downloadUrl,
          downloadUrlExpiresAt: EXPIRES_AT,
        }),
        async () => {
          fetched = true;
          return new Response("must not fetch");
        },
      ),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "invalid_response",
      downloadUrl,
    );
    assert.equal(fetched, false);
  }
});

test("presigned downloads reject redirects without forwarding credentials", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  await assert.rejects(
    downloadAsyncReport(
      ENDPOINT,
      receipt(),
      status("succeeded", {
        downloadUrl: REPORT_URL,
        downloadUrlExpiresAt: EXPIRES_AT,
      }),
      async (input, init) => {
        calls.push({ url: String(input), init });
        return new Response(null, {
          status: 302,
          headers: { Location: "http://169.254.169.254/latest/meta-data/" },
        });
      },
    ),
    (error: unknown) => error instanceof AsyncJobClientError
      && error.code === "download_http_302",
  );
  assert.equal(calls.length, 1);
  assert.equal(calls[0]?.init?.redirect, "error");
  assert.equal(
    new Headers(calls[0]?.init?.headers).has("X-Job-Token"),
    false,
  );
});

test("download renews an expired presigned URL through an authenticated job read", async () => {
  const calls: Array<{ url: string; headers: Headers }> = [];
  const fetchImpl: FetchImpl = async (input, init) => {
    const url = String(input);
    calls.push({ url, headers: new Headers(init?.headers) });
    if (url.includes(`/x402/jobs/${JOB_ID}`)) {
      return json(status("succeeded", {
        downloadUrl: NEW_REPORT_URL,
        downloadUrlExpiresAt: 50_000,
      }));
    }
    if (url === NEW_REPORT_URL) {
      return new Response("renewed report");
    }
    throw new Error(`stale URL must not be fetched: ${url}`);
  };

  const report = await downloadAsyncReport(
    ENDPOINT,
    receipt(),
    status("succeeded", {
      downloadUrl: OLD_REPORT_URL,
      downloadUrlExpiresAt: 900,
    }),
    fetchImpl,
    { now: () => 1_000 },
  );

  assert.equal(report, "renewed report");
  assert.equal(calls[0]?.headers.get("X-Job-Token"), JOB_TOKEN);
  assert.equal(calls[1]?.headers.has("X-Job-Token"), false);
});

test("download refreshes once after a rejected presigned URL", async () => {
  const calls: string[] = [];
  const fetchImpl: FetchImpl = async (input) => {
    const url = String(input);
    calls.push(url);
    if (url === OLD_REPORT_URL) {
      return new Response("expired", { status: 403 });
    }
    if (url.includes(`/x402/jobs/${JOB_ID}`)) {
      return json(status("succeeded", {
        downloadUrl: NEW_REPORT_URL,
        downloadUrlExpiresAt: EXPIRES_AT,
      }));
    }
    return new Response("fresh report");
  };

  const report = await downloadAsyncReport(
    ENDPOINT,
    receipt(),
    status("succeeded", {
      downloadUrl: OLD_REPORT_URL,
      downloadUrlExpiresAt: EXPIRES_AT,
    }),
    fetchImpl,
  );

  assert.equal(report, "fresh report");
  assert.deepEqual(calls, [
    OLD_REPORT_URL,
    `${ENDPOINT}/x402/jobs/${JOB_ID}`,
    NEW_REPORT_URL,
  ]);
});

test("download rejects reports over 2 MiB from headers or streaming bytes", async () => {
  const complete = status("succeeded", {
    downloadUrl: REPORT_URL,
    downloadUrlExpiresAt: EXPIRES_AT,
  });

  await assert.rejects(
    downloadAsyncReport(
      ENDPOINT,
      receipt(),
      complete,
      async () => new Response("small", {
        headers: { "Content-Length": String(2 * 1024 * 1024 + 1) },
      }),
    ),
    (error: unknown) => error instanceof AsyncJobClientError
      && error.code === "report_too_large",
  );

  const oversized = new Uint8Array(2 * 1024 * 1024 + 1);
  await assert.rejects(
    downloadAsyncReport(
      ENDPOINT,
      receipt(),
      complete,
      async () => new Response(new ReadableStream({
        start(controller) {
          controller.enqueue(oversized);
          controller.close();
        },
      })),
    ),
    (error: unknown) => error instanceof AsyncJobClientError
      && error.code === "report_too_large",
  );
});

test("pending-create and receipt records are atomic, owner-only, and leave no temp files", () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-atomic-"));
  const pendingPath = join(directory, "state", "pending.json");
  const receiptPath = join(directory, "state", "receipt.json");
  const now = 1_000_000;
  const request = {
    symbols: ["AAPL"],
    analysis_type: "comprehensive",
    portfolio: [{ symbol: "AAPL", shares: 1 }],
  };
  const pending = createPendingRecord(
    proofExpiringAt(now / 1_000 + 600),
    request,
    now,
  );
  try {
    persistPendingCreate(pendingPath, pending);
    assert.deepEqual(loadPendingCreate(pendingPath), pending);
    assert.equal(statSync(pendingPath).mode & 0o777, 0o600);

    persistAsyncJobReceipt(receiptPath, receipt());
    persistAsyncJobReceipt(receiptPath, receipt({ expiresAt: EXPIRES_AT + 1 }));
    assert.equal(statSync(receiptPath).mode & 0o777, 0o600);
    assert.deepEqual(JSON.parse(readFileSync(receiptPath, "utf8")), {
      jobId: JOB_ID,
      jobToken: JOB_TOKEN,
      statusUrl: `/x402/jobs/${JOB_ID}`,
      expiresAt: EXPIRES_AT + 1,
    });
    assert.deepEqual(
      readdirSync(join(directory, "state")).sort(),
      ["pending.json", "receipt.json"],
    );
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("exclusive CLI lock fails fast and never removes a replacement lock", () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-lock-"));
  const lockPath = join(directory, "state", "x402.lock");
  try {
    const release = acquireExclusiveCliLock(lockPath);
    assert.equal(statSync(lockPath).mode & 0o777, 0o600);
    assert.throws(
      () => acquireExclusiveCliLock(lockPath),
      /another x402 asynchronous client is active/,
    );
    release();
    assert.equal(existsSync(lockPath), false);

    const releaseOriginal = acquireExclusiveCliLock(lockPath);
    rmSync(lockPath);
    writeFileSync(lockPath, "replacement", { mode: 0o600 });
    releaseOriginal();
    assert.equal(readFileSync(lockPath, "utf8"), "replacement");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("graceful signal cleanup removes only the owned lock and unregisters handlers", () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-signal-lock-"));
  const lockPath = join(directory, "state", "x402.lock");
  class FakeSignalHost extends EventEmitter {
    readonly pid = 4242;
    readonly kills: Array<{ pid: number; signal: NodeJS.Signals }> = [];

    kill(pid: number, signal: NodeJS.Signals): boolean {
      this.kills.push({ pid, signal });
      return true;
    }
  }
  try {
    const host = new FakeSignalHost();
    const release = acquireExclusiveCliLock(lockPath);
    const removeHandlers = installGracefulLockCleanup(release, host);
    assert.equal(host.listenerCount("SIGINT"), 1);
    assert.equal(host.listenerCount("SIGTERM"), 1);

    host.emit("SIGTERM");
    assert.equal(existsSync(lockPath), false);
    assert.deepEqual(host.kills, [{ pid: 4242, signal: "SIGTERM" }]);
    assert.equal(host.listenerCount("SIGINT"), 0);
    assert.equal(host.listenerCount("SIGTERM"), 0);
    removeHandlers();

    const replacementHost = new FakeSignalHost();
    const releaseOriginal = acquireExclusiveCliLock(lockPath);
    const removeReplacementHandlers = installGracefulLockCleanup(
      releaseOriginal,
      replacementHost,
    );
    rmSync(lockPath);
    writeFileSync(lockPath, "replacement-owner", { mode: 0o600 });
    replacementHost.emit("SIGINT");
    assert.equal(readFileSync(lockPath, "utf8"), "replacement-owner");
    assert.deepEqual(
      replacementHost.kills,
      [{ pid: 4242, signal: "SIGINT" }],
    );
    removeReplacementHandlers();
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("normal lock cleanup unregisters signal handlers before releasing", () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-signal-remove-"));
  const lockPath = join(directory, "x402.lock");
  class FakeSignalHost extends EventEmitter {
    readonly pid = 4242;

    kill(): boolean {
      throw new Error("unexpected re-signal");
    }
  }
  try {
    const host = new FakeSignalHost();
    const release = acquireExclusiveCliLock(lockPath);
    const removeHandlers = installGracefulLockCleanup(release, host);
    removeHandlers();
    assert.equal(host.listenerCount("SIGINT"), 0);
    assert.equal(host.listenerCount("SIGTERM"), 0);
    release();
    assert.equal(existsSync(lockPath), false);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("stale private-state temp cleanup removes only exact owned temp names", () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-temp-cleanup-"));
  const pendingName = "pending.json";
  const receiptName = "receipt.json";
  const stalePending = `.${pendingName}.123.${"a".repeat(16)}.tmp`;
  const staleReceipt = `.${receiptName}.456.${"b".repeat(16)}.tmp`;
  const unrelated = `.${pendingName}.not-owned.tmp`;
  try {
    writeFileSync(join(directory, stalePending), "secret", { mode: 0o600 });
    writeFileSync(join(directory, staleReceipt), "secret", { mode: 0o600 });
    writeFileSync(join(directory, unrelated), "keep", { mode: 0o600 });

    cleanupPrivateStateTemps(directory, [pendingName, receiptName]);

    assert.equal(existsSync(join(directory, stalePending)), false);
    assert.equal(existsSync(join(directory, staleReceipt)), false);
    assert.equal(readFileSync(join(directory, unrelated), "utf8"), "keep");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("ambiguous create retries and restarts reuse one persisted proof and request", async () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-pending-retry-"));
  const pendingPath = join(directory, "pending.json");
  const receiptPath = join(directory, "receipt.json");
  const clock = fakeClock(1_000_000);
  const request = {
    symbols: ["AAPL"],
    analysis_type: "comprehensive",
    portfolio: [{ symbol: "AAPL", shares: 1 }],
    risk_profile: { tolerance: "moderate" },
  };
  const pending = createPendingRecord(
    proofExpiringAt(clock.now() / 1_000 + 600),
    request,
    clock.now(),
  );
  persistPendingCreate(pendingPath, pending);
  const calls: Array<{ proof: string | null; body: string }> = [];
  const ambiguous: FetchImpl = async (_input, init) => {
    assert.equal(existsSync(pendingPath), true);
    assert.equal(existsSync(receiptPath), false);
    calls.push({
      proof: new Headers(init?.headers).get("X-Payment"),
      body: String(init?.body),
    });
    throw new TypeError("response lost after server accepted payment");
  };
  try {
    await assert.rejects(
      createReceiptFromPending(
        ENDPOINT,
        pending,
        pendingPath,
        receiptPath,
        ambiguous,
        {
          ...clock,
          maxAttempts: 1,
          baseRetryMilliseconds: 1_000,
        },
      ),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "network_error",
    );
    assert.equal(existsSync(pendingPath), true);
    assert.equal(existsSync(receiptPath), false);

    const loaded = loadPendingCreate(pendingPath);
    const recovered = await createReceiptFromPending(
      ENDPOINT,
      loaded,
      pendingPath,
      receiptPath,
      async (_input, init) => {
        calls.push({
          proof: new Headers(init?.headers).get("X-Payment"),
          body: String(init?.body),
        });
        return json(receipt(), { status: 202 });
      },
      {
        ...clock,
        maxAttempts: 3,
        baseRetryMilliseconds: 1_000,
      },
    );

    assert.equal(recovered.jobId, JOB_ID);
    assert.equal(calls.length, 2);
    assert.ok(calls.every((call) => call.proof === pending.paymentProof));
    assert.ok(calls.every((call) => call.body === JSON.stringify(request)));
    assert.equal(
      existsSync(pendingPath),
      true,
      "create alone cannot replace the first authoritative authenticated GET",
    );
    assert.equal(statSync(receiptPath).mode & 0o777, 0o600);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("settling recovery retains both files across interruption and resubmits the exact proof", async () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-settling-recovery-"));
  const pendingPath = join(directory, "pending.json");
  const receiptPath = join(directory, "receipt.json");
  const initialClock = fakeClock(5_000_000);
  const pending = createPendingRecord(
    proofExpiringAt(initialClock.now() / 1_000 + 600),
    { symbols: ["AAPL"], analysis_type: "comprehensive" },
    initialClock.now(),
  );
  persistPendingCreate(pendingPath, pending);
  const submittedProofs: Array<string | null> = [];
  try {
    await assert.rejects(
      createReceiptFromPending(
        ENDPOINT,
        pending,
        pendingPath,
        receiptPath,
        async (_input, init) => {
          submittedProofs.push(new Headers(init?.headers).get("X-Payment"));
          return await new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => {
              reject(new DOMException("ambiguous timeout", "AbortError"));
            }, { once: true });
          });
        },
        {
          ...initialClock,
          maxAttempts: 1,
          requestTimeoutMilliseconds: 1_000,
          scheduleTimeout: (callback) => {
            queueMicrotask(callback);
            return 1;
          },
          clearScheduledTimeout: () => undefined,
        },
      ),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "network_error",
    );
    assert.equal(existsSync(pendingPath), true);
    assert.equal(existsSync(receiptPath), false);

    const settlingReceipt = await createReceiptFromPending(
      ENDPOINT,
      loadPendingCreate(pendingPath),
      pendingPath,
      receiptPath,
      async (_input, init) => {
        submittedProofs.push(new Headers(init?.headers).get("X-Payment"));
        return json(receipt({ status: "settling" }), { status: 202 });
      },
      {
        ...initialClock,
        maxAttempts: 1,
      },
    );
    assert.equal(settlingReceipt.status, "settling");
    assert.equal(existsSync(receiptPath), true);
    assert.equal(
      existsSync(pendingPath),
      true,
      "settling must retain the only proof capable of stale reconciliation",
    );

    const interruptedClock = fakeClock(initialClock.now());
    await assert.rejects(
      pollAsyncAnalysisWithPendingRecovery(
        ENDPOINT,
        loadAsyncJobReceipt(receiptPath),
        pendingPath,
        receiptPath,
        async () => json(status("settling"), {
          headers: { "Retry-After": "1" },
        }),
        {
          ...interruptedClock,
          timeoutMs: 500,
          defaultPollMilliseconds: 1_000,
          settlingRecoveryMilliseconds: 1_000,
        },
      ),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "poll_timeout",
    );
    assert.equal(existsSync(receiptPath), true);
    assert.equal(existsSync(pendingPath), true);

    const recoveryClock = fakeClock(interruptedClock.now());
    let jobReads = 0;
    const completed = await pollAsyncAnalysisWithPendingRecovery(
      ENDPOINT,
      loadAsyncJobReceipt(receiptPath),
      pendingPath,
      receiptPath,
      async (input, init) => {
        const url = String(input);
        if (url.endsWith("/x402/analyze/async")) {
          submittedProofs.push(new Headers(init?.headers).get("X-Payment"));
          return json(receipt({ status: "queued" }), { status: 202 });
        }
        jobReads += 1;
        if (jobReads <= 2) {
          return json(status("settling"), {
            headers: { "Retry-After": "1" },
          });
        }
        if (jobReads === 3) {
          return json(status("running"), {
            headers: { "Retry-After": "1" },
          });
        }
        return json(status("succeeded", {
          downloadUrl: REPORT_URL,
          downloadUrlExpiresAt: EXPIRES_AT - 1,
        }));
      },
      {
        ...recoveryClock,
        timeoutMs: 10_000,
        defaultPollMilliseconds: 1_000,
        settlingRecoveryMilliseconds: 1_000,
      },
      {
        ...recoveryClock,
        maxAttempts: 1,
      },
    );

    assert.equal(completed.status, "succeeded");
    assert.deepEqual(submittedProofs, [
      pending.paymentProof,
      pending.paymentProof,
      pending.paymentProof,
    ]);
    assert.equal(existsSync(pendingPath), false);
    assert.equal(existsSync(receiptPath), true);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("queued GET removes pending before a later network interruption", async () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-authoritative-cleanup-"));
  const pendingPath = join(directory, "pending.json");
  const receiptPath = join(directory, "receipt.json");
  const clock = fakeClock(6_000_000);
  const pending = bindPendingToJob(
    createPendingRecord(
      proofExpiringAt(clock.now() / 1_000 + 600),
      { symbols: ["AAPL"] },
      clock.now(),
    ),
    JOB_ID,
    JOB_TOKEN,
  );
  persistPendingCreate(pendingPath, pending);
  persistAsyncJobReceipt(receiptPath, receipt({ status: "settling" }));
  let reads = 0;
  try {
    await assert.rejects(
      pollAsyncAnalysisWithPendingRecovery(
        ENDPOINT,
        loadAsyncJobReceipt(receiptPath),
        pendingPath,
        receiptPath,
        async () => {
          reads += 1;
          if (reads === 1) {
            return json(status("queued"), {
              headers: { "Retry-After": "1" },
            });
          }
          throw new TypeError("interrupted after authoritative queue");
        },
        {
          ...clock,
          timeoutMs: 1_500,
          defaultPollMilliseconds: 1_000,
        },
      ),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "poll_timeout",
    );
    assert.equal(existsSync(pendingPath), false);
    assert.equal(existsSync(receiptPath), true);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("pending cleanup failure preserves the receipt and never deletes a replacement", async () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-cleanup-owner-"));
  const pendingPath = join(directory, "pending.json");
  const receiptPath = join(directory, "receipt.json");
  const now = 7_000_000;
  const pending = bindPendingToJob(
    createPendingRecord(
      proofExpiringAt(now / 1_000 + 600),
      { symbols: ["AAPL"] },
      now,
    ),
    JOB_ID,
    JOB_TOKEN,
  );
  const replacement = bindPendingToJob(
    createPendingRecord(
      proofExpiringAt(now / 1_000 + 600),
      { symbols: ["NVDA"] },
      now,
    ),
    JOB_ID,
    JOB_TOKEN,
  );
  persistPendingCreate(pendingPath, pending);
  persistAsyncJobReceipt(receiptPath, receipt({ status: "settling" }));
  try {
    await assert.rejects(
      pollAsyncAnalysisWithPendingRecovery(
        ENDPOINT,
        loadAsyncJobReceipt(receiptPath),
        pendingPath,
        receiptPath,
        async () => {
          persistPendingCreate(pendingPath, replacement);
          return json(status("queued"));
        },
        { ...fakeClock(now), timeoutMs: 5_000 },
      ),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "pending_cleanup_failed",
    );
    assert.deepEqual(loadPendingCreate(pendingPath), replacement);
    assert.equal(existsSync(receiptPath), true);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("create durably binds pending job identity before receipt persistence", async () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-pending-binding-"));
  const pendingPath = join(directory, "pending.json");
  const impossibleReceiptPath = join(directory, "receipt-directory");
  const now = 8_000_000;
  const pending = createPendingRecord(
    proofExpiringAt(now / 1_000 + 600),
    { symbols: ["AAPL"] },
    now,
  );
  persistPendingCreate(pendingPath, pending);
  mkdirSync(impossibleReceiptPath);
  try {
    await assert.rejects(
      createReceiptFromPending(
        ENDPOINT,
        pending,
        pendingPath,
        impossibleReceiptPath,
        async () => json(receipt({ status: "settling" }), { status: 202 }),
        { now: () => now, maxAttempts: 1 },
      ),
    );
    assert.equal(loadPendingCreate(pendingPath).jobId, JOB_ID);
    assert.equal(statSync(pendingPath).mode & 0o777, 0o600);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("quarantine cleanup deletes a matching bound pending record", () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-quarantine-match-"));
  const pendingPath = join(directory, "pending.json");
  const expected = bindPendingToJob(
    createPendingRecord(
      proofExpiringAt(9_000),
      { symbols: ["AAPL"] },
      8_000_000,
    ),
    JOB_ID,
    JOB_TOKEN,
  );
  try {
    persistPendingCreate(pendingPath, expected);
    quarantineRemoveOwnedPending(
      pendingPath,
      expected,
      JOB_ID,
      JOB_TOKEN,
    );
    assert.equal(existsSync(pendingPath), false);
    assert.deepEqual(readdirSync(directory), []);

    const laterReplacement = bindPendingToJob(
      {
        ...expected,
        request: { symbols: ["NVDA"] },
        binding: undefined,
      },
      JOB_ID,
      JOB_TOKEN,
    );
    persistPendingCreate(pendingPath, expected);
    quarantineRemoveOwnedPending(
      pendingPath,
      expected,
      JOB_ID,
      JOB_TOKEN,
      () => persistPendingCreate(pendingPath, laterReplacement),
    );
    assert.deepEqual(loadPendingCreate(pendingPath), laterReplacement);
    assert.deepEqual(readdirSync(directory), ["pending.json"]);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("quarantine cleanup restores a replacement present before atomic rename", () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-quarantine-before-"));
  const pendingPath = join(directory, "pending.json");
  const expected = bindPendingToJob(
    createPendingRecord(
      proofExpiringAt(10_000),
      { symbols: ["AAPL"] },
      9_000_000,
    ),
    JOB_ID,
    JOB_TOKEN,
  );
  const replacementJobId = `x402_${"b".repeat(32)}`;
  const replacement = bindPendingToJob(
    createPendingRecord(
      proofExpiringAt(10_000),
      { symbols: ["NVDA"] },
      9_000_000,
    ),
    replacementJobId,
    JOB_TOKEN,
  );
  try {
    persistPendingCreate(pendingPath, replacement);
    assert.throws(
      () => quarantineRemoveOwnedPending(
        pendingPath,
        expected,
        JOB_ID,
        JOB_TOKEN,
      ),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "pending_cleanup_failed",
    );
    assert.deepEqual(loadPendingCreate(pendingPath), replacement);
    const entries = readdirSync(directory);
    assert.equal(entries.includes("pending.json"), true);
    const quarantine = entries.find((entry) => entry !== "pending.json");
    assert.ok(quarantine, "mismatched quarantine remains race-safe");
    assert.deepEqual(
      loadPendingCreate(join(directory, quarantine)),
      replacement,
    );
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("quarantine cleanup never overwrites a replacement created after rename", () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-quarantine-after-"));
  const pendingPath = join(directory, "pending.json");
  const expected = bindPendingToJob(
    createPendingRecord(
      proofExpiringAt(11_000),
      { symbols: ["AAPL"] },
      10_000_000,
    ),
    JOB_ID,
    JOB_TOKEN,
  );
  const quarantinedReplacement = bindPendingToJob(
    { ...expected, request: { symbols: ["MSFT"] }, binding: undefined },
    JOB_ID,
    JOB_TOKEN,
  );
  const laterReplacement = bindPendingToJob(
    { ...expected, request: { symbols: ["NVDA"] }, binding: undefined },
    JOB_ID,
    JOB_TOKEN,
  );
  try {
    persistPendingCreate(pendingPath, quarantinedReplacement);
    assert.throws(
      () => quarantineRemoveOwnedPending(
        pendingPath,
        expected,
        JOB_ID,
        JOB_TOKEN,
        () => persistPendingCreate(pendingPath, laterReplacement),
      ),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "pending_cleanup_failed",
    );
    assert.deepEqual(loadPendingCreate(pendingPath), laterReplacement);
    const quarantine = readdirSync(directory).find(
      (entry) => entry !== "pending.json",
    );
    assert.ok(quarantine, "mismatched quarantine is preserved for recovery");
    assert.deepEqual(
      loadPendingCreate(join(directory, quarantine)),
      quarantinedReplacement,
    );
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("restart never adopts or deletes unbound or mismatched pending state", async () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-bound-restart-"));
  const pendingPath = join(directory, "pending.json");
  const receiptPath = join(directory, "receipt.json");
  const now = 11_000_000;
  const unbound = createPendingRecord(
    proofExpiringAt(now / 1_000 + 600),
    { symbols: ["AAPL"] },
    now,
  );
  const mismatched = {
    ...unbound,
    jobId: `x402_${"b".repeat(32)}`,
  };
  persistAsyncJobReceipt(receiptPath, receipt({ status: "settling" }));
  try {
    for (const candidate of [unbound, mismatched]) {
      persistPendingCreate(pendingPath, candidate);
      let fetched = false;
      await assert.rejects(
        pollAsyncAnalysisWithPendingRecovery(
          ENDPOINT,
          loadAsyncJobReceipt(receiptPath),
          pendingPath,
          receiptPath,
          async () => {
            fetched = true;
            return json(status("settling"));
          },
          { ...fakeClock(now), timeoutMs: 5_000 },
        ),
        (error: unknown) => error instanceof AsyncJobClientError
          && error.code === "pending_job_mismatch",
      );
      assert.equal(fetched, false);
      assert.deepEqual(loadPendingCreate(pendingPath), candidate);
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("pending HMAC binding is stable across request key insertion order", () => {
  const now = 12_000_000;
  const proof = proofExpiringAt(now / 1_000 + 600);
  const first = createPendingRecord(
    proof,
    {
      symbols: ["AAPL"],
      risk_profile: {
        tolerance: "moderate",
        nested: { z: 1, a: 2 },
      },
    },
    now,
  );
  const second = createPendingRecord(
    proof,
    {
      risk_profile: {
        nested: { a: 2, z: 1 },
        tolerance: "moderate",
      },
      symbols: ["AAPL"],
    },
    now,
  );

  assert.deepEqual(
    bindPendingToJob(first, JOB_ID, JOB_TOKEN).binding,
    bindPendingToJob(second, JOB_ID, JOB_TOKEN).binding,
  );
});

test("restart rejects same-job pending with changed identity or invalid binding", async () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-binding-tamper-"));
  const pendingPath = join(directory, "pending.json");
  const receiptPath = join(directory, "receipt.json");
  const now = 13_000_000;
  const original = bindPendingToJob(
    createPendingRecord(
      proofExpiringAt(now / 1_000 + 600),
      { symbols: ["AAPL"], risk_profile: { tolerance: "moderate" } },
      now,
    ),
    JOB_ID,
    JOB_TOKEN,
  );
  const { binding: _binding, ...missingBinding } = original;
  const candidates = [
    {
      ...original,
      request: { symbols: ["NVDA"] },
    },
    {
      ...original,
      paymentProof: proofExpiringAt(now / 1_000 + 601),
      proofExpiresAt: (now / 1_000 + 601) * 1_000,
    },
    {
      ...original,
      binding: {
        version: 1 as const,
        mac: `${original.binding!.mac[0] === "A" ? "B" : "A"}`
          + original.binding!.mac.slice(1),
      },
    },
    {
      ...original,
      binding: {
        version: 1 as const,
        mac: "too-short",
      },
    },
    missingBinding,
  ];
  persistAsyncJobReceipt(receiptPath, receipt({ status: "settling" }));
  try {
    for (const candidate of candidates) {
      const serialized = `${JSON.stringify(candidate)}\n`;
      writeFileSync(pendingPath, serialized, { mode: 0o600 });
      let fetched = false;
      await assert.rejects(
        pollAsyncAnalysisWithPendingRecovery(
          ENDPOINT,
          loadAsyncJobReceipt(receiptPath),
          pendingPath,
          receiptPath,
          async () => {
            fetched = true;
            return json(status("queued"));
          },
          { ...fakeClock(now), timeoutMs: 5_000 },
        ),
        (error: unknown) => error instanceof AsyncJobClientError
          && error.code === "pending_binding_invalid",
      );
      assert.equal(fetched, false);
      assert.equal(readFileSync(pendingPath, "utf8"), serialized);
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("retryable create failures back off and reuse the identical pending identity", async () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-pending-backoff-"));
  const pendingPath = join(directory, "pending.json");
  const receiptPath = join(directory, "receipt.json");
  const clock = fakeClock(3_000_000);
  const pending = createPendingRecord(
    proofExpiringAt(clock.now() / 1_000 + 600),
    { symbols: ["AAPL"], analysis_type: "comprehensive" },
    clock.now(),
  );
  persistPendingCreate(pendingPath, pending);
  const proofs: Array<string | null> = [];
  const bodies: string[] = [];
  let attempts = 0;
  try {
    const result = await createReceiptFromPending(
      ENDPOINT,
      pending,
      pendingPath,
      receiptPath,
      async (_input, init) => {
        attempts += 1;
        proofs.push(new Headers(init?.headers).get("X-Payment"));
        bodies.push(String(init?.body));
        if (attempts === 1) {
          return json({
            errorCode: "job_service_unavailable",
            retryable: true,
          }, { status: 503 });
        }
        return json(receipt(), { status: 202 });
      },
      {
        ...clock,
        maxAttempts: 3,
        baseRetryMilliseconds: 1_000,
      },
    );
    assert.equal(result.jobId, JOB_ID);
    assert.deepEqual(proofs, [pending.paymentProof, pending.paymentProof]);
    assert.deepEqual(bodies, [
      JSON.stringify(pending.request),
      JSON.stringify(pending.request),
    ]);
    assert.deepEqual(clock.waits, [1_000]);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("a timed-out create retries with the same durable proof and clears every timer", async () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-create-timeout-"));
  const pendingPath = join(directory, "pending.json");
  const receiptPath = join(directory, "receipt.json");
  const clock = fakeClock(4_000_000);
  const pending = createPendingRecord(
    proofExpiringAt(clock.now() / 1_000 + 600),
    { symbols: ["AAPL"], analysis_type: "comprehensive" },
    clock.now(),
  );
  persistPendingCreate(pendingPath, pending);
  const proofs: Array<string | null> = [];
  const bodies: string[] = [];
  const timeoutMilliseconds: number[] = [];
  const cleared: unknown[] = [];
  let attempt = 0;
  try {
    const created = await createReceiptFromPending(
      ENDPOINT,
      pending,
      pendingPath,
      receiptPath,
      async (_input, init) => {
        attempt += 1;
        proofs.push(new Headers(init?.headers).get("X-Payment"));
        bodies.push(String(init?.body));
        if (attempt === 1) {
          return await new Promise<Response>((_resolve, reject) => {
            assert.equal(init?.signal?.aborted, false);
            init?.signal?.addEventListener("abort", () => {
              reject(new DOMException("timed out", "AbortError"));
            }, { once: true });
          });
        }
        return json(receipt(), { status: 202 });
      },
      {
        ...clock,
        maxAttempts: 2,
        baseRetryMilliseconds: 1_000,
        requestTimeoutMilliseconds: 5_000,
        scheduleTimeout: (callback, milliseconds) => {
          const handle = Symbol(`timer-${timeoutMilliseconds.length}`);
          timeoutMilliseconds.push(milliseconds);
          queueMicrotask(callback);
          return handle;
        },
        clearScheduledTimeout: (handle) => {
          cleared.push(handle);
        },
      },
    );

    assert.equal(created.jobId, JOB_ID);
    assert.deepEqual(proofs, [pending.paymentProof, pending.paymentProof]);
    assert.deepEqual(bodies, [
      JSON.stringify(pending.request),
      JSON.stringify(pending.request),
    ]);
    assert.deepEqual(timeoutMilliseconds, [5_000, 5_000]);
    assert.equal(cleared.length, 2);
    assert.deepEqual(clock.waits, [1_000]);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("create timeout is a retryable ambiguous network failure and clears its timer", async () => {
  const timeoutHandle = Symbol("create-timeout");
  let cleared: unknown;
  let requestSignal: AbortSignal | undefined;

  await assert.rejects(
    createAsyncAnalysis(
      ENDPOINT,
      "same-proof",
      { symbols: ["AAPL"] },
      async (_input, init) => {
        requestSignal = init?.signal ?? undefined;
        return await new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(new DOMException("timed out", "AbortError"));
          }, { once: true });
        });
      },
      {
        requestTimeoutMilliseconds: 2_500,
        scheduleTimeout: (callback, milliseconds) => {
          assert.equal(milliseconds, 2_500);
          queueMicrotask(callback);
          return timeoutHandle;
        },
        clearScheduledTimeout: (handle) => {
          cleared = handle;
        },
      },
    ),
    (error: unknown) => error instanceof AsyncJobClientError
      && error.code === "network_error"
      && error.retryable,
  );
  assert.equal(requestSignal?.aborted, true);
  assert.equal(cleared, timeoutHandle);
});

test("create deadline covers a stalled response body and sanitizes the abort", async () => {
  let cleared = false;
  await assert.rejects(
    createAsyncAnalysis(
      ENDPOINT,
      "same-proof",
      { symbols: ["AAPL"] },
      async (_input, init) => new Response(new ReadableStream<Uint8Array>({
        start(controller) {
          init?.signal?.addEventListener("abort", () => {
            controller.error(new DOMException("secret transport detail", "AbortError"));
          }, { once: true });
        },
      }), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      }),
      {
        requestTimeoutMilliseconds: 2_500,
        scheduleTimeout: (callback) => {
          queueMicrotask(callback);
          return 1;
        },
        clearScheduledTimeout: () => {
          cleared = true;
        },
      },
    ),
    (error: unknown) => error instanceof AsyncJobClientError
      && error.code === "network_error"
      && !error.message.includes("secret"),
  );
  assert.equal(cleared, true);
});

test("create refuses a caller identity that differs from the durable pending record", async () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-pending-mismatch-"));
  const pendingPath = join(directory, "pending.json");
  const receiptPath = join(directory, "receipt.json");
  const now = 4_000_000;
  const pending = createPendingRecord(
    proofExpiringAt(now / 1_000 + 600),
    { symbols: ["AAPL"] },
    now,
  );
  persistPendingCreate(pendingPath, pending);
  let fetched = false;
  try {
    await assert.rejects(
      createReceiptFromPending(
        ENDPOINT,
        {
          ...pending,
          request: { symbols: ["NVDA"] },
        },
        pendingPath,
        receiptPath,
        async () => {
          fetched = true;
          return json(receipt(), { status: 202 });
        },
        { now: () => now, sleep: async () => undefined },
      ),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "pending_create_mismatch",
    );
    assert.equal(fetched, false);
    assert.equal(existsSync(pendingPath), true);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("create retries are bounded by attempts and pending-record expiry", async () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-pending-expiry-"));
  const pendingPath = join(directory, "pending.json");
  const receiptPath = join(directory, "receipt.json");
  const now = 2_000_000;
  const base = createPendingRecord(
    proofExpiringAt(now / 1_000 + 600),
    { symbols: ["AAPL"] },
    now,
  );
  try {
    let calls = 0;
    persistPendingCreate(pendingPath, base);
    await assert.rejects(
      createReceiptFromPending(
        ENDPOINT,
        base,
        pendingPath,
        receiptPath,
        async () => {
          calls += 1;
          return json({
            errorCode: "job_service_unavailable",
            retryable: true,
          }, { status: 503 });
        },
        {
          now: () => base.proofExpiresAt + 1,
          sleep: async () => undefined,
          maxAttempts: 5,
        },
      ),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "job_service_unavailable",
    );
    assert.equal(calls, 1, "an expired proof receives one reconciliation attempt");

    persistPendingCreate(pendingPath, base);
    await assert.rejects(
      createReceiptFromPending(
        ENDPOINT,
        base,
        pendingPath,
        receiptPath,
        async () => {
          calls += 1;
          return json(receipt(), { status: 202 });
        },
        {
          now: () => base.recoveryExpiresAt,
          sleep: async () => undefined,
        },
      ),
      (error: unknown) => error instanceof AsyncJobClientError
        && error.code === "pending_create_expired",
    );
    assert.equal(calls, 1, "an expired recovery record is never submitted");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("failure guidance claims retained state only when the file exists", () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-failure-message-"));
  const pendingPath = join(directory, "pending.json");
  const receiptPath = join(directory, "receipt.json");
  try {
    assert.doesNotMatch(
      pendingCreateFailureMessage(receiptPath, pendingPath),
      /retained/i,
    );
    writeFileSync(pendingPath, "pending", { mode: 0o600 });
    assert.match(
      pendingCreateFailureMessage(receiptPath, pendingPath),
      /pending payment request was retained/i,
    );
    writeFileSync(receiptPath, "receipt", { mode: 0o600 });
    assert.match(
      pendingCreateFailureMessage(receiptPath, pendingPath),
      /job receipt was retained/i,
    );
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("persists only the resumable receipt fields with owner-only permissions", () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-receipt-"));
  const path = join(directory, "nested", "receipt.json");
  try {
    persistAsyncJobReceipt(path, {
      ...receipt(),
      ignored: "must not be persisted",
    } as AsyncJobReceipt & { ignored: string });

    assert.deepEqual(JSON.parse(readFileSync(path, "utf8")), {
      jobId: JOB_ID,
      jobToken: JOB_TOKEN,
      statusUrl: `/x402/jobs/${JOB_ID}`,
      expiresAt: EXPIRES_AT,
    });
    assert.equal(statSync(path).mode & 0o777, 0o600);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("retains the receipt until the downloaded report is saved successfully", async () => {
  const directory = mkdtempSync(join(tmpdir(), "x402-finalize-"));
  const path = join(directory, "receipt.json");
  try {
    persistAsyncJobReceipt(path, receipt());
    await assert.rejects(
      finalizeAsyncReport(
        path,
        receipt(),
        "# report",
        ["AAPL"],
        async () => {
          throw new Error("disk full");
        },
      ),
      /disk full/,
    );
    assert.equal(readFileSync(path, "utf8").includes(JOB_TOKEN), true);

    let savedJobLabel = "";
    await finalizeAsyncReport(
      path,
      receipt(),
      "# report",
      ["AAPL"],
      async (_report, jobLabel) => {
        savedJobLabel = jobLabel;
        return { htmlPath: "/tmp/report.html", pdfPath: null };
      },
    );
    assert.match(savedJobLabel, /^\d+$/);
    assert.throws(() => readFileSync(path, "utf8"), { code: "ENOENT" });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
