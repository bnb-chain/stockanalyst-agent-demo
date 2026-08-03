import type {
  B402PaymentExtra,
  B402PaymentRequirement,
  B402PaymentResource,
  PaidPaymentChallenge,
} from "./x402-payment.js";

export type FetchImpl = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export type SleepImpl = (milliseconds: number) => Promise<void>;

export interface AsyncAnalysisRequest {
  symbols: string[];
  analysis_type?: string;
  portfolio?: unknown[];
  risk_profile?: unknown;
}

export interface AsyncJobReceipt {
  jobId: string;
  jobToken: string;
  status: string;
  statusUrl: string;
  expiresAt: number;
}

export interface AsyncJobStatus {
  jobId: string;
  status: "settling" | "queued" | "running" | "succeeded" | "failed";
  expiresAt: number;
  errorCode?: string;
  retryable?: boolean;
  downloadUrl?: string;
  downloadUrlExpiresAt?: number;
}

export interface PollOptions {
  timeoutMs?: number;
  sleep?: SleepImpl;
  now?: () => number;
  defaultPollMilliseconds?: number;
  settlingRecoveryMilliseconds?: number;
  runningRecoveryMilliseconds?: number;
  recoverSettling?: (
    remainingMilliseconds: number,
  ) => Promise<AsyncJobReceipt>;
  onAuthoritativeNonSettling?: () => Promise<void> | void;
}

export interface CreateRequestOptions {
  requestTimeoutMilliseconds?: number;
  scheduleTimeout?: (
    callback: () => void,
    milliseconds: number,
  ) => unknown;
  clearScheduledTimeout?: (handle: unknown) => void;
}

export interface DownloadOptions {
  now?: () => number;
}

export class AsyncJobClientError extends Error {
  readonly code: string;
  readonly httpStatus?: number;
  readonly retryable: boolean;

  constructor(
    code: string,
    options: { httpStatus?: number; retryable?: boolean } = {},
  ) {
    super(`x402 asynchronous job failed: ${code}`);
    this.name = "AsyncJobClientError";
    this.code = code;
    this.httpStatus = options.httpStatus;
    this.retryable = options.retryable ?? false;
  }
}

interface JobResponse {
  status: AsyncJobStatus;
  retryAfterMilliseconds: number;
}

const JOB_ID_PATTERN = /^x402_[0-9a-f]{32}$/;
const JOB_STATUSES = new Set([
  "settling",
  "queued",
  "running",
  "succeeded",
  "failed",
]);
const DEFAULT_POLL_MILLISECONDS = 10_000;
const DEFAULT_POLL_TIMEOUT_MILLISECONDS = 15 * 60 * 1_000;
const DEFAULT_SETTLING_RECOVERY_MILLISECONDS = 120_000;
const DEFAULT_RUNNING_RECOVERY_MILLISECONDS = 120_000;
const DEFAULT_CREATE_TIMEOUT_MILLISECONDS = 30_000;
const MAX_CREATE_TIMEOUT_MILLISECONDS = 5 * 60 * 1_000;
const MIN_POLL_MILLISECONDS = 1_000;
const MAX_REPORT_BYTES = 2 * 1024 * 1024;
const MAX_JSON_BYTES = 64 * 1024;
const BSC_TESTNET_NETWORK = "eip155:97";
const U_TOKEN_ADDRESS = "0x330949Aed7d00FCe0558C64ED6FeC9792616cC39";
const PAID_AMOUNT = "1000000";
const EVM_ADDRESS_PATTERN = /^0x[0-9a-fA-F]{40}$/;
const SAFE_SERVER_ERROR_CODES = new Set([
  "analysis_failed",
  "analysis_timeout",
  "async_jobs_paused",
  "attempts_exhausted",
  "invalid_request",
  "job_conflict",
  "job_expired",
  "job_not_found",
  "job_service_unavailable",
  "job_state_unavailable",
  "payment_failed",
  "payment_backend_unavailable",
  "payment_rejected",
  "payment_unavailable",
  "request_too_large",
  "settlement_pending",
]);
const AWS_REGION_PATTERN = /^(?:af|ap|ca|cn|eu|il|me|mx|sa|us|us-gov|us-iso|us-isob)-[a-z0-9-]+-\d+$/;
const DNS_LABEL_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requiredString(
  object: Record<string, unknown>,
  field: string,
): string {
  const value = object[field];
  if (typeof value !== "string" || value.length === 0) {
    throw new AsyncJobClientError("invalid_response");
  }
  return value;
}

