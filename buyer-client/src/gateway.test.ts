import assert from "node:assert/strict";
import {
  createServer,
  request as httpRequest,
  type ClientRequest,
  type Server,
} from "node:http";
import test from "node:test";
import {
  createGatewayHandler,
  fetchDeliverable,
  type GatewayHandlerOptions,
  type GatewayRelay,
} from "./gateway.js";

const TEST_TIMEOUT_MS = 1_000;

async function withRelay(
  token: string,
  run: (baseUrl: string, server: Server) => Promise<void>,
  options?: GatewayHandlerOptions,
): Promise<void> {
  const server = createServer(createGatewayHandler(token, options));
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  try {
    await run(`http://127.0.0.1:${address.port}`, server);
  } finally {
    await new Promise<void>((resolve) => {
      server.close(() => resolve());
      server.closeAllConnections();
    });
  }
}

function bounded<T>(promise: Promise<T>, label: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timeout = setTimeout(() => {
      reject(new Error(`Timed out waiting for ${label}`));
    }, TEST_TIMEOUT_MS);
    promise.then(
      (value) => {
        clearTimeout(timeout);
        resolve(value);
      },
      (error: unknown) => {
        clearTimeout(timeout);
        reject(error);
      },
    );
  });
}

async function postChunks(
  baseUrl: string,
  token: string,
  chunks: Buffer[],
  headers: Record<string, string> = {},
): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const target = new URL("/v1/payload/upload", baseUrl);
    let settled = false;
    const request = httpRequest(target, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, ...headers },
    }, (response) => {
      const parts: Buffer[] = [];
      response.on("data", (part: Buffer) => parts.push(part));
      response.on("end", () => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        resolve({
          status: response.statusCode ?? 0,
          body: Buffer.concat(parts).toString("utf8"),
        });
      });
    });
    const timeout = setTimeout(() => {
      if (settled) return;
      settled = true;
      request.destroy();
      reject(new Error("Timed out waiting for upload response"));
    }, TEST_TIMEOUT_MS);
    request.on("error", (error: Error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      reject(error);
    });
    for (const chunk of chunks) request.write(chunk);
    request.end();
  });
}

interface ObservedUpload {
  chunkReceived: Promise<void>;
  aborted: Promise<void>;
  errored: Promise<Error>;
  closed: Promise<void>;
}

function observeNextUpload(server: Server): ObservedUpload {
  let resolveChunk!: () => void;
  let resolveAborted!: () => void;
  let resolveError!: (error: Error) => void;
  let resolveClose!: () => void;
  const observed: ObservedUpload = {
    chunkReceived: new Promise((resolve) => { resolveChunk = resolve; }),
    aborted: new Promise((resolve) => { resolveAborted = resolve; }),
    errored: new Promise((resolve) => { resolveError = resolve; }),
    closed: new Promise((resolve) => { resolveClose = resolve; }),
  };
  server.once("request", (request) => {
    request.once("data", () => resolveChunk());
    request.once("aborted", () => resolveAborted());
    request.once("error", (error: Error) => resolveError(error));
    request.once("close", () => resolveClose());
  });
  return observed;
}

interface PartialUpload {
  request: ClientRequest;
  closed: Promise<void>;
  errored: Promise<Error>;
}

function beginPartialUpload(
  baseUrl: string,
  token: string,
  chunk: Buffer,
): PartialUpload {
  const target = new URL("/v1/payload/upload", baseUrl);
  const request = httpRequest(target, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  }, (response) => response.resume());
  const closed = new Promise<void>((resolve) => request.once("close", resolve));
  const errored = new Promise<Error>((resolve) => request.once("error", resolve));
  request.write(chunk);
  return { request, closed, errored };
}

async function upload(baseUrl: string, token: string, body: string): Promise<{
  payload_id: string;
  size: number;
}> {
  const response = await fetch(`${baseUrl}/v1/payload/upload`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body,
  });
  assert.equal(response.status, 200);
  return response.json() as Promise<{ payload_id: string; size: number }>;
}

test("requires bearer auth for upload, download, and existence checks", async () => {
  await withRelay("gw-secret", async (baseUrl) => {
    for (const [method, path] of [
      ["POST", "/v1/payload/upload"],
      ["GET", "/v1/payload/pay_missing"],
      ["HEAD", "/v1/payload/pay_missing"],
    ] as const) {
      const response = await fetch(`${baseUrl}${path}`, { method });
      assert.equal(response.status, 401);
    }
  });
});

