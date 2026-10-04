#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
const path = require("path");
const fs = require("fs");
const {
  loadConfig,
  getEnvBool,
  getEnvInt,
  parseArgs,
  emitArchiveResultRecord,
} = require("../base/utils.js");
const {
  connectToPage,
  captureBrowserDownloads,
  resolveChromeLaunchOptions,
} = require("../chrome/chrome_utils.js");
const { saveDownloads } = require("../base/downloads.js");

function sharePath(value) {
  const url = new URL(value);
  if (
    url.protocol !== "https:" ||
    !["www.dropbox.com", "dropbox.com"].includes(url.host) ||
    url.username ||
    url.password
  )
    return null;
  if (!/^\/(?:scl\/(?:fi|fo)|s|sh)\/[\w-]+\//.test(url.pathname)) return null;
  const parts = url.pathname.split("/").filter(Boolean);
  // Dropbox canonicalizes ?preview=FILE into a child URL with another secure
  // hash. Compare the shared root and exact relative path, not that hash.
  const modernFolder = parts[0] === "scl" && parts[1] === "fo";
  if (modernFolder || parts[0] === "sh") {
    const root = modernFolder ? `fo:${parts[2]}` : `sh:${parts[1]}`;
    const child = parts.slice(modernFolder ? 4 : 3).map(decodeURIComponent);
    if (url.searchParams.has("preview"))
      child.push(url.searchParams.get("preview"));
    return `${root}/${child.join("/")}`;
  }
  return url.pathname;
}

async function main() {
  const config = loadConfig();
  const { url } = parseArgs();
  if (!getEnvBool("DROPBOX_ENABLED", true))
    return emitArchiveResultRecord("skipped", "DROPBOX_ENABLED=False");
  if (!url) throw new Error("Missing --url");
  const original = sharePath(url);
  if (!original)
    return emitArchiveResultRecord("noresults", "Not a Dropbox share URL");
  if (["dl", "raw"].some((key) => new URL(url).searchParams.get(key) === "1"))
    return emitArchiveResultRecord(
      "noresults",
      "Direct Dropbox downloads are captured by staticfile"
    );
  const timeoutMs = getEnvInt("DROPBOX_TIMEOUT", 120) * 1000;
  const deadline = Date.now() + timeoutMs;
  const remaining = () => {
    const ms = deadline - Date.now();
    if (ms <= 0) throw new Error("Provider download deadline exceeded");
    return ms;
  };
  const snapshotDir = path.resolve(config.SNAP_DIR || ".");
  const { browser, page } = await connectToPage({
    chromeSessionDir: path.join(snapshotDir, "chrome"),
    timeoutMs,
    waitForNavigationComplete: true,
  });
  let downloads = [];
  try {
    if (sharePath(page.url()) !== original)
      throw new Error(
        "Chrome tab is not on the requested Dropbox share (login may be required)"
      );
    let folderDownload = false;
    downloads = await captureBrowserDownloads({
      browser,
      page,
      timeoutMs: remaining(),
      downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
      trigger: async ({ downloadStarted }) => {
        // This action downloads the current share, not any individual child item.
        await page.waitForSelector('[data-testid="action-bar-download-button"], #fvsdk-mount-point button[aria-label="Download"]', {timeout: remaining()});
        folderDownload = await page.$('[data-testid="action-bar-download-button"]') !== null;
        await page
          .locator(
            '[data-testid="action-bar-download-button"], #fvsdk-mount-point button[aria-label="Download"]'
          )
          .setTimeout(remaining())
          .click();
        console.error("Opened Dropbox Download action");
        const continueButton = await Promise.race([
          downloadStarted.then(() => null),
          page.waitForSelector(
            ":is(#folder-preview-modal, #shared-link-download-signup-modal) .dig-Modal-footer button",
            { timeout: remaining() }
          ),
        ]);
        if (continueButton) {
          // The dialog animates after it enters the DOM. Wait for a stable,
          // visible control before clicking, including after infiniscroll.
          await page
            .locator(
              ":is(#folder-preview-modal, #shared-link-download-signup-modal) .dig-Modal-footer button"
            )
            .setTimeout(remaining())
            .click();
        }
        console.error("Dropbox is preparing the download");
      },
    });
    await saveDownloads(
      path.join(snapshotDir, "dropbox"),
      await page.title(),
      downloads,
      { requireZip: folderDownload, timeoutMs: remaining() }
    );
    emitArchiveResultRecord("succeeded", "dropbox/downloads.json");
  } finally {
    // These paths were validated and claimed by this tab, even if unpacking
    // or the remaining deadline failed after Chrome completed its download.
    for (const { filePath } of downloads) {
      await fs.promises.unlink(filePath).catch((error) => {
        if (error.code !== "ENOENT") console.error(`Cannot remove provider download: ${error.message}`);
      });
    }
    await browser.disconnect();
  }
}
main().catch((error) => {
  console.error(error.message);
  emitArchiveResultRecord("failed", error.message);
  process.exitCode = 1;
});
