/**
 * UOMP payload relay — buyer-side reverse gateway.
 *
 * Starts a local HTTP server that stores deliverable payloads in memory,
 * then exposes it publicly via a Cloudflare Tunnel (reverse tunnel).
 *
 * The seller uploads the report to the public tunnel URL and gets back a
 * payload_id. That URL is stored on-chain as a locator, while the signed
 * off-chain gateway token remains required for reads. No direct seller→buyer
 * connection or public buyer IP is needed.
 *
 * Endpoints:
 *   POST /v1/payload/upload   Bearer <token>   → { payload_id }
 *   GET  /v1/payload/:id      Bearer <token>   → raw bytes
 *   HEAD /v1/payload/:id      Bearer <token>   → payload metadata
 *   GET  /v1/health           (unauthenticated) → { status }
 */

import {
  createServer,
  type IncomingMessage,
  type RequestListener,
  type ServerResponse,
} from "http";
import { spawn, type ChildProcess } from "child_process";
import { randomBytes, timingSafeEqual } from "crypto";
import { accessSync, constants, statSync } from "fs";
import { delimiter, join } from "path";

export const MAX_PAYLOAD_BYTES = 2 * 1024 * 1024;
export const MAX_RELAY_BYTES = 16 * 1024 * 1024;
export const MAX_RELAY_PAYLOADS = 32;
const MAX_PAYLOAD_SEGMENTS = 32;

export interface RelayLimits {
  maxPayloadBytes: number;
  maxRelayBytes: number;
  maxPayloads: number;
}

export interface GatewayHandlerOptions {
  idFactory?: () => string;
  limits?: Partial<RelayLimits>;
  /** Optional storage telemetry used by focused relay tests. */
  onPayloadStored?: (observation: PayloadStorageObservation) => void;
}

export interface PayloadStorageObservation {
  allocatedBytes: number;
  byteLength: number;
  dataEvents: number;
  segmentCount: number;
}

export interface GatewayRelay {
  localUrl: string;    // http://127.0.0.1:PORT  (for buyer's own fetch)
  publicUrl: string;   // https://xxx.trycloudflare.com  (for seller to upload)
  token: string;       // Bearer token required for payload uploads and reads
  close(): void;
}

export interface CloudflaredDiscoveryOptions {
  env?: NodeJS.ProcessEnv;
  isExecutable?: (path: string) => boolean;
}

export function shouldUseBuyerRelay(deliveryMode: string | undefined): boolean {
  return deliveryMode?.trim().toLowerCase() !== "ipfs";
}

export async function fetchDeliverable(
  url: string,
  relay: GatewayRelay | undefined,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  if (!relay) return fetchImpl(url);
  const target = new URL(url);
  const relayOrigins = new Set([
    new URL(relay.publicUrl).origin,
    new URL(relay.localUrl).origin,
  ]);
  const isCanonicalPayload =
    !url.includes("?")
    && !url.includes("#")
    && target.href === url
    && !target.username
    && !target.password
    && !target.search
    && !target.hash
    && /^\/v1\/payload\/pay_[0-9a-f]{32}$/.test(target.pathname);
  if (!relayOrigins.has(target.origin) || !isCanonicalPayload) {
    return fetchImpl(url);
  }
  return fetchImpl(url, {
    headers: { Authorization: `Bearer ${relay.token}` },
    redirect: "error",
  });
}

function hasBearer(req: IncomingMessage, token: string): boolean {
  const header = req.headers.authorization;
  if (typeof header !== "string" || !header.startsWith("Bearer ")) return false;
  const supplied = Buffer.from(header.slice(7), "utf8");
  const expected = Buffer.from(token, "utf8");
  return supplied.length === expected.length && timingSafeEqual(supplied, expected);
}

function sendUnauthorized(res: ServerResponse): void {
  res.writeHead(401, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: { code: "UNAUTHORIZED", message: "Invalid or missing Bearer token" } }));
}

function sendNotFound(res: ServerResponse): void {
  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: { code: "NOT_FOUND", message: "Payload not found" } }));
}