test("stores under a 128-bit ID and serves authenticated GET and HEAD", async () => {
  await withRelay("gw-secret", async (baseUrl) => {
    const uploadResponse = await fetch(`${baseUrl}/v1/payload/upload`, {
      method: "POST",
      headers: { Authorization: "Bearer gw-secret" },
      body: "private report",
    });
    assert.equal(uploadResponse.status, 200);
    const result = await uploadResponse.json() as { payload_id: string; size: number };
    assert.match(result.payload_id, /^pay_[0-9a-f]{32}$/);
    assert.equal(result.size, 14);

    const path = `/v1/payload/${result.payload_id}`;
    const get = await fetch(`${baseUrl}${path}`, {
      headers: { Authorization: "Bearer gw-secret" },
    });
    assert.equal(await get.text(), "private report");

    const head = await fetch(`${baseUrl}${path}`, {
      method: "HEAD",
      headers: { Authorization: "Bearer gw-secret" },
    });
    assert.equal(head.status, 200);
    assert.equal(head.headers.get("content-length"), "14");
    assert.equal(await head.text(), "");
  });
});

test("keeps stores isolated between relay handlers", async () => {
  await withRelay("first-token", async (firstBaseUrl) => {
    const uploaded = await upload(firstBaseUrl, "first-token", "first relay only");
    await withRelay("second-token", async (secondBaseUrl) => {
      const response = await fetch(`${secondBaseUrl}/v1/payload/${uploaded.payload_id}`, {
        headers: { Authorization: "Bearer second-token" },
      });
      assert.equal(response.status, 404);
    });
  });
});

const smallLimits = {
  maxPayloadBytes: 8,
  maxRelayBytes: 12,
  maxPayloads: 2,
};

test("rejects a chunked upload that crosses the per-payload byte limit", async () => {
  await withRelay("gw-secret", async (baseUrl) => {
    const response = await postChunks(baseUrl, "gw-secret", [Buffer.alloc(9)]);
    assert.equal(response.status, 413);
    assert.doesNotMatch(response.body, /payload_id/);
  }, { limits: smallLimits });
});

test("rejects oversized and malformed Content-Length before reading the body", async () => {
  await withRelay("gw-secret", async (baseUrl) => {
    const oversized = await postChunks(baseUrl, "gw-secret", [], {
      "Content-Length": "9",
    });
    assert.equal(oversized.status, 413);
    assert.doesNotMatch(oversized.body, /payload_id/);

    const negative = await postChunks(baseUrl, "gw-secret", [], {
      "Content-Length": "-1",
    });
    assert.equal(negative.status, 400);
    assert.doesNotMatch(negative.body, /payload_id/);
  }, { limits: smallLimits });
});

test("preserves stored payloads when aggregate byte capacity rejects an upload", async () => {
  await withRelay("gw-secret", async (baseUrl) => {
    const original = await postChunks(baseUrl, "gw-secret", [Buffer.alloc(8, "a")]);
    assert.equal(original.status, 200);
    const { payload_id: payloadId } = JSON.parse(original.body) as {
      payload_id: string;
    };

    const rejected = await postChunks(baseUrl, "gw-secret", [Buffer.alloc(5)]);
    assert.equal(rejected.status, 507);
    assert.doesNotMatch(rejected.body, /payload_id/);

    const stored = await fetch(`${baseUrl}/v1/payload/${payloadId}`, {
      headers: { Authorization: "Bearer gw-secret" },
    });
    assert.equal(stored.status, 200);
    assert.equal(await stored.text(), "aaaaaaaa");
  }, { limits: smallLimits });
});

test("releases the active slot exactly once after an aborted upload", async () => {
  await withRelay("gw-secret", async (baseUrl, server) => {
    const lifecycle = observeNextUpload(server);
    const partial = beginPartialUpload(baseUrl, "gw-secret", Buffer.alloc(1));
    await bounded(lifecycle.chunkReceived, "server to receive partial upload");

    try {
      const rejected = await postChunks(baseUrl, "gw-secret", [Buffer.alloc(1)]);
      assert.equal(rejected.status, 507);
      assert.doesNotMatch(rejected.body, /payload_id/);
    } finally {
      partial.request.destroy();
      await bounded(Promise.all([
        partial.closed,
        lifecycle.aborted,
        lifecycle.closed,
      ]), "aborted upload cleanup");
    }

    const recovered = await postChunks(baseUrl, "gw-secret", [Buffer.alloc(1)]);
    assert.equal(recovered.status, 200);
    const stillFull = await postChunks(baseUrl, "gw-secret", [Buffer.alloc(1)]);
    assert.equal(stillFull.status, 507);
    assert.doesNotMatch(stillFull.body, /payload_id/);
  }, { limits: { ...smallLimits, maxPayloads: 1 } });
});

