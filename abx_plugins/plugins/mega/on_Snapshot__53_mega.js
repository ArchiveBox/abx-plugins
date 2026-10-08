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
  if (!getEnvBool("MEGA_ENABLED", true)) {
    console.error("MEGA_ENABLED=False");
    return emitArchiveResultRecord("skipped", "MEGA_ENABLED=False");
  }
  const timeoutMs = getEnvInt("MEGA_TIMEOUT", 120) * 1000;
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
      candidate.hostname === "mega.nz" &&
      /^\/(file|folder)\/[A-Za-z0-9_-]+/.test(candidate.pathname) &&
      Boolean(candidate.hash);
    if (!isShare(new URL(url)) && !isShare(new URL(page.url()))) {
      console.error("Not a MEGA public file or folder share");
      return emitArchiveResultRecord(
        "noresults",
        "Not a MEGA public file or folder share",
      );
    }
    await waitForNavigationComplete(
      path.join(snapshotDir, "chrome"),
      remaining(),
    );
    const current = new URL(page.url());
    const share = current.pathname.match(/^\/(file|folder)\/[A-Za-z0-9_-]+/);
    if (!isShare(current)) {
      console.error("Not a MEGA public file or folder share");
      return emitArchiveResultRecord(
        "noresults",
        "Not a MEGA public file or folder share",
      );
    }
    const folder = share[1] === "folder";
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
    const click = async (selector) => {
      await (
        await page.waitForSelector(selector, {
          visible: true,
          timeout: remaining(),
        })
      ).click();
    };
    downloads = await captureBrowserDownloads({
      browser,
      page,
      timeoutMs: remaining(),
      downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
      trigger: async () => {
        if (folder) {
          // MEGA's toolbar and ZIP menu operate on the loaded public folder.
          await click(".fm-download");
          await click(".fm-download-dropdown button:has(.icon-download-zip)");
        } else {
          // These official webclient components call startDownload. MEGA owns
          // decryption and browser delivery; the hook never invokes app internals.
          await click(
            ".download-page .info-block:not(.hidden) .footer .icon-loading, .download-page .dl-header .actions .icon-loading",
          );
        }
      },
    });
    await saveDownloads(
      path.join(snapshotDir, "mega"),
      await page.title(),
      downloads,
      { requireZip: folder, timeoutMs: remaining() },
    );
    emitArchiveResultRecord("succeeded", "mega/downloads.json");
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