function sendUploadError(
  req: IncomingMessage,
  res: ServerResponse,
  status: number,
  code: string,
  message: string,
): void {
  if (
    req.aborted
    || req.destroyed
    || req.socket.destroyed
    || res.headersSent
    || res.writableEnded
    || res.destroyed
  ) {
    return;
  }
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Connection": "close",
  });
  res.end(JSON.stringify({ error: { code, message } }));
}

function drainRequest(req: IncomingMessage): void {
  req.on("error", () => undefined);
  req.resume();
}

/**
 * Create an isolated handler for one relay. Its payload store intentionally
 * belongs to this closure so concurrently running relays cannot share data.
 */
export function createGatewayHandler(
  token: string,
  options: GatewayHandlerOptions = {},
): RequestListener {
  interface StoredPayload {
    allocatedBytes: number;
    byteLength: number;
    segments: Buffer[];
  }

  const payloads = new Map<string, StoredPayload>();
  const idFactory = options.idFactory ?? (() => `pay_${randomBytes(16).toString("hex")}`);
  const limits: RelayLimits = {
    maxPayloadBytes: options.limits?.maxPayloadBytes ?? MAX_PAYLOAD_BYTES,
    maxRelayBytes: options.limits?.maxRelayBytes ?? MAX_RELAY_BYTES,
    maxPayloads: options.limits?.maxPayloads ?? MAX_RELAY_PAYLOADS,
  };
  // The production 2 MiB limit yields 64 KiB pages. Smaller injected limits
  // scale the page size down while preserving the same 32-segment ceiling.
  const segmentBytes = Math.max(
    1,
    Math.ceil(limits.maxPayloadBytes / MAX_PAYLOAD_SEGMENTS),
  );
  let storedAllocatedBytes = 0;
  let inFlightAllocatedBytes = 0;
  let activeUploads = 0;

  return (
    req: IncomingMessage,
    res: ServerResponse,
  ): void => {
    const { method, url = "/" } = req;

    // ── Public health check ──────────────────────────────────────────────────
    if (method === "GET" && url === "/v1/health") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ status: "ok" }));
      return;
    }

    // ── Upload (requires Bearer token) ──────────────────────────────────────
    if (method === "POST" && url === "/v1/payload/upload") {
      if (!hasBearer(req, token)) {
        sendUnauthorized(res);
        drainRequest(req);
        return;
      }

      const contentLengthHeader = req.headers["content-length"];
      let declaredBytes = 0;
      if (contentLengthHeader !== undefined) {
        if (
          typeof contentLengthHeader !== "string"
          || !/^[0-9]+$/.test(contentLengthHeader)
        ) {
          sendUploadError(req, res, 400, "BAD_REQUEST", "Invalid Content-Length");
          drainRequest(req);
          return;
        }
        const declaredBigInt = BigInt(contentLengthHeader);
        if (declaredBigInt > BigInt(limits.maxPayloadBytes)) {
          sendUploadError(req, res, 413, "PAYLOAD_TOO_LARGE", "Payload exceeds byte limit");
          drainRequest(req);
          return;
        }
        declaredBytes = Number(declaredBigInt);
      }

      if (payloads.size + activeUploads >= limits.maxPayloads) {
        sendUploadError(req, res, 507, "INSUFFICIENT_STORAGE", "Payload slot limit reached");
        drainRequest(req);
        return;
      }

      const segments: Buffer[] = [];
      let receivedBytes = 0;
      let reservedAllocatedBytes = 0;
      let dataEvents = 0;
      let writeSegment = 0;
      let writeOffset = 0;
      let terminal = false;
      let released = false;
      activeUploads += 1;

      const release = (committed = false): void => {
        if (released) return;
        released = true;
        inFlightAllocatedBytes -= reservedAllocatedBytes;
        activeUploads -= 1;
        if (committed) {
          storedAllocatedBytes += reservedAllocatedBytes;
        } else {
          segments.length = 0;
        }
      };

      const rejectUpload = (
        status: number,
        code: string,
        message: string,
      ): void => {
        if (terminal) return;
        terminal = true;
        release();
        sendUploadError(req, res, status, code, message);
        req.resume();
      };

      const ensureRetainedCapacity = (targetBytes: number): boolean => {
        const targetAllocatedBytes = targetBytes === 0
          ? 0
          : Math.min(
            limits.maxPayloadBytes,
            Math.ceil(targetBytes / segmentBytes) * segmentBytes,
          );
        const additionalAllocatedBytes =
          targetAllocatedBytes - reservedAllocatedBytes;
        if (
          additionalAllocatedBytes
            > limits.maxRelayBytes
              - storedAllocatedBytes
              - inFlightAllocatedBytes
        ) {
          return false;
        }

        while (reservedAllocatedBytes < targetAllocatedBytes) {
          const nextSegmentBytes = Math.min(
            segmentBytes,
            limits.maxPayloadBytes - reservedAllocatedBytes,
          );
          if (nextSegmentBytes <= 0) return false;
          reservedAllocatedBytes += nextSegmentBytes;
          inFlightAllocatedBytes += nextSegmentBytes;
          try {
            segments.push(Buffer.allocUnsafeSlow(nextSegmentBytes));
          } catch {
            return false;
          }
        }
        return true;
      };

      if (!ensureRetainedCapacity(declaredBytes)) {
        rejectUpload(507, "INSUFFICIENT_STORAGE", "Relay byte limit reached");
        return;
      }

      req.on("data", (chunk: Buffer) => {
        if (terminal) return;
        dataEvents += 1;
        const nextReceivedBytes = receivedBytes + chunk.byteLength;
        if (nextReceivedBytes > limits.maxPayloadBytes) {
          rejectUpload(413, "PAYLOAD_TOO_LARGE", "Payload exceeds byte limit");
          return;
        }

        if (!ensureRetainedCapacity(nextReceivedBytes)) {
          rejectUpload(507, "INSUFFICIENT_STORAGE", "Relay byte limit reached");
          return;
        }

        let sourceOffset = 0;
        while (sourceOffset < chunk.byteLength) {
          const segment = segments[writeSegment];
          if (!segment) {
            rejectUpload(507, "INSUFFICIENT_STORAGE", "Relay byte limit reached");
            return;
          }
          const copiedBytes = Math.min(
            chunk.byteLength - sourceOffset,
            segment.byteLength - writeOffset,
          );
          chunk.copy(
            segment,
            writeOffset,
            sourceOffset,
            sourceOffset + copiedBytes,
          );
          sourceOffset += copiedBytes;
          writeOffset += copiedBytes;
          if (writeOffset === segment.byteLength) {
            writeSegment += 1;
            writeOffset = 0;
          }
        }
        receivedBytes = nextReceivedBytes;
      });
      req.on("end", () => {
        if (terminal) return;
        let id = idFactory();
        while (payloads.has(id)) {
          id = idFactory();
        }
        const payload: StoredPayload = {
          allocatedBytes: reservedAllocatedBytes,
          byteLength: receivedBytes,
          segments,
        };
        payloads.set(id, payload);
        terminal = true;
        release(true);
        options.onPayloadStored?.({
          allocatedBytes: payload.allocatedBytes,
          byteLength: payload.byteLength,
          dataEvents,
          segmentCount: payload.segments.length,
        });
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ payload_id: id, size: payload.byteLength }));
      });
      req.on("aborted", () => {
        rejectUpload(400, "BAD_REQUEST", "Upload aborted");
      });
      req.on("error", () => {
        rejectUpload(400, "BAD_REQUEST", "Upload failed");
      });
      req.on("close", () => {
        if (!terminal) {
          rejectUpload(400, "BAD_REQUEST", "Upload closed before completion");
        }
      });
      return;
    }

    // ── Download and existence checks (requires Bearer token) ──────────────
    if ((method === "GET" || method === "HEAD") && url.startsWith("/v1/payload/")) {
      if (!hasBearer(req, token)) {
        sendUnauthorized(res);
        return;
      }

      const match = url.match(/^\/v1\/payload\/(pay_[0-9a-f]{32})$/);
      if (!match) {
        sendNotFound(res);
        return;
      }

      const payload = payloads.get(match[1]);
      if (!payload) {
        sendNotFound(res);
        return;
      }
      res.writeHead(200, {
        "Content-Type": "application/octet-stream",
        "Content-Length": String(payload.byteLength),
      });
      if (method === "HEAD" || payload.byteLength === 0) {
        res.end();
        return;
      }
      let remainingBytes = payload.byteLength;
      for (const segment of payload.segments) {
        const bodyBytes = Math.min(remainingBytes, segment.byteLength);
        remainingBytes -= bodyBytes;
        const body = bodyBytes === segment.byteLength
          ? segment
          : segment.subarray(0, bodyBytes);
        if (remainingBytes === 0) {
          res.end(body);
          return;
        }
        res.write(body);
      }
      res.end();
      return;
    }

    res.writeHead(404);
    res.end();
  };
}