test("releases in-flight bytes exactly once after an upload stream error", async () => {
  await withRelay("gw-secret", async (baseUrl, server) => {
    const lifecycle = observeNextUpload(server);
    const partial = beginPartialUpload(baseUrl, "gw-secret", Buffer.alloc(8));
    await bounded(lifecycle.chunkReceived, "server to receive partial upload");

    try {
      const rejected = await postChunks(baseUrl, "gw-secret", [Buffer.alloc(5)]);
      assert.equal(rejected.status, 507);
      assert.doesNotMatch(rejected.body, /payload_id/);
    } finally {
      const forcedError = new Error("forced upload failure");
      partial.request.destroy(forcedError);
      assert.equal(await bounded(partial.errored, "client upload error"), forcedError);
      await bounded(Promise.all([
        partial.closed,
        lifecycle.errored,
        lifecycle.closed,
      ]), "errored upload cleanup");
    }

    const recovered = await postChunks(baseUrl, "gw-secret", [Buffer.alloc(5)]);
    assert.equal(recovered.status, 200);
    const stillBounded = await postChunks(baseUrl, "gw-secret", [Buffer.alloc(8)]);
    assert.equal(stillBounded.status, 507);
    assert.doesNotMatch(stillBounded.body, /payload_id/);
  }, { limits: smallLimits });
});

test("health response exposes no payload state", async () => {
  await withRelay("gw-secret", async (baseUrl) => {
    await upload(baseUrl, "gw-secret", "private report");
    const response = await fetch(`${baseUrl}/v1/health`);
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { status: "ok" });
  });
});

test("retries an ID collision without overwriting the stored payload", async () => {
  const existingId = `pay_${"a".repeat(32)}`;
  const freshId = `pay_${"b".repeat(32)}`;
  const ids = [existingId, existingId, freshId];
  let nextId = 0;

  await withRelay("gw-secret", async (baseUrl) => {
    const first = await upload(baseUrl, "gw-secret", "first payload");
    const second = await upload(baseUrl, "gw-secret", "second payload");
    assert.equal(first.payload_id, existingId);
    assert.equal(second.payload_id, freshId);

    const firstPayload = await fetch(`${baseUrl}/v1/payload/${existingId}`, {
      headers: { Authorization: "Bearer gw-secret" },
    });
    assert.equal(await firstPayload.text(), "first payload");
  }, {
    idFactory: () => ids[nextId++] ?? freshId,
  });
});

test("sends the relay token only to canonical payloads on exact relay origins", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const fakeFetch = async (
    url: string | URL | Request,
    init?: RequestInit,
  ): Promise<Response> => {
    calls.push({ url: String(url), init });
    return new Response("ok");
  };
  const relay: GatewayRelay = {
    localUrl: "http://127.0.0.1:9444",
    publicUrl: "https://buyer.trycloudflare.com",
    token: "gw-secret",
    close() {},
  };
  const payloadId = "pay_0123456789abcdef0123456789abcdef";

  for (const url of [
    `https://buyer.trycloudflare.com/v1/payload/${payloadId}`,
    `http://127.0.0.1:9444/v1/payload/${payloadId}`,
  ]) {
    await fetchDeliverable(url, relay, fakeFetch);
    const call = calls.at(-1);
    assert.equal(
      new Headers(call?.init?.headers).get("Authorization"),
      "Bearer gw-secret",
    );
    assert.equal(call?.init?.redirect, "error");
  }

  const untrustedUrls = [
    `https://evil.example/v1/payload/${payloadId}`,
    `https://buyer.trycloudflare.com.evil.example/v1/payload/${payloadId}`,
    `https://buyer.trycloudflare.com:8443/v1/payload/${payloadId}`,
    `http://127.0.0.1:9445/v1/payload/${payloadId}`,
    `https://user@buyer.trycloudflare.com/v1/payload/${payloadId}`,
    `https://user:password@buyer.trycloudflare.com/v1/payload/${payloadId}`,
    `https://buyer.trycloudflare.com/v1/payload/${payloadId}?download=1`,
    `https://buyer.trycloudflare.com/v1/payload/${payloadId}#report`,
    `https://buyer.trycloudflare.com/v1/payload/${payloadId}/extra`,
    `https://buyer.trycloudflare.com/v1/payload/${payloadId}.pdf`,
    `https://buyer.trycloudflare.com/v1/payload/pay_0123456789ABCDEF0123456789abcdef`,
    `https://buyer.trycloudflare.com/v1/payload/pay_0123456789abcdef`,
    `https://buyer.trycloudflare.com/v1/payload/pay_0123456789abcdef0123456789abcdeg`,
    "https://buyer.trycloudflare.com/v1/payload/upload",
    "https://buyer.trycloudflare.com/report",
  ];

  for (const url of untrustedUrls) {
    await fetchDeliverable(url, relay, fakeFetch);
    const call = calls.at(-1);
    assert.equal(
      new Headers(call?.init?.headers).has("Authorization"),
      false,
      url,
    );
  }
});

