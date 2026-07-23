import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import { createGatewayHandler, type GatewayHandlerOptions } from "./gateway.js";

async function withRelay(
  token: string,
  run: (baseUrl: string) => Promise<void>,
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
    await run(`http://127.0.0.1:${address.port}`);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
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
