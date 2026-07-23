import {
  verifyDeliverableManifest,
  type DeliverableExpectation,
} from "./deliverable.js";
import { MAX_PAYLOAD_BYTES } from "./gateway.js";

export interface CompletionParams {
  jobId: bigint;
  fundTxBlock?: number;
  chainId: bigint;
  contracts: DeliverableExpectation["contracts"];
}

export interface CompletionDependencies {
  getDeliverableUrl(jobId: bigint, fundTxBlock?: number): Promise<unknown>;
  getDeliverableCommitment(jobId: bigint): Promise<string>;
  fetchDeliverable(url: string): Promise<Response>;
  renderReport(reportText: string): Promise<void>;
  settle(jobId: bigint): Promise<string>;
}

export interface CompletionResult {
  reportText: string;
  settleTx: string;
  renderError?: string;
}

export class SettlementAttemptError extends Error {
  constructor(cause: unknown) {
    const message = cause instanceof Error ? cause.message : String(cause);
    super(message, { cause });
    this.name = "SettlementAttemptError";
  }
}

export class SettlementBlockedError extends Error {
  constructor(reason: string) {
    super(`Settlement blocked: ${reason}`);
    this.name = "SettlementBlockedError";
  }
}

function blocked(reason: string): SettlementBlockedError {
  return new SettlementBlockedError(reason);
}

async function readBoundedBody(response: Response): Promise<string> {
  const declared = response.headers.get("content-length");
  if (declared !== null && (!/^[0-9]+$/.test(declared) || BigInt(declared) > BigInt(MAX_PAYLOAD_BYTES))) {
    throw blocked("deliverable exceeds the 2 MiB limit");
  }
  if (!response.body) return "";

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_PAYLOAD_BYTES) {
        await reader.cancel();
        throw blocked("deliverable exceeds the 2 MiB limit");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  return Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)), size).toString("utf8");
}

export async function completeSubmittedJob(
  params: CompletionParams,
  dependencies: CompletionDependencies,
): Promise<CompletionResult> {
  let url: unknown;
  try {
    url = await dependencies.getDeliverableUrl(params.jobId, params.fundTxBlock);
  } catch {
    throw blocked("deliverable URL could not be read");
  }
  if (url === null || url === undefined || url === "") {
    throw blocked("deliverable URL is missing");
  }
  if (typeof url !== "string") {
    throw blocked("deliverable URL must be a string");
  }

  let parsedUrl: URL;
  try {
    parsedUrl = new URL(url);
  } catch {
    throw blocked("deliverable URL is malformed");
  }
  if (parsedUrl.protocol !== "http:" && parsedUrl.protocol !== "https:") {
    throw blocked("deliverable URL must use HTTP or HTTPS");
  }

  let response: Response;
  try {
    response = await dependencies.fetchDeliverable(url);
  } catch {
    throw blocked("deliverable download failed");
  }
  if (!response.ok) {
    throw blocked(`deliverable download returned HTTP ${response.status}`);
  }

  let rawText: string;
  let commitment: string;
  try {
    [rawText, commitment] = await Promise.all([
      readBoundedBody(response),
      dependencies.getDeliverableCommitment(params.jobId),
    ]);
  } catch (error) {
    if (error instanceof SettlementBlockedError) throw error;
    throw blocked("deliverable body or on-chain commitment could not be read");
  }

  let reportText: string;
  try {
    reportText = verifyDeliverableManifest(rawText, {
      jobId: params.jobId,
      chainId: params.chainId,
      contracts: params.contracts,
      commitment,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "verification failed";
    throw blocked(message);
  }

  let renderError: string | undefined;
  try {
    await dependencies.renderReport(reportText);
  } catch (error) {
    renderError = error instanceof Error ? error.message : String(error);
  }

  let settleTx: string;
  try {
    settleTx = await dependencies.settle(params.jobId);
  } catch (error) {
    throw new SettlementAttemptError(error);
  }
  return { reportText, settleTx, ...(renderError ? { renderError } : {}) };
}
