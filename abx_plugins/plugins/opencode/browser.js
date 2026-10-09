// Read and probe the Chrome plugin's existing session; never launch a browser.
const { connectToPage, connectToBrowserEndpoint, waitForChromeSessionState,
  resolvePuppeteerModule } = require("../chrome/chrome_utils.js");

(async () => {
  const puppeteer = resolvePuppeteerModule();
  const chromeSessionDir = process.argv[2];
  const requireTargetId = process.argv[3] === "snapshot";
  let browser;
  try {
    const state = requireTargetId
      ? await connectToPage({ chromeSessionDir, puppeteer, requireTargetId: true,
          requireBrowserReady: true, timeoutMs: 5000, missingTargetGraceMs: 0 })
      : await waitForChromeSessionState(chromeSessionDir, {
          requireBrowserReady: true, requireConnectable: true, puppeteer, timeoutMs: 5000,
        });
    if (!state) throw new Error("No live Chrome session; start the requested capture first");
    browser = state.browser?.wsEndpoint ? state.browser
      : await connectToBrowserEndpoint(puppeteer, state.cdpUrl, { defaultViewport: null });
    const client = await browser.target().createCDPSession();
    await client.send("Browser.getVersion");
    const { targetInfos } = await client.send("Target.getTargets");
    const targetId = process.argv[4] || state.targetId;
    const target = targetId ? targetInfos.find(target => target.targetId === targetId && target.type === 'page') : null;
    if (targetId && !target) throw new Error("Selected target has closed; select a live tab");
    console.log(JSON.stringify({ cdp_url: browser.wsEndpoint(), target_id: targetId || null,
      browser_context_id: target?.browserContextId || null }));
  } finally { if (browser) await browser.disconnect(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
