const {
  connectToBrowserEndpoint,
  findExtensionMetadataByName,
  resolvePuppeteerModule,
  waitForChromeSessionState,
} = require("../chrome/chrome_utils.js");

async function disableStrictBlocking(chromeSessionDir, timeoutMs = 30000) {
  const chromeSession = await waitForChromeSessionState(chromeSessionDir, {
    timeoutMs,
    requireBrowserReady: true,
  });
  if (!chromeSession?.cdpUrl) {
    throw new Error(`Chrome session is not ready in ${chromeSessionDir}`);
  }

  const extension = findExtensionMetadataByName(
    chromeSession.extensions || [],
    "ublock",
  );
  if (!extension?.id) {
    throw new Error("uBlock extension ID is missing from browser.json");
  }

  const browser = await connectToBrowserEndpoint(
    resolvePuppeteerModule(),
    chromeSession.cdpUrl,
    { defaultViewport: null },
  );
  let settingsPage = null;
  try {
    settingsPage = await browser.newPage();
    await settingsPage.goto(
      `chrome-extension://${extension.id}/dashboard.html`,
      { waitUntil: "domcontentloaded", timeout: Math.min(timeoutMs, 10000) },
    );

    const configured = await settingsPage.evaluate(async () => {
      // Use the same public message consumed by uBlock's settings checkbox.
      // This updates both the live DNR session rules and persisted config;
      // writing chrome.storage alone would leave the running worker unchanged.
      await chrome.runtime.sendMessage({
        what: "setStrictBlockMode",
        state: false,
      });
      const { rulesetConfig } = await chrome.storage.local.get("rulesetConfig");
      return rulesetConfig?.strictBlockMode === false;
    });
    if (!configured) {
      throw new Error("uBlock did not persist strictBlockMode=false");
    }
  } finally {
    if (settingsPage) {
      await settingsPage.close({ runBeforeUnload: false }).catch(() => {});
    }
    await browser.disconnect();
  }
}

module.exports = { disableStrictBlocking };
