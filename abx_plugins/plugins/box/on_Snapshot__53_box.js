#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
const fs = require("fs");
const path = require("path");
const {
  loadConfig,
  getEnvBool,
  getEnvInt,
  parseArgs,
  emitArchiveResultRecord,
} = require("../base/utils.js");
const {
  connectToPage,
  openExportPage,
  closeExportPage,
  waitForNavigationComplete,
  captureBrowserDownloads,
  resolveChromeLaunchOptions,
} = require("../chrome/chrome_utils.js");
const { saveDownloads } = require("../base/downloads.js");

async function main() {
  const config = loadConfig();
  const { url } = parseArgs();
  if (!url) throw new Error("Missing --url");
  if (!getEnvBool("BOX_ENABLED", true)) {
    console.error("BOX_ENABLED=False");
    return emitArchiveResultRecord("skipped", "BOX_ENABLED=False");
  }
  const timeoutMs = getEnvInt("BOX_TIMEOUT", 120) * 1000;
  const deadline = Date.now() + timeoutMs;
  const remaining = () => {
    const ms = deadline - Date.now();
    if (ms <= 0) throw new Error("Provider download deadline exceeded");
    return ms;
  };
  const snapshotDir = path.resolve(config.SNAP_DIR || ".");
  const chromeSessionDir = path.join(snapshotDir, "chrome");
  const { browser, page: sourcePage } = await connectToPage({
    chromeSessionDir,
    timeoutMs,
  });
  let page = sourcePage;
  let downloads = [];
  try {
    const candidate = (value) =>
      value.protocol === "https:" &&
      /(^|\.)box\.com$/.test(value.hostname) &&
      /^\/s\/[\w-]+/.test(value.pathname);
    if (!candidate(new URL(url)) && !candidate(new URL(page.url()))) {
      console.error("Not a Box share");
      return emitArchiveResultRecord("noresults", "Not a Box share");
    }
    await waitForNavigationComplete(chromeSessionDir, remaining());
    const current = new URL(page.url());
    if (
      candidate(new URL(url)) &&
      /(^|\.)box\.com$/.test(current.hostname) &&
      /^\/login\/?$/.test(current.pathname)
    ) {
      console.error("Persona must be logged in to box.com");
      return emitArchiveResultRecord(
        "skipped",
        "Persona must be logged in to box.com",
      );
    }
    if (!candidate(current)) {
      console.error("Not a Box share");
      return emitArchiveResultRecord("noresults", "Not a Box share");
    }
    // Export controls must never modify the shared capture tab.
    page = await openExportPage({
      page: sourcePage,
      chromeSessionDir: path.join(snapshotDir, "chrome"),
      timeoutMs: remaining(),
    });
    await page.goto(sourcePage.url(), {
      waitUntil: "domcontentloaded",
      timeout: remaining(),
    });
    const title = await page.title();
    let folder = false;
    downloads = await captureBrowserDownloads({
      browser,
      page,
      timeoutMs: remaining(),
      downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
      trigger: async () => {
        const button = await page.waitForSelector(
          'button[aria-label="Download"]',
          { visible: true, timeout: remaining() },
        );
        folder = await page.evaluate(
          () =>
            window.Box?.postStreamData?.["/app-api/enduserapp/shared-item"]
              ?.itemType === "folder",
        );
        await button.click();
      },
    });
    await saveDownloads(path.join(snapshotDir, "box"), title, downloads, {
      requireZip: folder,
      timeoutMs: remaining(),
    });
    emitArchiveResultRecord("succeeded", "box/downloads.json");
  } finally {
    if (page !== sourcePage)
      await closeExportPage({
        page,
        chromeSessionDir: path.join(snapshotDir, "chrome"),
      });
    for (const { filePath } of downloads)
      await fs.promises.unlink(filePath).catch((error) => {
        if (error.code !== "ENOENT") console.error(error.message);
      });
    await browser.disconnect();
  }
}
main().catch((error) => {
  console.error(error.message);
  emitArchiveResultRecord("failed", error.message);
  process.exitCode = 1;
});