function requiredInteger(
  object: Record<string, unknown>,
  field: string,
): number {
  const value = object[field];
  if (
    typeof value !== "number"
    || !Number.isSafeInteger(value)
    || value <= 0
  ) {
    throw new AsyncJobClientError("invalid_response");
  }
  return value;
}

function normalizedEndpoint(endpoint: string): URL {
  let parsed: URL;
  try {
    parsed = new URL(endpoint);
  } catch {
    throw new AsyncJobClientError("invalid_endpoint");
  }
  if (
    !["http:", "https:"].includes(parsed.protocol)
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
  ) {
    throw new AsyncJobClientError("invalid_endpoint");
  }
  return parsed;
}

function routeUrl(endpoint: string, path: string): string {
  const base = normalizedEndpoint(endpoint);
  const prefix = base.pathname.replace(/\/+$/, "");
  base.pathname = `${prefix}/${path.replace(/^\/+/, "")}`;
  return base.toString();
}

function invalidPaymentChallenge(): never {
  throw new AsyncJobClientError("invalid_payment_challenge");
}

function parsePaymentChallenge(
  endpoint: string,
  expectedSeller: string,
  value: unknown,
): PaidPaymentChallenge {
  if (!isRecord(value)) invalidPaymentChallenge();
  const paymentRequired = value["paymentRequired"];
  if (
    !isRecord(paymentRequired)
    || paymentRequired["x402Version"] !== 2
    || !Array.isArray(paymentRequired["accepts"])
    || paymentRequired["accepts"].length !== 1
  ) {
    invalidPaymentChallenge();
  }
  const acceptedValue = paymentRequired["accepts"][0];
  const resourceValue = paymentRequired["resource"];
  if (!isRecord(acceptedValue) || !isRecord(resourceValue)) {
    invalidPaymentChallenge();
  }
  const extraValue = acceptedValue["extra"];
  if (!isRecord(extraValue)) invalidPaymentChallenge();

  const resourceUrl = resourceValue["url"];
  const description = resourceValue["description"];
  const mimeType = resourceValue["mimeType"];
  const expectedResourceUrl = routeUrl(endpoint, "/x402/analyze/async");
  if (
    resourceUrl !== expectedResourceUrl
    || typeof description !== "string"
    || description.length === 0
    || mimeType !== "application/json"
  ) {
    invalidPaymentChallenge();
  }

  const seller = expectedSeller.toLowerCase();
  const asset = acceptedValue["asset"];
  const payTo = acceptedValue["payTo"];
  const timeout = acceptedValue["maxTimeoutSeconds"];
  const signerAddress = extraValue["signerAddress"];
  if (
    !EVM_ADDRESS_PATTERN.test(expectedSeller)
    || acceptedValue["scheme"] !== "exact"
    || acceptedValue["network"] !== BSC_TESTNET_NETWORK
    || acceptedValue["amount"] !== PAID_AMOUNT
    || typeof asset !== "string"
    || asset.toLowerCase() !== U_TOKEN_ADDRESS.toLowerCase()
    || typeof payTo !== "string"
    || payTo.toLowerCase() !== seller
    || !Number.isSafeInteger(timeout)
    || (timeout as number) <= 0
    || (timeout as number) > 3_600
    || extraValue["name"] !== "U"
    || extraValue["version"] !== "1"
    || extraValue["assetTransferMethod"] !== "eip3009"
    || typeof signerAddress !== "string"
    || !EVM_ADDRESS_PATTERN.test(signerAddress)
  ) {
    invalidPaymentChallenge();
  }

  const extra: B402PaymentExtra = {
    ...extraValue,
    name: "U",
    version: "1",
    assetTransferMethod: "eip3009",
    signerAddress,
  };
  const accepted: B402PaymentRequirement = {
    scheme: "exact",
    network: BSC_TESTNET_NETWORK,
    amount: PAID_AMOUNT,
    asset,
    payTo: payTo.toLowerCase(),
    maxTimeoutSeconds: timeout as number,
    extra,
  };
  const resource: B402PaymentResource = {
    url: resourceUrl,
    description,
    mimeType: "application/json",
  };
  return {
    x402Version: 2,
    resource,
    accepted,
  };
}

