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
  openExportPage,
  closeExportPage,
  captureBrowserDownloads,
  resolveChromeLaunchOptions,
  withTimeout,
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
    url.pathname.match(/^\/drive\/(?:u\/\d+\/)?folders\/([\w-]+)\/?$/)?.[1] ||
    (/^\/(?:u\/\d+\/)?folderview$/.test(url.pathname)
      ? url.searchParams.get("id")
      : null);
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
      "Not a Google Drive folder URL",
    );
  const timeoutMs = getEnvInt("GOOGLEDRIVE_TIMEOUT", 120) * 1000;
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
    waitForNavigationComplete: true,
  });
  let page = sourcePage;
  let downloads = [];
  try {
    if (folderId(page.url()) !== id)
      throw new Error(
        "Chrome tab is not on the requested Drive folder (login may be required)",
      );
    page = await openExportPage({
      page: sourcePage,
      chromeSessionDir: path.join(snapshotDir, "chrome"),
      timeoutMs: remaining(),
    });
    // Drive localizes its Download and ZIP-progress controls. Normalize only
    // this owned export tab; preserve the captured page and other URL options.
    const exportUrl = new URL(sourcePage.url());
    exportUrl.searchParams.set("hl", "en");
    await page.goto(exportUrl.href, {
      waitUntil: "domcontentloaded",
      timeout: remaining(),
    });
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
          { timeout: remaining() },
        );
        const breadcrumb = await page.$(folderButton);
        console.error(
          breadcrumb
            ? "Opening Drive folder menu"
            : "Selecting folder contents in Drive",
        );
        if (breadcrumb) await breadcrumb.click();
        else {
          const first = await page.waitForSelector('[role="row"][data-id]', {
            visible: true,
            timeout: remaining(),
          });
          await first.click();
          await page.waitForFunction(
            (row) => row.getAttribute("aria-selected") === "true",
            { timeout: remaining() },
            first,
          );
          const allRowsSelected = () => {
            const rows = [
              ...document.querySelectorAll(
                '[role="row"][data-id][aria-selected]',
              ),
            ].filter((row) => row.getClientRects().length > 0);
            return (
              rows.length > 0 &&
              rows.every((row) => row.getAttribute("aria-selected") === "true")
            );
          };
          // Drive may interpret the configured UA differently from the actual
          // platform. Verify Select all before opening the context menu: for a
          // one-row selection Drive happily downloads just that one directory.
          const modifier = await page.evaluate(() =>
            /Mac/.test(navigator.userAgent) ? "Meta" : "Control",
          );
          const selectAll = async (key) => {
            await page.keyboard.down(key);
            try {
              await page.keyboard.press("KeyA");
            } finally {
              await page.keyboard.up(key);
            }
          };
          await selectAll(modifier);
          try {
            await page.waitForFunction(allRowsSelected, {
              timeout: Math.min(1500, remaining()),
            });
          } catch (error) {
            if (error.name !== "TimeoutError") throw error;
            await selectAll(modifier === "Meta" ? "Control" : "Meta");
            await page.waitForFunction(allRowsSelected, {
              timeout: remaining(),
            });
          }
          await first.click({ button: "right" });
        }
        console.error("Opening Drive Download action");
        // Retain only UI state, before the deadline closes the owned tab.
        // This observation must not consume more than the existing budget.
        try {
          const state = await withTimeout(
            () => page.$$eval(
                '::-p-aria([name="Download"][role="menuitem"])',
                (matches) => {
                  const visible = (element) => {
                    const box = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return box.width > 0 && box.height > 0 &&
                      style.visibility !== "hidden" && style.display !== "none";
                  };
                  return {
                    phase: "Drive Download menu",
                    focused: document.hasFocus(),
                    visibility: document.visibilityState,
                    selectedRows: document.querySelectorAll('[role="row"][aria-selected="true"]').length,
                    visibleMenuItems: [...document.querySelectorAll('[role="menuitem"]')].filter(visible).length,
                    visibleButtons: [...document.querySelectorAll('[role="button"]')].filter(visible).length,
                    downloadMatches: matches.map((element) => ({
                      visible: visible(element),
                      box: element.getBoundingClientRect().toJSON(),
                    })),
                  };
                },
              ),
            Math.min(1000, remaining()),
            "Drive UI observation exceeded its remaining budget",
          );
          console.error(`Drive Download UI: ${JSON.stringify(state)}`);
        } catch {
          console.error("Drive Download UI observation unavailable within budget");
        }
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
          { timeout: remaining() },
        );
      },
    });
    await saveDownloads(
      path.join(snapshotDir, "googledrive"),
      await page.title(),
      downloads,
      { requireZip: true, timeoutMs: remaining() },
    );
    emitArchiveResultRecord("succeeded", "googledrive/downloads.json");
  } finally {
    if (page !== sourcePage)
      await closeExportPage({
        page,
        chromeSessionDir: path.join(snapshotDir, "chrome"),
      });
    // These paths were validated and claimed by this tab, even if unpacking
    // or the remaining deadline failed after Chrome completed its download.
    for (const { filePath } of downloads) {
      await fs.promises.unlink(filePath).catch((error) => {
        if (error.code !== "ENOENT")
          console.error(`Cannot remove provider download: ${error.message}`);
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
