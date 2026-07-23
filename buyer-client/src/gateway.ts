/**
 * UOMP payload relay — buyer-side reverse gateway.
 *
 * Starts a local HTTP server that stores deliverable payloads in memory,
 * then exposes it publicly via a Cloudflare Tunnel (reverse tunnel).
 *
 * The seller uploads the report to the public tunnel URL and gets back a
 * payload_id. That URL is stored on-chain. The buyer (and anyone else with
 * the tunnel URL) can download the payload by ID — no direct seller→buyer
 * connection needed, and the buyer has no public IP requirement.
 *
 * Endpoints:
 *   POST /v1/payload/upload   Bearer <token>   → { payload_id }
 *   GET  /v1/payload/:id      Bearer <token>   → raw bytes
 *   HEAD /v1/payload/:id      Bearer <token>   → payload metadata
 *   GET  /v1/health           (no auth)        → { status }
 */

import {
  createServer,
  type IncomingMessage,
  type RequestListener,
  type ServerResponse,
} from "http";
import { spawn, type ChildProcess } from "child_process";
import { randomBytes, timingSafeEqual } from "crypto";

export const MAX_PAYLOAD_BYTES = 2 * 1024 * 1024;
export const MAX_RELAY_BYTES = 16 * 1024 * 1024;
export const MAX_RELAY_PAYLOADS = 32;

export interface RelayLimits {
  maxPayloadBytes: number;
  maxRelayBytes: number;
  maxPayloads: number;
}

export interface GatewayHandlerOptions {
  idFactory?: () => string;
  limits?: Partial<RelayLimits>;
}

export interface GatewayRelay {
  localUrl: string;    // http://127.0.0.1:PORT  (for buyer's own fetch)
  publicUrl: string;   // https://xxx.trycloudflare.com  (for seller to upload)
  token: string;       // Bearer token seller must include on upload
  close(): void;
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
  res: ServerResponse,
  status: number,
  code: string,
  message: string,
): void {
  if (res.headersSent || res.writableEnded || res.destroyed) return;
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
  const payloads = new Map<string, Buffer>();
  const idFactory = options.idFactory ?? (() => `pay_${randomBytes(16).toString("hex")}`);
  const limits: RelayLimits = {
    maxPayloadBytes: options.limits?.maxPayloadBytes ?? MAX_PAYLOAD_BYTES,
    maxRelayBytes: options.limits?.maxRelayBytes ?? MAX_RELAY_BYTES,
    maxPayloads: options.limits?.maxPayloads ?? MAX_RELAY_PAYLOADS,
  };
  let storedBytes = 0;
  let inFlightBytes = 0;
  let activeUploads = 0;

  return (
    req: IncomingMessage,
    res: ServerResponse,
  ): void => {
    const { method, url = "/" } = req;

    // ── Health (no auth) ─────────────────────────────────────────────────────
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
          sendUploadError(res, 400, "BAD_REQUEST", "Invalid Content-Length");
          drainRequest(req);
          return;
        }
        const declaredBigInt = BigInt(contentLengthHeader);
        if (declaredBigInt > BigInt(limits.maxPayloadBytes)) {
          sendUploadError(res, 413, "PAYLOAD_TOO_LARGE", "Payload exceeds byte limit");
          drainRequest(req);
          return;
        }
        declaredBytes = Number(declaredBigInt);
      }

      if (payloads.size + activeUploads >= limits.maxPayloads) {
        sendUploadError(res, 507, "INSUFFICIENT_STORAGE", "Payload slot limit reached");
        drainRequest(req);
        return;
      }
      if (declaredBytes > limits.maxRelayBytes - storedBytes - inFlightBytes) {
        sendUploadError(res, 507, "INSUFFICIENT_STORAGE", "Relay byte limit reached");
        drainRequest(req);
        return;
      }

      const chunks: Buffer[] = [];
      let receivedBytes = 0;
      let reservedBytes = declaredBytes;
      let terminal = false;
      let released = false;
      activeUploads += 1;
      inFlightBytes += declaredBytes;

      const release = (committedBytes?: number): void => {
        if (released) return;
        released = true;
        inFlightBytes -= reservedBytes;
        activeUploads -= 1;
        if (committedBytes !== undefined) {
          storedBytes += committedBytes;
        }
        chunks.length = 0;
      };

      const rejectUpload = (
        status: number,
        code: string,
        message: string,
      ): void => {
        if (terminal) return;
        terminal = true;
        release();
        sendUploadError(res, status, code, message);
        req.resume();
      };

      req.on("data", (chunk: Buffer) => {
        if (terminal) return;
        const nextReceivedBytes = receivedBytes + chunk.byteLength;
        if (nextReceivedBytes > limits.maxPayloadBytes) {
          rejectUpload(413, "PAYLOAD_TOO_LARGE", "Payload exceeds byte limit");
          return;
        }

        const additionalBytes = Math.max(0, nextReceivedBytes - reservedBytes);
        if (additionalBytes > limits.maxRelayBytes - storedBytes - inFlightBytes) {
          rejectUpload(507, "INSUFFICIENT_STORAGE", "Relay byte limit reached");
          return;
        }
        if (additionalBytes > 0) {
          reservedBytes += additionalBytes;
          inFlightBytes += additionalBytes;
        }
        receivedBytes = nextReceivedBytes;
        chunks.push(chunk);
      });
      req.on("end", () => {
        if (terminal) return;
        let id = idFactory();
        while (payloads.has(id)) {
          id = idFactory();
        }
        const data = Buffer.concat(chunks, receivedBytes);
        payloads.set(id, data);
        terminal = true;
        release(data.byteLength);
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ payload_id: id, size: data.byteLength }));
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

      const data = payloads.get(match[1]);
      if (!data) {
        sendNotFound(res);
        return;
      }
      res.writeHead(200, {
        "Content-Type": "application/octet-stream",
        "Content-Length": String(data.byteLength),
      });
      res.end(method === "HEAD" ? undefined : data);
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

function findCloudflared(): string {
  const candidates = [
    "cloudflared",
    `${process.env["HOME"]}/.local/bin/cloudflared`,
    "/usr/local/bin/cloudflared",
    "/opt/homebrew/bin/cloudflared",
  ];
  // Return first match — on PATH we can't stat, so just return the first name
  // and let spawn fail if not found.
  return candidates[0] ?? "cloudflared";
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
 * URL to pass to the seller, and a Bearer token the seller must send on
 * upload requests.
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