function jobUrl(endpoint: string, jobId: string): string {
  if (!JOB_ID_PATTERN.test(jobId)) {
    throw new AsyncJobClientError("invalid_receipt");
  }
  return routeUrl(endpoint, `/x402/jobs/${jobId}`);
}

export function canonicalStatusPath(
  endpoint: string | undefined,
  jobId: string,
  statusUrl: string,
): string {
  const expectedPath = `/x402/jobs/${jobId}`;
  if (!JOB_ID_PATTERN.test(jobId)) {
    throw new AsyncJobClientError("invalid_response");
  }
  if (statusUrl === expectedPath) return expectedPath;
  if (endpoint === undefined) {
    throw new AsyncJobClientError("invalid_response");
  }
  let supplied: URL;
  try {
    supplied = new URL(statusUrl, normalizedEndpoint(endpoint).origin);
  } catch {
    throw new AsyncJobClientError("invalid_response");
  }
  if (
    supplied.toString() !== jobUrl(endpoint, jobId)
    || supplied.username
    || supplied.password
  ) {
    throw new AsyncJobClientError("invalid_response");
  }
  return expectedPath;
}

function parseReceipt(endpoint: string, value: unknown): AsyncJobReceipt {
  if (!isRecord(value)) throw new AsyncJobClientError("invalid_response");
  const jobId = requiredString(value, "jobId");
  const jobToken = requiredString(value, "jobToken");
  const status = requiredString(value, "status");
  const statusUrl = requiredString(value, "statusUrl");
  const expiresAt = requiredInteger(value, "expiresAt");
  if (
    !JOB_ID_PATTERN.test(jobId)
    || !JOB_STATUSES.has(status)
    || jobToken.length > 4_096
    || /[\r\n]/.test(jobToken)
  ) {
    throw new AsyncJobClientError("invalid_response");
  }
  return {
    jobId,
    jobToken,
    status,
    statusUrl: canonicalStatusPath(endpoint, jobId, statusUrl),
    expiresAt,
  };
}

function parseDownloadUrl(value: unknown): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new AsyncJobClientError("invalid_response");
  }
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new AsyncJobClientError("invalid_response");
  }
  if (
    parsed.protocol !== "https:"
    || parsed.username
    || parsed.password
    || parsed.port
    || !isAwsS3Hostname(parsed.hostname)
    || parsed.pathname === "/"
  ) {
    throw new AsyncJobClientError("invalid_response");
  }
  return parsed.toString();
}