function createRelayServer(port: number, token: string): Promise<() => void> {
  return new Promise((resolve, reject) => {
    const server = createServer(createGatewayHandler(token));
    server.listen(port, "127.0.0.1", () => resolve(() => server.close()));
    server.on("error", reject);
  });
}

function defaultIsExecutable(path: string): boolean {
  try {
    if (!statSync(path).isFile()) return false;
    accessSync(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

export function findCloudflared(
  options: CloudflaredDiscoveryOptions = {},
): string {
  const env = options.env ?? process.env;
  const isExecutable = options.isExecutable ?? defaultIsExecutable;
  const pathCandidates = (env.PATH ?? "")
    .split(delimiter)
    .filter(Boolean)
    .map((directory) => join(directory, "cloudflared"));
  const candidates = [
    ...pathCandidates,
    ...(env.HOME ? [join(env.HOME, ".local", "bin", "cloudflared")] : []),
    "/usr/local/bin/cloudflared",
    "/opt/homebrew/bin/cloudflared",
  ];
  const match = candidates.find(isExecutable);
  if (!match) {
    throw new Error(
      "cloudflared executable not found in PATH, ~/.local/bin, /usr/local/bin, or /opt/homebrew/bin",
    );
  }
  return match;
}

function startCloudflaredTunnel(localPort: number): Promise<{ url: string; proc: ChildProcess }> {
  return new Promise((resolve, reject) => {
    const bin = findCloudflared();
    const proc = spawn(bin, ["tunnel", "--url", `http://127.0.0.1:${localPort}`], {
      stdio: ["ignore", "pipe", "pipe"],
    });

    let settled = false;

    const tryExtract = (text: string) => {
      const m = text.match(/https:\/\/[a-z0-9-]+\.trycloudflare\.com/);
      if (m && !settled) {
        settled = true;
        resolve({ url: m[0], proc });
      }
    };

    proc.stdout?.on("data", (d: Buffer) => tryExtract(d.toString()));
    proc.stderr?.on("data", (d: Buffer) => tryExtract(d.toString()));

    proc.on("error", (err: Error) => {
      if (!settled) { settled = true; reject(err); }
    });
    proc.on("exit", (code: number | null) => {
      if (!settled) { settled = true; reject(new Error(`cloudflared exited with code ${code}`)); }
    });

    setTimeout(() => {
      if (!settled) {
        settled = true;
        proc.kill();
        reject(new Error("cloudflared tunnel timed out (20s). Install: https://github.com/cloudflare/cloudflared/releases"));
      }
    }, 20_000);
  });
}

/**
 * Start the UOMP payload relay and (optionally) a Cloudflare Tunnel.
 *
 * Returns the local URL for the buyer's own fetch calls, the public tunnel
 * URL to pass to the seller, and a Bearer token required for payload uploads
 * and reads.
 */
export async function startGatewayRelay(port = 9444): Promise<GatewayRelay> {
  const token = `gw-${randomBytes(16).toString("hex")}`;
  const closeServer = await createRelayServer(port, token);
  const localUrl = `http://127.0.0.1:${port}`;

  console.log(`  Relay started at ${localUrl}`);
  console.log("  Starting Cloudflare Tunnel...");

  let publicUrl = localUrl;
  let tunnelProc: ChildProcess | undefined;

  try {
    const { url, proc } = await startCloudflaredTunnel(port);
    publicUrl = url;
    tunnelProc = proc;
    console.log(`  Tunnel URL: ${publicUrl}`);
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    console.log(`  ⚠  Cloudflare Tunnel unavailable: ${msg}`);
    console.log("  Falling back to local-only relay (seller must be on same machine).");
  }

  return {
    localUrl,
    publicUrl,
    token,
    close() {
      closeServer();
      tunnelProc?.kill();
    },
  };
}
