import assert from "node:assert/strict";
import test from "node:test";
import { renderPdf, type PageLike } from "./pdf-renderer.js";

const lifecycle: Parameters<PageLike["setContent"]>[1]["waitUntil"] = "domcontentloaded";
// @ts-expect-error PDF rendering permits only the DOM content lifecycle event.
const unsafeLifecycle: Parameters<PageLike["setContent"]>[1]["waitUntil"] = "networkidle0";
void lifecycle;
void unsafeLifecycle;

test("renders with sandbox, JavaScript disabled, and every request aborted during content loading", async () => {
  const calls: string[] = [];
  let requestHandler: ((request: { abort(): Promise<void> }) => void) | undefined;
  let launchOptions: Record<string, unknown> | undefined;
  let pdfOptions: Parameters<PageLike["pdf"]>[0] | undefined;

  const setContent: PageLike["setContent"] = async (
    html: string,
    options: { waitUntil: "domcontentloaded" },
  ) => {
    calls.push(`content:${options.waitUntil}:${html}`);
    assert.ok(requestHandler, "the request handler must be registered before content loads");
    requestHandler({
      async abort() {
        calls.push("request-abort");
      },
    });
    await Promise.resolve();
  };
  const page: PageLike = {
    async setJavaScriptEnabled(value: boolean) { calls.push(`js:${value}`); },
    async setRequestInterception(value: boolean) { calls.push(`intercept:${value}`); },
    on(event: string, handler: typeof requestHandler) {
      assert.equal(event, "request");
      calls.push(`on:${event}`);
      requestHandler = handler;
    },
    setContent,
    async pdf(options) {
      calls.push("pdf");
      pdfOptions = options;
    },
    async close() { calls.push("page-close"); },
  };
  const browser = {
    async newPage() {
      calls.push("new-page");
      return page;
    },
    async close() { calls.push("browser-close"); },
  };
  const puppeteer = {
    async launch(options: Record<string, unknown>) {
      calls.push("launch");
      launchOptions = options;
      return browser;
    },
  };

  await renderPdf(puppeteer, "<p>safe</p>", "/tmp/report.pdf");

  assert.deepEqual(launchOptions, { headless: true });
  assert.deepEqual(calls, [
    "launch",
    "new-page",
    "js:false",
    "intercept:true",
    "on:request",
    "content:domcontentloaded:<p>safe</p>",
    "request-abort",
    "pdf",
    "page-close",
    "browser-close",
  ]);
  assert.deepEqual(pdfOptions, {
    path: "/tmp/report.pdf",
    format: "A4",
    printBackground: true,
    margin: { top: "0", right: "0", bottom: "0", left: "0" },
  });
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