function isAwsS3Hostname(hostname: string): boolean {
  const host = hostname.toLowerCase();
  let suffix: string;
  if (host.endsWith(".amazonaws.com.cn")) {
    suffix = ".amazonaws.com.cn";
  } else if (host.endsWith(".amazonaws.com")) {
    suffix = ".amazonaws.com";
  } else {
    return false;
  }
  const labels = host.slice(0, -suffix.length).split(".");
  const regionAllowed = (region: string): boolean => (
    AWS_REGION_PATTERN.test(region)
    && (suffix !== ".amazonaws.com.cn" || region.startsWith("cn-"))
  );
  for (let serviceIndex = 0; serviceIndex < labels.length; serviceIndex += 1) {
    const service = labels[serviceIndex] ?? "";
    if (service !== "s3" && !service.startsWith("s3-")) continue;
    const bucketLabels = labels.slice(0, serviceIndex);
    if (!bucketLabels.every((label) => DNS_LABEL_PATTERN.test(label))) {
      continue;
    }
    const tail = labels.slice(serviceIndex + 1);
    if (service === "s3") {
      if (
        (tail.length === 0 && suffix === ".amazonaws.com")
        || (tail.length === 1 && regionAllowed(tail[0] ?? ""))
        || (
          tail.length === 2
          && tail[0] === "dualstack"
          && regionAllowed(tail[1] ?? "")
        )
      ) {
        return true;
      }
      continue;
    }
    if (service === "s3-fips") {
      if (tail.length === 1 && regionAllowed(tail[0] ?? "")) return true;
      continue;
    }
    if (service === "s3-accelerate") {
      if (
        bucketLabels.length > 0
        && (
          tail.length === 0
          || (tail.length === 1 && tail[0] === "dualstack")
        )
      ) {
        return true;
      }
      continue;
    }
    if (tail.length !== 0) continue;
    const embeddedRegion = service.slice("s3-".length);
    if (embeddedRegion.startsWith("fips-")) {
      if (regionAllowed(embeddedRegion.slice("fips-".length))) return true;
      continue;
    }
    if (regionAllowed(embeddedRegion)) return true;
  }
  return false;
}

function safeServerErrorCode(value: unknown, fallback: string): string {
  if (
    typeof value === "string"
    && value.length <= 64
    && /^[a-z][a-z0-9_]*$/.test(value)
    && SAFE_SERVER_ERROR_CODES.has(value)
  ) {
    return value;
  }
  return fallback;
}

function parseJobStatus(expectedJobId: string, value: unknown): AsyncJobStatus {
  if (!isRecord(value)) throw new AsyncJobClientError("invalid_response");
  const jobId = requiredString(value, "jobId");
  const rawStatus = requiredString(value, "status");
  const expiresAt = requiredInteger(value, "expiresAt");
  if (
    jobId !== expectedJobId
    || !JOB_ID_PATTERN.test(jobId)
    || !JOB_STATUSES.has(rawStatus)
  ) {
    throw new AsyncJobClientError("invalid_response");
  }
  const status = rawStatus as AsyncJobStatus["status"];
  const result: AsyncJobStatus = { jobId, status, expiresAt };
  if (status === "failed") {
    result.errorCode = safeServerErrorCode(
      value["errorCode"],
      "analysis_failed",
    );
    if (typeof value["retryable"] !== "boolean") {
      throw new AsyncJobClientError("invalid_response");
    }
    result.retryable = value["retryable"];
  } else if (status === "succeeded") {
    result.downloadUrl = parseDownloadUrl(value["downloadUrl"]);
    result.downloadUrlExpiresAt = requiredInteger(
      value,
      "downloadUrlExpiresAt",
    );
  }
  return result;
}

async function readBoundedText(
  response: Response,
  maximumBytes: number,
  overflowCode: string,
): Promise<string> {
  const declared = response.headers.get("Content-Length");
  if (declared !== null) {
    const bytes = Number(declared);
    if (Number.isFinite(bytes) && bytes > maximumBytes) {
      await response.body?.cancel();
      throw new AsyncJobClientError(overflowCode);
    }
  }
  if (!response.body) return "";

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let received = 0;
  let text = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      received += value.byteLength;
      if (received > maximumBytes) {
        await reader.cancel();
        throw new AsyncJobClientError(overflowCode);
      }
      text += decoder.decode(value, { stream: true });
    }
    return text + decoder.decode();
  } finally {
    reader.releaseLock();
  }
}

async function readJson(response: Response): Promise<unknown> {
  const text = await readBoundedText(
    response,
    MAX_JSON_BYTES,
    "invalid_response",
  );
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new AsyncJobClientError("invalid_response", {
      httpStatus: response.status,
    });
  }
}

