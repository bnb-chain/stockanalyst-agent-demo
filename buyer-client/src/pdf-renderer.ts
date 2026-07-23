export interface RequestLike {
  abort(): Promise<void>;
}

export interface PageLike {
  setJavaScriptEnabled(value: boolean): Promise<void>;
  setRequestInterception(value: boolean): Promise<void>;
  on(event: "request", handler: (request: RequestLike) => void): void;
  setContent(html: string, options: { waitUntil: "domcontentloaded" }): Promise<void>;
  pdf(options: {
    path: string;
    format: string;
    printBackground: boolean;
    margin: { top: string; right: string; bottom: string; left: string };
  }): Promise<unknown>;
  close(): Promise<void>;
}

export interface BrowserLike {
  newPage(): Promise<PageLike>;
  close(): Promise<void>;
}

export interface PuppeteerLike {
  launch(options: Record<string, unknown>): Promise<BrowserLike>;
}

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