test("does not authenticate raw locators normalized by URL parsing", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const fakeFetch = async (
    url: string | URL | Request,
    init?: RequestInit,
  ): Promise<Response> => {
    calls.push({ url: String(url), init });
    return new Response("ok");
  };
  const relay: GatewayRelay = {
    localUrl: "http://127.0.0.1:9444",
    publicUrl: "https://buyer.trycloudflare.com",
    token: "gw-secret",
    close() {},
  };
  const payloadId = "pay_0123456789abcdef0123456789abcdef";
  const noncanonicalUrls = [
    `https://buyer.trycloudflare.com/v1/payload/ignored/../${payloadId}`,
    `https://buyer.trycloudflare.com/v1/payload/ignored/%2e%2e/${payloadId}`,
    `https://buyer.trycloudflare.com/v1/payload/ignored/%2E./${payloadId}`,
    `https://buyer.trycloudflare.com/v1/payload/ignored\\..\\${payloadId}`,
    `https://BUYER.TRYCLOUDFLARE.COM/v1/payload/${payloadId}`,
    `https://buyer.trycloudflare.com:443/v1/payload/${payloadId}`,
  ];

  for (const url of noncanonicalUrls) {
    const target = new URL(url);
    assert.notEqual(target.href, url);
    assert.equal(target.origin, "https://buyer.trycloudflare.com");
    assert.equal(target.pathname, `/v1/payload/${payloadId}`);
    await fetchDeliverable(url, relay, fakeFetch);
  }

  assert.deepEqual(
    calls.map((call) => new Headers(call.init?.headers).has("Authorization")),
    noncanonicalUrls.map(() => false),
  );
});

test("does not authenticate empty query or fragment delimiters", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const fakeFetch = async (
    url: string | URL | Request,
    init?: RequestInit,
  ): Promise<Response> => {
    calls.push({ url: String(url), init });
    return new Response("ok");
  };
  const relay: GatewayRelay = {
    localUrl: "http://127.0.0.1:9444",
    publicUrl: "https://buyer.trycloudflare.com",
    token: "gw-secret",
    close() {},
  };
  const canonicalUrl =
    "https://buyer.trycloudflare.com/v1/payload/"
    + "pay_0123456789abcdef0123456789abcdef";
  const delimitedUrls = [
    `${canonicalUrl}?`,
    `${canonicalUrl}#`,
    `${canonicalUrl}?#`,
  ];

  for (const url of delimitedUrls) {
    const target = new URL(url);
    assert.equal(target.href, url);
    assert.equal(target.search, "");
    assert.equal(target.hash, "");
    await fetchDeliverable(url, relay, fakeFetch);
  }

  assert.deepEqual(
    calls.map((call) => new Headers(call.init?.headers).has("Authorization")),
    delimitedUrls.map(() => false),
  );
});

test("fetches without credentials when no relay is available", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const fakeFetch = async (
    url: string | URL | Request,
    init?: RequestInit,
  ): Promise<Response> => {
    calls.push({ url: String(url), init });
    return new Response("ok");
  };
  const url = "https://storage.example/report";

  await fetchDeliverable(url, undefined, fakeFetch);

  assert.deepEqual(calls, [{ url, init: undefined }]);
});