async function responseError(response: Response): Promise<AsyncJobClientError> {
  let code = `http_${response.status}`;
  let retryable = response.status >= 500;
  try {
    const body = await readJson(response);
    if (isRecord(body)) {
      code = safeServerErrorCode(body["errorCode"], code);
      if (typeof body["retryable"] === "boolean") {
        retryable = body["retryable"];
      }
    }
  } catch {
    // Keep a stable status-derived error and never expose an untrusted body.
  }
  return new AsyncJobClientError(code, {
    httpStatus: response.status,
    retryable,
  });
}

function tokenHeaders(token: string): HeadersInit {
  if (
    typeof token !== "string"
    || token.length === 0
    || token.length > 4_096
    || /[\r\n]/.test(token)
  ) {
    throw new AsyncJobClientError("invalid_receipt");
  }
  return {
    Accept: "application/json",
    "X-Job-Token": token,
  };
}

function retryAfterMilliseconds(response: Response, now: () => number): number {
  const raw = response.headers.get("Retry-After");
  if (raw === null || raw.trim() === "") return DEFAULT_POLL_MILLISECONDS;
  const seconds = Number(raw);
  if (Number.isFinite(seconds) && seconds >= 0) {
    return Math.max(MIN_POLL_MILLISECONDS, Math.ceil(seconds * 1_000));
  }
  const date = Date.parse(raw);
  if (Number.isFinite(date)) {
    return Math.max(MIN_POLL_MILLISECONDS, date - now());
  }
  return DEFAULT_POLL_MILLISECONDS;
}

async function safeFetch(
  fetchImpl: FetchImpl,
  input: RequestInfo | URL,
  init: RequestInit,
  errorCode = "network_error",
): Promise<Response> {
  try {
    return await fetchImpl(input, init);
  } catch {
    throw new AsyncJobClientError(errorCode, { retryable: true });
  }
}

async function getJobStatus(
  endpoint: string,
  receipt: AsyncJobReceipt,
  fetchImpl: FetchImpl,
  now: () => number,
  signal?: AbortSignal,
): Promise<JobResponse> {
  const response = await safeFetch(fetchImpl, jobUrl(endpoint, receipt.jobId), {
    method: "GET",
    headers: tokenHeaders(receipt.jobToken),
    redirect: "error",
    signal,
  });
  if (!response.ok) throw await responseError(response);
  return {
    status: parseJobStatus(receipt.jobId, await readJson(response)),
    retryAfterMilliseconds: retryAfterMilliseconds(response, now),
  };
}

async function resumeJob(
  endpoint: string,
  receipt: AsyncJobReceipt,
  fetchImpl: FetchImpl,
  now: () => number,
  signal?: AbortSignal,
): Promise<JobResponse> {
  const response = await safeFetch(fetchImpl, `${jobUrl(endpoint, receipt.jobId)}/resume`, {
    method: "POST",
    headers: tokenHeaders(receipt.jobToken),
    redirect: "error",
    signal,
  });
  if (response.status !== 202) throw await responseError(response);
  return {
    status: parseJobStatus(receipt.jobId, await readJson(response)),
    retryAfterMilliseconds: retryAfterMilliseconds(response, now),
  };
}

export async function fetchPaymentChallenge(
  endpoint: string,
  request: AsyncAnalysisRequest,
  expectedSeller: string,
  fetchImpl: FetchImpl = globalThis.fetch,
): Promise<PaidPaymentChallenge> {
  let body: string;
  try {
    body = JSON.stringify(request);
  } catch {
    throw new AsyncJobClientError("invalid_request");
  }
  const response = await safeFetch(
    fetchImpl,
    routeUrl(endpoint, "/x402/analyze/async"),
    {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body,
      redirect: "error",
    },
  );
  if (response.status !== 402) {
    throw new AsyncJobClientError("invalid_payment_challenge", {
      httpStatus: response.status,
      retryable: response.status >= 500,
    });
  }
  try {
    return parsePaymentChallenge(
      endpoint,
      expectedSeller,
      await readJson(response),
    );
  } catch (error) {
    if (
      error instanceof AsyncJobClientError
      && error.code === "invalid_payment_challenge"
    ) {
      throw error;
    }
    throw new AsyncJobClientError("invalid_payment_challenge", {
      httpStatus: response.status,
    });
  }
}

