// Read the real provider state before the hook closes its owned export tab.
// Never record request bodies or signed query strings, or change download policy.
const chrome = require(process.argv[2]);
let browser;
let stopped = false;
const record = (value) => console.log(JSON.stringify({ time: new Date().toISOString(), ...value }));
const address = (raw) => {
  try {
    const url = new URL(raw);
    return url.origin + url.pathname;
  } catch { return ""; }
};
process.on("SIGTERM", () => {
  stopped = true;
  if (browser) browser.disconnect();
  process.exit(0);
});
(async () => {
  browser = await chrome.connectToBrowserEndpoint(
    chrome.resolvePuppeteerModule(), process.argv[3],
    { defaultViewport: null, protocolTimeout: 1000 },
  );
  const observed = new Set();
  const observe = (page) => {
    if (observed.has(page)) return;
    observed.add(page);
    const id = observed.size;
    page.on("response", (response) => {
      if (["xhr", "fetch", "document"].includes(response.request().resourceType()))
        record({ phase: "response", page: id, url: address(response.url()), status: response.status() });
    });
    page.on("requestfailed", (request) => record({
      phase: "request_failed", page: id, url: address(request.url()),
      error: request.failure()?.errorText,
    }));
    page.on("pageerror", (error) => record({ phase: "page_error", page: id, name: error.name }));
  };
  browser.on("targetcreated", (target) => target.page().then((page) => page && observe(page)).catch(() => {}));
  while (!stopped && browser.connected) {
    for (const page of await browser.pages()) {
      observe(page);
      if (!page.url().startsWith("https://drive.google.com/")) continue;
      try {
        const state = await page.evaluate(() => ({
          focus: document.hasFocus(), visibility: document.visibilityState,
          selectedRows: document.querySelectorAll('[role="row"][aria-selected="true"]').length,
          cancelDownload: [...document.querySelectorAll('[aria-label="Cancel download"]')]
            .map((element) => ({ visible: !!element.getClientRects().length })),
          status: [...document.querySelectorAll('[role="status"], [role="alert"], [role="dialog"]')]
            .filter((element) => element.getClientRects().length)
            .map((element) => element.innerText.slice(0, 1000)),
        }));
        record({ phase: "zip_ui", page: [...observed].indexOf(page) + 1, state });
      } catch (error) { record({ phase: "observation_error", name: error.name }); }
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
})().catch((error) => record({ phase: "observer_error", name: error.name }))
  .finally(() => { if (browser) browser.disconnect(); });
