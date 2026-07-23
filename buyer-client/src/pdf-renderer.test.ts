import assert from "node:assert/strict";
import test from "node:test";
import { renderPdf } from "./pdf-renderer.js";

test("renders with sandbox, JavaScript disabled, and every request aborted", async () => {
  const calls: string[] = [];
  let requestHandler: ((request: { abort(): Promise<void> }) => void) | undefined;
  let launchOptions: Record<string, unknown> | undefined;
  let aborted = false;

  const page = {
    async setJavaScriptEnabled(value: boolean) { calls.push(`js:${value}`); },
    async setRequestInterception(value: boolean) { calls.push(`intercept:${value}`); },
    on(event: string, handler: typeof requestHandler) {
      assert.equal(event, "request");
      requestHandler = handler;
    },
    async setContent(_html: string, options: { waitUntil: string }) {
      calls.push(`content:${options.waitUntil}`);
    },
    async pdf() { calls.push("pdf"); },
    async close() { calls.push("page-close"); },
  };
  const browser = {
    async newPage() { return page; },
    async close() { calls.push("browser-close"); },
  };
  const puppeteer = {
    async launch(options: Record<string, unknown>) {
      launchOptions = options;
      return browser;
    },
  };

  await renderPdf(puppeteer, "<p>safe</p>", "/tmp/report.pdf");
  requestHandler?.({ async abort() { aborted = true; } });
  await Promise.resolve();

  assert.deepEqual(launchOptions, { headless: true });
  assert.deepEqual(calls.slice(0, 3), [
    "js:false",
    "intercept:true",
    "content:domcontentloaded",
  ]);
  assert.equal(aborted, true);
  assert.deepEqual(calls.slice(-2), ["page-close", "browser-close"]);
});

test("closes page and browser when loading content fails", async () => {
  const calls: string[] = [];
  const page = {
    async setJavaScriptEnabled() {},
    async setRequestInterception() {},
    on() {},
    async setContent() { throw new Error("content failed"); },
    async pdf() { throw new Error("should not generate PDF"); },
    async close() { calls.push("page-close"); },
  };
  const browser = {
    async newPage() { return page; },
    async close() { calls.push("browser-close"); },
  };

  await assert.rejects(
    renderPdf({ async launch() { return browser; } }, "<p>safe</p>", "/tmp/report.pdf"),
    /content failed/,
  );
  assert.deepEqual(calls, ["page-close", "browser-close"]);
});

test("closes page and browser when generating a PDF fails", async () => {
  const calls: string[] = [];
  const page = {
    async setJavaScriptEnabled() {},
    async setRequestInterception() {},
    on() {},
    async setContent() {},
    async pdf() { throw new Error("PDF failed"); },
    async close() { calls.push("page-close"); },
  };
  const browser = {
    async newPage() { return page; },
    async close() { calls.push("browser-close"); },
  };

  await assert.rejects(
    renderPdf({ async launch() { return browser; } }, "<p>safe</p>", "/tmp/report.pdf"),
    /PDF failed/,
  );
  assert.deepEqual(calls, ["page-close", "browser-close"]);
});