export async function createAsyncAnalysis(
  endpoint: string,
  paymentProof: string,
  request: AsyncAnalysisRequest,
  fetchImpl: FetchImpl = globalThis.fetch,
  options: CreateRequestOptions = {},
): Promise<AsyncJobReceipt> {
  let body: string;
  try {
    body = JSON.stringify(request);
  } catch {
    throw new AsyncJobClientError("invalid_request");
  }
  const requestTimeoutMilliseconds = (
    options.requestTimeoutMilliseconds ?? DEFAULT_CREATE_TIMEOUT_MILLISECONDS
  );
  if (
    !Number.isSafeInteger(requestTimeoutMilliseconds)
    || requestTimeoutMilliseconds < 1
    || requestTimeoutMilliseconds > MAX_CREATE_TIMEOUT_MILLISECONDS
  ) {
    throw new AsyncJobClientError("invalid_request");
  }
  const scheduleTimeout = options.scheduleTimeout ?? (
    (callback: () => void, milliseconds: number) => (
      setTimeout(callback, milliseconds)
    )
  );
  const clearScheduledTimeout = options.clearScheduledTimeout ?? (
    (handle: unknown) => clearTimeout(handle as ReturnType<typeof setTimeout>)
  );
  const controller = new AbortController();
  const timeoutHandle = scheduleTimeout(
    () => controller.abort(),
    requestTimeoutMilliseconds,
  );
  try {
    const response = await safeFetch(
      fetchImpl,
      routeUrl(endpoint, "/x402/analyze/async"),
      {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Payment": paymentProof,
        },
        body,
        redirect: "error",
        signal: controller.signal,
      },
    );
    if (response.status !== 202) throw await responseError(response);
    return parseReceipt(endpoint, await readJson(response));
  } catch (error) {
    if (error instanceof AsyncJobClientError) throw error;
    throw new AsyncJobClientError("network_error", { retryable: true });
  } finally {
    clearScheduledTimeout(timeoutHandle);
  }
}

export async function resumeAsyncAnalysis(
  endpoint: string,
  receipt: AsyncJobReceipt,
  fetchImpl: FetchImpl = globalThis.fetch,
): Promise<AsyncJobStatus> {
  return (await resumeJob(endpoint, receipt, fetchImpl, Date.now)).status;
}

