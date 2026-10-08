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
  waitForNavigationComplete,
  captureBrowserDownloads,
  resolveChromeLaunchOptions,
} = require("../chrome/chrome_utils.js");
const { saveDownloads } = require("../base/downloads.js");

async function main() {
  const config = loadConfig();
  const { url } = parseArgs();
  if (!url) throw new Error("Missing --url");
  if (!getEnvBool("MIRO_ENABLED", true)) {
    console.error("MIRO_ENABLED=False");
    return emitArchiveResultRecord("skipped", "MIRO_ENABLED=False");
  }
  const timeoutMs = getEnvInt("MIRO_TIMEOUT", 120) * 1000;
  const deadline = Date.now() + timeoutMs;
  const remaining = () => {
    const ms = deadline - Date.now();
    if (ms <= 0) throw new Error("Document export deadline exceeded");
    return ms;
  };
  const snapshotDir = path.resolve(config.SNAP_DIR || ".");
  const chromeSessionDir = path.join(snapshotDir, "chrome");
  const isBoard = (value) => {
    try {
      const candidate = new URL(value);
      return (
        candidate.protocol === "https:" &&
        candidate.hostname === "miro.com" &&
        /^\/app\/board\/[^/]+/.test(candidate.pathname)
      );
    } catch {
      return false;
    }
  };
  const { browser, page } = await connectToPage({
    chromeSessionDir,
    timeoutMs,
  });
  const downloads = [];
  try {
    if (!isBoard(url) && !isBoard(page.url())) {
      console.error("Not a Miro board");
      return emitArchiveResultRecord("noresults", "Not a Miro board");
    }
    await waitForNavigationComplete(chromeSessionDir, remaining());
    if (!isBoard(page.url())) {
      console.error("Not a Miro board");
      return emitArchiveResultRecord("noresults", "Not a Miro board");
    }
    await page.bringToFront();
    const waitForControl = async (selector) => {
      await page
        .waitForFunction(
          (selector) => {
            const el = document.querySelector(selector);
            if (!el) return false;
            const box = el.getBoundingClientRect();
            return box.width > 0 && box.height > 0;
          },
          { polling: 100, timeout: remaining() },
          selector,
        )
        .catch(() => {
          throw new Error(`Miro export control unavailable: ${selector}`);
        });
      return page.$(selector);
    };
    await waitForControl('[data-testid="board-settings__toggle-icon"]');
    await waitForControl("#main-canvas");
    await waitForControl('[data-testid="canvas-controls-zoom-display"]');
    if (await page.$('[data-testid="signup-bar-button"]')) {
      console.error("Persona must be logged in to miro.com");
      return emitArchiveResultRecord(
        "skipped",
        "Persona must be logged in to miro.com",
      );
    }
    const click = async (selector) => {
      const element = await waitForControl(selector);
      await page.waitForFunction(
        (selector) => {
          const el = document.querySelector(selector);
          if (!el) return false;
          const r = el.getBoundingClientRect();
          return el.contains(
            document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2),
          );
        },
        { polling: 100, timeout: remaining() },
        selector,
      );
      const box = await element.boundingBox();
      if (!box) throw new Error("Miro export control is not visible");
      // Miro's fixed board toolbar does not need scrolling. Clicking its
      // visible bounds avoids Puppeteer's scroll-into-view step, which stalls
      // on this board's renderer while the actual mouse click works.
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    };
    const openExportMenu = async () => {
      await click('[data-testid="board-settings__toggle-icon"]');
      for (const [selector, submenu] of [
        [
          '[data-testid="board-settings__item_board_menu"]',
          '[data-testid="settings-board__submenu"]',
        ],
        [
          '[data-testid="settings__export_subMenu"]',
          '[data-testid="settings-apps__submenu"]',
        ],
      ]) {
        const element = await waitForControl(selector);
        const box = await element.boundingBox();
        if (!box) throw new Error("Miro export submenu is not visible");
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
        await waitForControl(submenu);
      }
      await waitForControl('[data-testid="settings-apps__submenu"]');
    };
    await openExportMenu();
    const backup = await page.$(
      '[data-testid="settings-apps__submenu"] ::-p-text(Download board backup)',
    );
    if (
      backup &&
      !(await backup.evaluate((el) =>
        Boolean(el.closest('[aria-disabled="true"], [disabled]')),
      ))
    ) {
      downloads.push(
        ...(await captureBrowserDownloads({
          browser,
          page,
          timeoutMs: remaining(),
          downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
          trigger: async () => {
            const box = await backup.boundingBox();
            if (!box) throw new Error("Miro backup control is not visible");
            await page.mouse.click(
              box.x + box.width / 2,
              box.y + box.height / 2,
            );
          },
        })),
      );
      for (const download of downloads) {
        const data = await fs.promises.readFile(download.filePath);
        if (
          !download.suggestedFilename.endsWith(".rtb") ||
          !data.length ||
          /^\s*(?:<!doctype|<html)/i.test(data.subarray(0, 200).toString())
        )
          throw new Error("Miro did not return a native board backup");
      }
      await openExportMenu();
    }
    const pdf = await waitForControl(
      '[data-testid="board-settings__app_BOARD_EXPORT_PDF"]',
    );
    const pdfDisabled = await pdf.evaluate((el) =>
      Boolean(el.closest('[aria-disabled="true"], [disabled]')),
    );
    if (pdfDisabled && !downloads.length) {
      console.error("Owner disabled PDF export");
      return emitArchiveResultRecord("skipped", "Owner disabled PDF export");
    }
    const pdfDownloads = pdfDisabled
      ? []
      : await captureBrowserDownloads({
          browser,
          page,
          timeoutMs: remaining(),
          downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
          trigger: async () => {
            await click('[data-testid="board-settings__app_BOARD_EXPORT_PDF"]');
            // The provider's default PDF size is available on every export-enabled
            // plan; premium quality is never selected automatically.
            await click(
              '[data-testid="export-quality-settings-dialog__export-button"]:not([disabled])',
            );
          },
        });
    downloads.push(...pdfDownloads);
    for (const download of pdfDownloads) {
      const signature = (
        await fs.promises.readFile(download.filePath)
      ).subarray(0, 5);
      if (
        !download.suggestedFilename.endsWith(".pdf") ||
        !signature.equals(Buffer.from("%PDF-"))
      )
        throw new Error("Invalid Miro PDF export");
    }
    await saveDownloads(
      path.join(snapshotDir, "miro"),
      await page.title(),
      downloads,
      { timeoutMs: remaining() },
    );
    emitArchiveResultRecord("succeeded", "miro/downloads.json");
  } finally {
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
