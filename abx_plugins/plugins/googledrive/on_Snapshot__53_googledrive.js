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

function folderId(value) {
  const url = new URL(value);
  if (
    url.protocol !== "https:" ||
    !["drive.google.com", "www.drive.google.com"].includes(url.host) ||
    url.username ||
    url.password
  )
    return null;
  const id =
    url.pathname.match(
      /^\/drive\/(?:u\/\d+\/)?folders\/([\w-]+)\/?$/
    )?.[1] ||
    (/^\/(?:u\/\d+\/)?folderview$/.test(url.pathname) ? url.searchParams.get("id") : null);
  return id && /^[\w-]+$/.test(id) ? id : null;
}

async function main() {
  const config = loadConfig();
  const { url } = parseArgs();
  if (!getEnvBool("GOOGLEDRIVE_ENABLED", true))
    return emitArchiveResultRecord("skipped", "GOOGLEDRIVE_ENABLED=False");
  if (!url) throw new Error("Missing --url");
  const id = folderId(url);
  if (!id)
    return emitArchiveResultRecord(
      "noresults",
      "Not a Google Drive folder URL"
    );
  const timeoutMs = getEnvInt("GOOGLEDRIVE_TIMEOUT", 120) * 1000;
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
    if (folderId(page.url()) !== id)
      throw new Error(
        "Chrome tab is not on the requested Drive folder (login may be required)"
      );
    downloads = await captureBrowserDownloads({
      browser,
      page,
      timeoutMs: remaining(),
      downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
      trigger: async () => {
        // The signed-in breadcrumb downloads the folder itself, including nesting.
        // Anonymous Drive exposes only its built-in Select all + Download action.
        const folderButton =
          '[guidedhelpid="folder_path_button"] [role="button"]';
        await page.waitForSelector(
          `${folderButton}, [data-id="${id}"][role="link"]`,
          { timeout: remaining() }
        );
        const breadcrumb = await page.$(folderButton);
        console.error(
          breadcrumb
            ? "Opening Drive folder menu"
            : "Selecting folder contents in Drive"
        );
        if (breadcrumb) await breadcrumb.click();
        else {
          const first = await page.waitForSelector('[role="row"][data-id]', {
            timeout: remaining(),
          });
          await first.click();
          // Drive chooses shortcuts from the user agent, not the host OS.
          // ArchiveBox's default Mac UA also runs on Linux: using platform there
          // leaves only the first row selected and can download an empty folder.
          const modifier = await page.evaluate(() =>
            /Mac/.test(navigator.userAgent) ? "Meta" : "Control"
          );
          await page.keyboard.down(modifier);
          await page.keyboard.press("KeyA");
          await page.keyboard.up(modifier);
          await first.click({ button: "right" });
        }
        console.error("Opening Drive Download action");
        await page
          .locator('::-p-aria([name="Download"][role="menuitem"])')
          .setTimeout(remaining())
          .click();
        console.error("Drive is preparing the folder ZIP");
        // Wait for ZIP preparation to finish before closing the download batch.
        await page.waitForSelector('[aria-label="Cancel download"]', {
          timeout: remaining(),
        });
        await page.waitForFunction(
          () =>
            ![
              ...document.querySelectorAll('[aria-label="Cancel download"]'),
            ].some((el) => el.getClientRects().length),
          { timeout: remaining() }
        );
      },
    });
    await saveDownloads(
      path.join(snapshotDir, "googledrive"),
      await page.title(),
      downloads,
      { requireZip: true, timeoutMs: remaining() }
    );
    emitArchiveResultRecord("succeeded", "googledrive/downloads.json");
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
