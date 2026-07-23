# Secure PDF Rendering Design

**Date:** 2026-07-23

## Problem

The buyer treats the seller's report as Markdown, but the current renderer
passes raw report and metadata strings into an HTML template. Chromium then
parses that HTML with JavaScript enabled, unrestricted network access, and the
process sandbox disabled. A seller-controlled report can therefore execute
scripts or event handlers, make outbound or internal-network requests, and
leave an active payload in the fallback HTML file.

The report, its metadata, and every deliverable fetched from a seller are
untrusted even when the job and delivery gateway are correctly authenticated.

## Security Invariants

1. Seller-controlled report text must never be interpreted as raw HTML.
2. Report metadata must never be able to create HTML elements or attributes.
3. PDF rendering must not execute JavaScript.
4. PDF rendering must not make network requests.
5. Chromium must run with its normal process sandbox.
6. Failure to launch a sandboxed Chromium process must never trigger an
   insecure fallback.
7. The saved HTML fallback must itself be inert when opened in a browser.
8. Existing Markdown presentation features—headings, emphasis, code, lists,
   blockquotes, tables, recommendation badges, and percentage colouring—must
   remain available without browser-side script.

## Design

### Static Markdown conversion

Add one `escapeHtml` boundary that encodes `&`, `<`, `>`, `"`, and `'`.
`mdToHtml` applies this boundary to the complete report before performing any
Markdown transformations. The converter therefore operates only on inert text
and its own generated tags. Raw HTML, inline event handlers, scripts, iframes,
images, styles, links, and malformed closing tags are rendered as visible text,
not browser markup.

The Markdown converter remains deliberately small and does not add support for
raw HTML or links. No sanitizer dependency or allowlist parser is introduced.

### Metadata isolation

`buildHtml` escapes every metadata field independently at the insertion
boundary. This includes `jobId`, `date`, and `symbols`, even when a current
caller normally supplies a numeric job ID or locally sourced date.

### Script-free presentation

Remove the template's inline post-processing script and all `innerHTML`
rewrites. Recommendation badges and signed percentage colouring are produced
by the trusted Markdown conversion code after report text has been escaped.
The completed document contains no executable script.

### Hardened Chromium session

Launch Puppeteer without `--no-sandbox` or `--disable-setuid-sandbox`. Before
loading content:

- disable JavaScript on the page;
- enable request interception;
- abort every request;
- use `domcontentloaded` rather than `networkidle0`.

The template has no external dependencies, so a legitimate render requires no
network traffic. Request blocking is defense in depth against future template
changes and Chromium parsing quirks.

The browser and page are closed in `finally` blocks. If Puppeteer is missing,
Chrome cannot launch with its sandbox, content loading fails, or PDF generation
fails, `saveReport` returns `pdfPath: null` and retains the already-sanitized
HTML fallback. It never retries with unsafe browser flags.

## Interfaces

The public interface remains:

```ts
saveReport(
  reportText: string,
  jobId: string,
  symbols: string[],
): Promise<{ pdfPath: string | null; htmlPath: string }>
```

Test seams may be added for deterministic HTML generation and an injected
Puppeteer-like launcher. These seams must not expose unsafe renderer options to
production callers.

## Testing

Regression tests must first demonstrate the current vulnerability, then verify:

- report payloads containing `script`, event attributes, images, iframes,
  styles, links, and closing-tag injection are encoded as text;
- malicious metadata cannot close its containing element;
- the generated HTML contains no executable script or `innerHTML` rewrite;
- ordinary Markdown and recommendation/percentage styling still render;
- Puppeteer receives no sandbox-disabling flags;
- JavaScript is disabled before `setContent`;
- request interception is enabled and every request is aborted;
- `setContent` waits for `domcontentloaded`, not network idle;
- browser resources close on success and failure;
- a sandboxed launch failure returns `pdfPath: null` while leaving only inert
  fallback HTML.

Tests must not launch a real browser, contact the network, access wallet
material, or call a live chain.

## Scope

This change is limited to buyer-side HTML/PDF report generation and its tests.
It does not change seller execution, gateway authentication, blockchain
contracts, pricing, settlement, wallet handling, or deployment policy.
