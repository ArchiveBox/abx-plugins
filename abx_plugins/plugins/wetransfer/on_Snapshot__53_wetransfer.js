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
  readNavigatedUrls,
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
  const transferReady = () =>
    [...document.querySelectorAll("button")].some((button) => {
      if (!/^(Download|Download all)$/.test(button.textContent.trim()) || button.disabled) return false;
      const box = button.getBoundingClientRect();
      return box.width && box.height && button.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2));
    }) ||
    /transfer (?:has )?expired|transfer (?:was )?deleted|Oops, the transfer you requested/i.test(document.body.innerText);
  const snapshotDir = path.resolve(config.SNAP_DIR || ".");
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
  const navigatedUrls = readNavigatedUrls(path.join(snapshotDir, "chrome"));
  if (
    navigatedUrls &&
    !shortLink &&
    ![url, ...navigatedUrls].some((value) => isTransfer(new URL(value)))
  ) {
    console.error("Not a WeTransfer download");
    return emitArchiveResultRecord("noresults", "Not a WeTransfer download");
  }
  const { browser, page: sourcePage } = await connectToPage({
    chromeSessionDir: path.join(snapshotDir, "chrome"),
    timeoutMs,
  });
  let page = sourcePage;
  let downloads = [];
  try {
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
      console.error("Accepting WeTransfer terms");
      const controller = new AbortController();
      const consent = async () => {
        while (true) {
          // A survey can cover the consent button after navigation. Like other
          // provider exports, act on the topmost control: a covered click merely
          // dismisses the survey backdrop without accepting the transfer terms.
          const control = await page.waitForFunction(() => {
            return [...document.querySelectorAll("button")].find(button => {
              const label = (button.getAttribute("aria-label") || button.textContent).trim();
              if (label !== "I agree" && !(button.closest('[role="dialog"]') && /^(skip(?:\s|$)|close$)/i.test(label))) return false;
              if (button.disabled) return false;
              const box = button.getBoundingClientRect();
              return box.width && box.height && button.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2));
            });
          }, { timeout: remaining(), polling: 100, signal: controller.signal });
          try {
            const button = control.asElement();
            const label = await button.evaluate(element => (element.getAttribute("aria-label") || element.textContent).trim());
            const selector = `::-p-aria([name=${JSON.stringify(label)}][role="button"])`;
            // React can replace the button after observation. Resolve the live
            // control for the click, just as we do for the Download button.
            await page.locator(selector).setTimeout(remaining()).click({signal: controller.signal});
            if (label === "I agree") break;
            console.error("Dismissed optional WeTransfer dialog");
            await page.waitForSelector(selector, { hidden: true, timeout: remaining(), signal: controller.signal });
          } finally { await control.dispose(); }
        }
      };
      // Cookie hydration can remove consent without another click. Stop the
      // consent flow once the transfer is ready, including a stale control wait.
      const pending = [
        consent(),
        page.waitForFunction(transferReady, {timeout: remaining(), signal: controller.signal})
          .then(handle => handle.dispose()),
      ];
      let failure;
      try {
        await Promise.race(pending);
      } catch (error) {
        failure = error;
      }
      controller.abort();
      // Settle the cancelled locator before issuing the download click, and
      // preserve unexpected failures even if the readiness wait won the race.
      const settled = await Promise.allSettled(pending);
      if (failure) throw failure;
      for (const result of settled) {
        if (result.status === "rejected" && result.reason !== controller.signal.reason) throw result.reason;
      }
    }
    await page.waitForFunction(transferReady, { timeout: remaining() });
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
