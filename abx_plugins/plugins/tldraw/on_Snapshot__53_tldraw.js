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
  if (!getEnvBool("TLDRAW_ENABLED", true)) {
    console.error("TLDRAW_ENABLED=False");
    return emitArchiveResultRecord("skipped", "TLDRAW_ENABLED=False");
  }
  const timeoutMs = getEnvInt("TLDRAW_TIMEOUT", 120) * 1000;
  const deadline = Date.now() + timeoutMs;
  const remaining = () => {
    const ms = deadline - Date.now();
    if (ms <= 0) throw new Error("Document export deadline exceeded");
    return ms;
  };
  const snapshotDir = path.resolve(config.SNAP_DIR || ".");
  const { browser, page } = await connectToPage({
    chromeSessionDir: path.join(snapshotDir, "chrome"),
    timeoutMs,
  });
  const downloads = [];
  try {
    const isBoard = (candidate) =>
      candidate.protocol === "https:" &&
      ["www.tldraw.com", "tldraw.com"].includes(candidate.hostname) &&
      /^\/(?:r|ro|f)\//.test(candidate.pathname);
    if (!isBoard(new URL(url)) && !isBoard(new URL(page.url()))) {
      console.error("Not a tldraw board");
      return emitArchiveResultRecord("noresults", "Not a tldraw board");
    }
    await waitForNavigationComplete(
      path.join(snapshotDir, "chrome"),
      remaining(),
    );
    const current = new URL(page.url());
    if (!isBoard(current)) {
      console.error("Not a tldraw board");
      return emitArchiveResultRecord("noresults", "Not a tldraw board");
    }
    const click = async (selector) => {
      const element = await page.waitForSelector(selector, {
        visible: true,
        timeout: remaining(),
      });
      const box = await element.boundingBox();
      if (!box) throw new Error(`tldraw control is not visible: ${selector}`);
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    };
    await page.bringToFront();
    await page.waitForSelector(".tl-shape", { timeout: remaining() });
    const close = await page.$('[data-testid="dialog.close"]');
    if (close) await click('[data-testid="dialog.close"]');
    for (const format of ["tldr", "svg"]) {
      downloads.push(
        ...(await captureBrowserDownloads({
          browser,
          page,
          timeoutMs: remaining(),
          downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
          trigger: async () => {
            await click('[data-testid="tla-main-menu"]');
            if (format === "tldr")
              await click('[data-testid="dialog.save-file-copy"]');
            else {
              const menu = await page.waitForSelector(
                '[data-testid="dialog-sub.export-all-as-button"]',
                { visible: true, timeout: remaining() },
              );
              const box = await menu.boundingBox();
              if (!box) throw new Error("tldraw export menu is not visible");
              await page.mouse.move(
                box.x + box.width / 2,
                box.y + box.height / 2,
              );
              await click('[data-testid="dialog.export-all-as-svg"]');
            }
          },
        })),
      );
    }
    for (const download of downloads) {
      const data = await fs.promises.readFile(download.filePath, "utf8");
      if (download.suggestedFilename.endsWith(".tldr")) {
        const document = JSON.parse(data);
        if (
          !document.tldrawFileFormatVersion ||
          !Array.isArray(document.records) ||
          !document.records.some((record) => record.typeName === "shape")
        )
          throw new Error("tldraw export contains no editable shapes");
      } else if (
        !download.suggestedFilename.endsWith(".svg") ||
        !/<svg\b/.test(data)
      )
        throw new Error("Invalid tldraw SVG export");
    }
    await saveDownloads(
      path.join(snapshotDir, "tldraw"),
      await page.title(),
      downloads,
      { timeoutMs: remaining() },
    );
    emitArchiveResultRecord("succeeded", "tldraw/downloads.json");
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
