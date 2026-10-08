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
  if (!getEnvBool("WETRANSFER_ENABLED", true)) {
    console.error("WETRANSFER_ENABLED=False");
    return emitArchiveResultRecord("skipped", "WETRANSFER_ENABLED=False");
  }
  const timeoutMs = getEnvInt("WETRANSFER_TIMEOUT", 120) * 1000;
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
    const isTransfer = (candidate) =>
      candidate.protocol === "https:" &&
      /(^|\.)wetransfer\.com$/.test(candidate.hostname) &&
      (candidate.pathname.startsWith("/downloads/") ||
        /^\/previews\/[a-f0-9]+\/[a-f0-9]+\/?$/.test(candidate.pathname));
    const original = new URL(url);
    const shortLink =
      original.protocol === "https:" &&
      original.hostname === "we.tl" &&
      /^\/t-[A-Za-z0-9]+\/?$/.test(original.pathname);
    if (
      !shortLink &&
      !isTransfer(original) &&
      !isTransfer(new URL(page.url()))
    ) {
      console.error("Not a WeTransfer download");
      return emitArchiveResultRecord("noresults", "Not a WeTransfer download");
    }
    await waitForNavigationComplete(
      path.join(snapshotDir, "chrome"),
      remaining(),
    );
    const current = new URL(page.url());
    if (!isTransfer(current)) {
      console.error("Not a WeTransfer download");
      return emitArchiveResultRecord("noresults", "Not a WeTransfer download");
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
    await page.waitForFunction(
      () =>
        [...document.querySelectorAll("button")].some((button) =>
          /^(I agree|Download|Download all)$/.test(button.textContent.trim()),
        ) ||
        /transfer (?:has )?expired|transfer (?:was )?deleted|Oops, the transfer you requested/i.test(
          document.body.innerText,
        ),
      { timeout: remaining(), polling: "mutation" },
    );
    const terms = await page.evaluate(() =>
      [...document.querySelectorAll("button")].some(
        (button) => button.textContent.trim() === "I agree",
      ),
    );
    if (terms) {
      await page
        .locator('::-p-aria([name="I agree"][role="button"])')
        .setTimeout(remaining())
        .click();
      await page.waitForFunction(
        () =>
          [...document.querySelectorAll("button")].some((button) =>
            /^(Download|Download all)$/.test(button.textContent.trim()),
          ) ||
          /transfer (?:has )?expired|transfer (?:was )?deleted|Oops, the transfer you requested/i.test(
            document.body.innerText,
          ),
        { timeout: remaining(), polling: "mutation" },
      );
    }
    const expired = await page.evaluate(() =>
      /transfer (?:has )?expired|transfer (?:was )?deleted|Oops, the transfer you requested/i.test(
        document.body.innerText,
      ),
    );
    if (expired) throw new Error("WeTransfer share expired or was deleted");
    const title = await page.title();
    downloads = await captureBrowserDownloads({
      browser,
      page,
      timeoutMs: remaining(),
      downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
      trigger: async () => {
        await page
          .locator(
            '::-p-aria([name="Download"][role="button"]), ::-p-aria([name="Download all"][role="button"])',
          )
          .setTimeout(remaining())
          .click();
      },
    });
    await saveDownloads(
      path.join(snapshotDir, "wetransfer"),
      title,
      downloads,
      {
        timeoutMs: remaining(),
      },
    );
    emitArchiveResultRecord("succeeded", "wetransfer/downloads.json");
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