export async function pollAsyncAnalysis(
  endpoint: string,
  receipt: AsyncJobReceipt,
  fetchImpl: FetchImpl = globalThis.fetch,
  options: PollOptions = {},
): Promise<AsyncJobStatus> {
  const now = options.now ?? Date.now;
  const sleep = options.sleep ?? (
    (milliseconds: number) => new Promise<void>(
      (resolve) => setTimeout(resolve, milliseconds),
    )
  );
  const timeoutMs = options.timeoutMs ?? DEFAULT_POLL_TIMEOUT_MILLISECONDS;
  const defaultPollMilliseconds = Math.max(
    MIN_POLL_MILLISECONDS,
    options.defaultPollMilliseconds ?? DEFAULT_POLL_MILLISECONDS,
  );
  const settlingRecoveryMilliseconds = (
    options.settlingRecoveryMilliseconds
    ?? DEFAULT_SETTLING_RECOVERY_MILLISECONDS
  );
  const runningRecoveryMilliseconds = (
    options.runningRecoveryMilliseconds
    ?? DEFAULT_RUNNING_RECOVERY_MILLISECONDS
  );
  if (
    !Number.isFinite(timeoutMs)
    || timeoutMs <= 0
    || !Number.isSafeInteger(settlingRecoveryMilliseconds)
    || settlingRecoveryMilliseconds < MIN_POLL_MILLISECONDS
    || !Number.isSafeInteger(runningRecoveryMilliseconds)
    || runningRecoveryMilliseconds < MIN_POLL_MILLISECONDS
  ) {
    throw new AsyncJobClientError("invalid_timeout");
  }
  parseReceipt(endpoint, receipt);
  const deadline = now() + timeoutMs;
  let resumeAttempted = false;
  let settlingObservedAt: number | undefined;
  let runningObservedAt: number | undefined;
  let settlingRecoveryEnabled = options.recoverSettling !== undefined;
  let authoritativeCleanupAttempted = false;

  const notifyAuthoritativeNonSettling = async (): Promise<void> => {
    if (
      authoritativeCleanupAttempted
      || options.onAuthoritativeNonSettling === undefined
    ) {
      return;
    }
    authoritativeCleanupAttempted = true;
    try {
      await options.onAuthoritativeNonSettling();
    } catch {
      throw new AsyncJobClientError("pending_cleanup_failed");
    }
  };

  const runBeforeDeadline = async <T>(
    operation: (signal: AbortSignal) => Promise<T>,
  ): Promise<T> => {
    const remaining = deadline - now();
    if (remaining <= 0) throw new AsyncJobClientError("poll_timeout");
    const controller = new AbortController();
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const expired = new Promise<never>((_resolve, reject) => {
      timeout = setTimeout(() => {
        controller.abort();
        reject(new AsyncJobClientError("poll_timeout"));
      }, remaining);
    });
    try {
      return await Promise.race([operation(controller.signal), expired]);
    } finally {
      if (timeout !== undefined) clearTimeout(timeout);
    }
  };

  const wait = async (requested: number): Promise<void> => {
    const remaining = deadline - now();
    if (remaining <= 0) throw new AsyncJobClientError("poll_timeout");
    const milliseconds = Math.min(
      remaining,
      Math.max(MIN_POLL_MILLISECONDS, requested),
    );
    await sleep(milliseconds);
    if (now() >= deadline) throw new AsyncJobClientError("poll_timeout");
  };

  while (true) {
    if (now() >= deadline) throw new AsyncJobClientError("poll_timeout");
    let response: JobResponse;
    try {
      response = await runBeforeDeadline(
        (signal) => getJobStatus(endpoint, receipt, fetchImpl, now, signal),
      );
    } catch (error) {
      if (error instanceof AsyncJobClientError) {
        if (error.httpStatus === 410) {
          await notifyAuthoritativeNonSettling();
          throw error;
        }
        if (!error.retryable) throw error;
      }
      await wait(defaultPollMilliseconds);
      continue;
    }

    const current = response.status;
    if (current.status !== "settling") {
      await notifyAuthoritativeNonSettling();
    }
    if (current.status === "succeeded") return current;
    if (current.status === "failed") {
      if (current.retryable === true && !resumeAttempted) {
        resumeAttempted = true;
        try {
          const resumed = await runBeforeDeadline(
            (signal) => resumeJob(endpoint, receipt, fetchImpl, now, signal),
          );
          await wait(resumed.retryAfterMilliseconds);
        } catch (error) {
          if (
            error instanceof AsyncJobClientError
            && error.httpStatus === 409
            && error.code === "job_conflict"
          ) {
            continue;
          }
          if (
            error instanceof AsyncJobClientError
            && error.retryable
          ) {
            await wait(defaultPollMilliseconds);
            continue;
          }
          if (!(error instanceof AsyncJobClientError)) {
            await wait(defaultPollMilliseconds);
            continue;
          }
          throw error;
        }
        continue;
      }
      throw new AsyncJobClientError(
        current.errorCode ?? "analysis_failed",
        { retryable: current.retryable },
      );
    }
    if (current.status === "settling" && settlingRecoveryEnabled) {
      settlingObservedAt ??= now();
      if (now() - settlingObservedAt >= settlingRecoveryMilliseconds) {
        try {
          const remaining = deadline - now();
          if (remaining <= 0) {
            throw new AsyncJobClientError("poll_timeout");
          }
          const recovered = await options.recoverSettling!(remaining);
          if (recovered.jobId !== receipt.jobId) {
            throw new AsyncJobClientError("invalid_response");
          }
          settlingRecoveryEnabled = recovered.status === "settling";
          settlingObservedAt = settlingRecoveryEnabled ? now() : undefined;
        } catch (error) {
          if (
            !(error instanceof AsyncJobClientError)
            || !error.retryable
          ) {
            throw error;
          }
          settlingObservedAt = now();
          await wait(defaultPollMilliseconds);
        }
        continue;
      }
    } else if (current.status !== "settling") {
      settlingRecoveryEnabled = false;
      settlingObservedAt = undefined;
    }
    if (current.status === "running") {
      runningObservedAt ??= now();
      if (now() - runningObservedAt >= runningRecoveryMilliseconds) {
        try {
          const resumed = await runBeforeDeadline(
            (signal) => resumeJob(endpoint, receipt, fetchImpl, now, signal),
          );
          runningObservedAt = (
            resumed.status.status === "running" ? now() : undefined
          );
          await wait(resumed.retryAfterMilliseconds);
        } catch (error) {
          if (
            error instanceof AsyncJobClientError
            && (
              (
                error.httpStatus === 409
                && error.code === "job_conflict"
              )
              || error.retryable
            )
          ) {
            runningObservedAt = now();
            await wait(response.retryAfterMilliseconds);
            continue;
          }
          if (!(error instanceof AsyncJobClientError)) {
            runningObservedAt = now();
            await wait(response.retryAfterMilliseconds);
            continue;
          }
          throw error;
        }
        continue;
      }
    } else {
      runningObservedAt = undefined;
    }
    await wait(response.retryAfterMilliseconds);
  }
}

