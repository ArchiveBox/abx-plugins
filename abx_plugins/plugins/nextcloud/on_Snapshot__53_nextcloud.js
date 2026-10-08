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
  if (!getEnvBool("NEXTCLOUD_ENABLED", true)) {
    console.error("NEXTCLOUD_ENABLED=False");
    return emitArchiveResultRecord("skipped", "NEXTCLOUD_ENABLED=False");
  }
  const timeoutMs = getEnvInt("NEXTCLOUD_TIMEOUT", 120) * 1000;
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
      /^https?:$/.test(candidate.protocol) &&
      /\/s\/[\w-]+\/?$/.test(candidate.pathname);
    if (!isShare(new URL(url)) && !isShare(new URL(page.url()))) {
      console.error("Not a Nextcloud or ownCloud share");
      return emitArchiveResultRecord(
        "noresults",
        "Not a Nextcloud or ownCloud share",
      );
    }
    try {
      await waitForNavigationComplete(
        path.join(snapshotDir, "chrome"),
        remaining(),
      );
    } catch (error) {
      // A failed navigation cannot establish that an arbitrary host runs
      // Nextcloud. Suppress only the navigation hook's published failure.
      let navigation;
      try {
        navigation = JSON.parse(
          fs.readFileSync(
            path.join(snapshotDir, "chrome", "navigation.json"),
            "utf8",
          ),
        );
      } catch {
        throw error;
      }
      if (!navigation.error || navigation.error !== error.message) throw error;
      console.error("No Nextcloud or ownCloud page loaded");
      return emitArchiveResultRecord(
        "noresults",
        "No Nextcloud or ownCloud page loaded",
      );
    }
    const current = new URL(page.url());
    if (!isShare(current)) {
      console.error("Not a Nextcloud or ownCloud share");
      return emitArchiveResultRecord(
        "noresults",
        "Not a Nextcloud or ownCloud share",
      );
    }
    // Self-hosted domains cannot be recognized by hostname. Read the settled
    // page's provider markers without making another request or waiting.
    const provider = await page.evaluate(() =>
      Boolean(
        document.querySelector(
          "#initial-state-files_sharing-sharingToken, #sharingToken",
        ) && document.querySelector("#body-public, body#body-public"),
      ),
    );
    if (!provider) {
      console.error("Not a Nextcloud or ownCloud share");
      return emitArchiveResultRecord(
        "noresults",
        "Not a Nextcloud or ownCloud share",
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
    const selector = "#public-page-menu--primary, #download";
    const button = await page.waitForSelector(selector, {
      visible: true,
      timeout: remaining(),
    });
    const folder = await button.evaluate(
      (el) =>
        /[?&]accept=zip(?:&|$)/.test(el.getAttribute("href") || "") ||
        document.querySelector("#mimetype")?.value === "httpd/unix-directory" ||
        Boolean(
          document.querySelector(
            '#filestable, table[aria-label^="Publicly shared files"]',
          ),
        ),
    );
    const title = await page.title();
    downloads = await captureBrowserDownloads({
      browser,
      page,
      timeoutMs: remaining(),
      downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
      trigger: async () => {
        await button.click();
      },
    });
    await saveDownloads(path.join(snapshotDir, "nextcloud"), title, downloads, {
      requireZip: folder,
      timeoutMs: remaining(),
    });
    emitArchiveResultRecord("succeeded", "nextcloud/downloads.json");
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
