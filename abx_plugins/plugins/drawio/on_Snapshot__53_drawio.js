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
  if (!getEnvBool("DRAWIO_ENABLED", true)) {
    console.error("DRAWIO_ENABLED=False");
    return emitArchiveResultRecord("skipped", "DRAWIO_ENABLED=False");
  }
  const timeoutMs = getEnvInt("DRAWIO_TIMEOUT", 120) * 1000;
  const deadline = Date.now() + timeoutMs;
  const remaining = () => {
    const ms = deadline - Date.now();
    if (ms <= 0) throw new Error("Document export deadline exceeded");
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
    const isDiagram = (candidate) => {
      if (
        candidate.protocol !== "https:" ||
        !["app.diagrams.net", "app.draw.io"].includes(candidate.hostname)
      )
        return false;
      // https://www.drawio.com/docs/reference/supported-location-hash-properties/
      // Storage/document references differ from editor settings and local files.
      const reference = candidate.hash.slice(1).split("#", 1)[0];
      if (/^[GWTDAHR].+/.test(reference)) return true;
      if (!reference.startsWith("U")) return false;
      try {
        const source = new URL(decodeURIComponent(reference.slice(1)));
        return ["http:", "https:"].includes(source.protocol);
      } catch {
        return false;
      }
    };
    if (!isDiagram(new URL(url)) && !isDiagram(new URL(page.url()))) {
      console.error("Not a shared draw.io diagram");
      return emitArchiveResultRecord(
        "noresults",
        "Not a shared draw.io diagram",
      );
    }
    await waitForNavigationComplete(
      path.join(snapshotDir, "chrome"),
      remaining(),
    );
    const current = new URL(page.url());
    if (!isDiagram(current)) {
      console.error("Not a shared draw.io diagram");
      return emitArchiveResultRecord(
        "noresults",
        "Not a shared draw.io diagram",
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
    const click = async (selector) =>
      page.locator(selector).setTimeout(remaining()).click();
    downloads = await captureBrowserDownloads({
      browser,
      page,
      timeoutMs: remaining(),
      downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
      trigger: async () => {
        // EditorUi.fileLoaded calls setGraphEnabled(true) after file.open();
        // that makes the diagram container visible, even for empty diagrams.
        // The filename header is optional across editor layouts.
        await page.waitForSelector(".geDiagramContainer", {
          visible: true,
          timeout: remaining(),
        });
        await click(".geMenubar ::-p-text(File)");
        const exportMenu = await page.waitForSelector("::-p-text(Export as)", {
          visible: true,
          timeout: remaining(),
        });
        await exportMenu.hover();
        await click("::-p-text(SVG...)");
        await page.waitForSelector('.geDialog input[type="checkbox"]', {
          timeout: remaining(),
        });
        const include = await page.$(
          '::-p-aria([name="Include a copy of my diagram"])',
        );
        if (!include)
          throw new Error("Editable SVG export option is unavailable");
        if (!(await include.evaluate((el) => el.checked)))
          await include.click();
        await click(".geDialog ::-p-text(Export)");
        await page.waitForSelector(
          '.geDialog select option[value="download"]',
          { timeout: remaining() },
        );
        await page.select(".geDialog select", "download");
        await click(".geDialog ::-p-text(OK)");
      },
    });
    if (
      downloads.length !== 1 ||
      !downloads[0].suggestedFilename.endsWith(".svg")
    )
      throw new Error("draw.io did not export an SVG document");
    const svg = await fs.promises.readFile(downloads[0].filePath, "utf8");
    if (!/<svg\b/.test(svg) || !svg.includes("mxfile"))
      throw new Error("SVG export is missing editable diagram data");
    await saveDownloads(
      path.join(snapshotDir, "drawio"),
      await page.title(),
      downloads,
      { timeoutMs: remaining() },
    );
    emitArchiveResultRecord("succeeded", "drawio/downloads.json");
  } finally {
    if (page !== sourcePage)
      await closeExportPage({
        page,
        chromeSessionDir: path.join(snapshotDir, "chrome"),
      });
    for (const { filePath } of downloads)
      await fs.promises.unlink(filePath).catch((error) => {
        if (error.code !== "ENOENT")
          console.error(error.stack || error.message);
      });
    await browser.disconnect();
  }
}
main().catch((error) => {
  console.error(error.stack || error.message);
  emitArchiveResultRecord("failed", error.message);
  process.exitCode = 1;
});
