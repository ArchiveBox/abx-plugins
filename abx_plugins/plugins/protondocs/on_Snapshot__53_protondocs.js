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
  if (!getEnvBool("PROTONDOCS_ENABLED", true)) {
    console.error("PROTONDOCS_ENABLED=False");
    return emitArchiveResultRecord("skipped", "PROTONDOCS_ENABLED=False");
  }
  const timeoutMs = getEnvInt("PROTONDOCS_TIMEOUT", 120) * 1000;
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
    const isDocument = (candidate) =>
      candidate.protocol === "https:" &&
      candidate.hostname === "docs.proton.me" &&
      candidate.pathname === "/doc";
    if (!isDocument(new URL(url)) && !isDocument(new URL(page.url()))) {
      console.error("Not a Proton document");
      return emitArchiveResultRecord("noresults", "Not a Proton document");
    }
    await waitForNavigationComplete(
      path.join(snapshotDir, "chrome"),
      remaining(),
    );
    const current = new URL(page.url());
    if (!isDocument(current)) {
      console.error("Not a Proton document");
      return emitArchiveResultRecord("noresults", "Not a Proton document");
    }
    // Proton defers loading its editor while the snapshot tab is hidden.
    await page.bringToFront();
    const titleButton = await page.waitForSelector(
      '[data-testid="document-name-dropdown"]',
      { visible: true, timeout: remaining() },
    );
    const editor = await page.waitForFrame(
      (frame) => frame.url().startsWith("https://docs-editor.proton.me/"),
      { timeout: remaining() },
    );
    await editor.waitForSelector('[data-testid="main-editor"] > *', {
      timeout: remaining(),
    });
    const title = await titleButton.evaluate((el) => el.textContent.trim());
    const click = (selector) =>
      page.locator(selector).setTimeout(remaining()).click();
    for (const [label, extension] of [
      ["Markdown (.md)", "md"],
      ["Web page (.html)", "html"],
      ["Microsoft Word (.docx)", "docx"],
    ]) {
      const exported = await captureBrowserDownloads({
        browser,
        page,
        timeoutMs: remaining(),
        downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
        trigger: async () => {
          await click('[data-testid="document-name-dropdown"]');
          await click('[data-testid="dropdown-download"]');
          await click(`::-p-aria([name="${label}"][role="button"])`);
        },
      });
      downloads.push(...exported);
      if (
        exported.length !== 1 ||
        !exported[0].suggestedFilename.endsWith(`.${extension}`)
      )
        throw new Error(`Proton Docs did not export ${extension}`);
    }
    await saveDownloads(
      path.join(snapshotDir, "protondocs"),
      title,
      downloads,
      { timeoutMs: remaining() },
    );
    emitArchiveResultRecord("succeeded", "protondocs/downloads.json");
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
