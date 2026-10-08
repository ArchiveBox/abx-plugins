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
  if (!getEnvBool("PROTONDRIVE_ENABLED", true)) {
    console.error("PROTONDRIVE_ENABLED=False");
    return emitArchiveResultRecord("skipped", "PROTONDRIVE_ENABLED=False");
  }
  const timeoutMs = getEnvInt("PROTONDRIVE_TIMEOUT", 120) * 1000;
  const deadline = Date.now() + timeoutMs;
  const remaining = () => {
    const ms = deadline - Date.now();
    if (ms <= 0) throw new Error("Provider download deadline exceeded");
    return ms;
  };
  const snapshotDir = path.resolve(config.SNAP_DIR || ".");
  const { browser, page: sourcePage } = await connectToPage({
    chromeSessionDir: path.join(snapshotDir, "chrome"),
    timeoutMs,
  });
  let page = sourcePage;
  let downloads = [];
  try {
    const isShare = (candidate) =>
      candidate.protocol === "https:" &&
      candidate.hostname === "drive.proton.me" &&
      /^\/urls\/[A-Za-z0-9]+/.test(candidate.pathname);
    if (!isShare(new URL(url)) && !isShare(new URL(page.url()))) {
      console.error("Not a Proton Drive public share");
      return emitArchiveResultRecord(
        "noresults",
        "Not a Proton Drive public share",
      );
    }
    await waitForNavigationComplete(
      path.join(snapshotDir, "chrome"),
      remaining(),
    );
    const current = new URL(page.url());
    if (!isShare(current)) {
      console.error("Not a Proton Drive public share");
      return emitArchiveResultRecord(
        "noresults",
        "Not a Proton Drive public share",
      );
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
    const button = await page.waitForSelector(
      '[data-testid="dropdown-download-button"]',
      { visible: true, timeout: remaining() },
    );
    const folder = await page.evaluate(() =>
      Boolean(document.querySelector('[role="application"] table')),
    );
    // The toolbar renders before the asynchronous child list. Its Download
    // action is a no-op until that list supplies the items to export.
    if (folder)
      await page.waitForSelector('[data-testid="item-a11y-activator"]', {
        timeout: remaining(),
      });
    const title = (await page.title()).replace(/ - Proton Drive$/, "");
    downloads = await captureBrowserDownloads({
      browser,
      page,
      timeoutMs: remaining(),
      downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
      trigger: async () => {
        await button.click();
        await page
          .locator(
            '[role="dialog"] ::-p-aria([name="Download"][role="button"])',
          )
          .setTimeout(remaining())
          .click();
      },
    });
    await saveDownloads(
      path.join(snapshotDir, "protondrive"),
      title,
      downloads,
      {
        requireZip: folder,
        timeoutMs: remaining(),
      },
    );
    emitArchiveResultRecord("succeeded", "protondrive/downloads.json");
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