async function downloadResponseText(response: Response): Promise<string> {
  if (!response.ok) {
    throw new AsyncJobClientError(`download_http_${response.status}`, {
      httpStatus: response.status,
    });
  }
  return readBoundedText(response, MAX_REPORT_BYTES, "report_too_large");
}

export async function downloadAsyncReport(
  endpoint: string,
  receipt: AsyncJobReceipt,
  completed: AsyncJobStatus,
  fetchImpl: FetchImpl = globalThis.fetch,
  options: DownloadOptions = {},
): Promise<string> {
  const now = options.now ?? Date.now;
  parseReceipt(endpoint, receipt);
  let current = parseJobStatus(receipt.jobId, completed);
  let refreshed = false;

  if (
    current.status !== "succeeded"
    || current.downloadUrlExpiresAt === undefined
    || current.downloadUrlExpiresAt <= now()
  ) {
    current = (await getJobStatus(endpoint, receipt, fetchImpl, now)).status;
    refreshed = true;
  }
  if (current.status !== "succeeded" || !current.downloadUrl) {
    throw new AsyncJobClientError("report_not_ready");
  }

  let response = await safeFetch(fetchImpl, current.downloadUrl, {
    method: "GET",
    headers: { Accept: "text/markdown, text/plain;q=0.9" },
    redirect: "error",
  }, "download_network_error");
  if (
    !refreshed
    && (response.status === 401 || response.status === 403)
  ) {
    current = (await getJobStatus(endpoint, receipt, fetchImpl, now)).status;
    if (current.status !== "succeeded" || !current.downloadUrl) {
      throw new AsyncJobClientError("report_not_ready");
    }
    response = await safeFetch(fetchImpl, current.downloadUrl, {
      method: "GET",
      headers: { Accept: "text/markdown, text/plain;q=0.9" },
      redirect: "error",
    }, "download_network_error");
  }
  return downloadResponseText(response);
}
