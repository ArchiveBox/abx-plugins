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
  if (!getEnvBool("EXCALIDRAW_ENABLED", true)) {
    console.error("EXCALIDRAW_ENABLED=False");
    return emitArchiveResultRecord("skipped", "EXCALIDRAW_ENABLED=False");
  }
  const timeoutMs = getEnvInt("EXCALIDRAW_TIMEOUT", 120) * 1000;
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
    const original = new URL(url);
    const isShare = (candidate) =>
      candidate.protocol === "https:" &&
      ["excalidraw.com", "app.excalidraw.com"].includes(candidate.hostname) &&
      /^#(?:json|room)=[A-Za-z0-9_-]+,[A-Za-z0-9_-]+$/.test(candidate.hash);
    if (!isShare(original) && !isShare(new URL(page.url()))) {
      console.error("Not an Excalidraw shared scene");
      return emitArchiveResultRecord(
        "noresults",
        "Not an Excalidraw shared scene",
      );
    }
    await waitForNavigationComplete(
      path.join(snapshotDir, "chrome"),
      remaining(),
    );
    const current = new URL(page.url());
    if (
      current.protocol !== "https:" ||
      !["excalidraw.com", "app.excalidraw.com"].includes(current.hostname) ||
      !(isShare(original) || isShare(current))
    ) {
      console.error("Not an Excalidraw shared scene");
      return emitArchiveResultRecord(
        "noresults",
        "Not an Excalidraw shared scene",
      );
    }
    if (
      await page.evaluate(() => typeof window.showSaveFilePicker === "function")
    )
      throw new Error(
        "Excalidraw requires Chrome launched with --disable-blink-features=FileSystemAccessLocal for automatic browser downloads",
      );
    // A consumed fragment alone is ambiguous: canceling the provider's import
    // confirmation also clears it and keeps the persona's previous local scene.
    // Corroborate a JSON import with the provider's existing resource timing,
    // and never accept or dismiss a destructive overwrite confirmation.
    const share = isShare(original) ? original : current;
    const [kind, id] = share.hash.slice(1).split(/[=,]/);
    await page.bringToFront();
    const readyHandle = await page.waitForFunction(
      (expectedKind, expectedId, expectedHash) => {
        const visible = (selector) =>
          [...document.querySelectorAll(selector)].some(
            (element) =>
              element.getBoundingClientRect().width &&
              element.getBoundingClientRect().height,
          );
        if (visible(".OverwriteConfirm")) return "overwrite";
        if (
          visible(".Modal.Dialog") &&
          /error|could not|couldn't|failed|invalid/i.test(
            document.querySelector(".Modal.Dialog").innerText,
          )
        )
          return "error";
        if (
          !document.querySelector('[data-testid="main-menu-trigger"]') ||
          visible(".LoadingMessage")
        )
          return false;
        if (expectedKind === "room")
          return location.hash === expectedHash ? "ready" : "not imported";
        if (location.hash === expectedHash) return false;
        if (location.hash) return "not imported";
        const navigation = performance.getEntriesByType("navigation")[0];
        const imported = performance
          .getEntriesByType("resource")
          .some(
            (resource) =>
              resource.name ===
                `https://json.excalidraw.com/api/v2/${expectedId}` &&
              resource.responseStatus === 200,
          );
        return imported &&
          navigation &&
          new URL(navigation.name).hash === expectedHash
          ? "ready"
          : "not imported";
      },
      { timeout: remaining(), polling: 100 },
      kind,
      id,
      share.hash,
    );
    const ready = await readyHandle.jsonValue();
    await readyHandle.dispose();
    if (ready === "overwrite")
      throw new Error(
        "Excalidraw overwrite confirmation requires a user decision; the existing local scene was preserved",
      );
    if (ready === "error")
      throw new Error(
        "Excalidraw displayed a scene import error; the existing local scene was preserved",
      );
    if (ready !== "ready")
      throw new Error(
        "Excalidraw shared scene was not imported; refusing to export the persona's previous local scene",
      );
    const click = async (selector) => {
      const element = await page.waitForSelector(selector, {
        visible: true,
        timeout: remaining(),
      });
      const box = await element.boundingBox();
      if (!box)
        throw new Error(`Excalidraw control is not visible: ${selector}`);
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    };
    for (const format of ["excalidraw", "svg"]) {
      downloads.push(
        ...(await captureBrowserDownloads({
          browser,
          page,
          timeoutMs: remaining(),
          downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
          trigger: async () => {
            await click('[data-testid="main-menu-trigger"]');
            await click(
              `[data-testid="${
                format === "excalidraw" ? "json" : "image"
              }-export-button"]`,
            );
            if (format === "excalidraw")
              await click('button[aria-label="Save to file"]');
            else {
              const embed = await page.waitForSelector("#exportEmbedSwitch", {
                timeout: remaining(),
              });
              if (!(await embed.evaluate((el) => el.checked)))
                await click("#exportEmbedSwitch");
              await page.waitForFunction(
                () => {
                  const input = document.querySelector("#exportEmbedSwitch");
                  return (
                    input?.checked === true &&
                    input.closest(".Switch")?.classList.contains("toggled")
                  );
                },
                { timeout: remaining() },
              );
              await click('button[aria-label="Export to SVG"]');
            }
          },
        })),
      );
      await page.keyboard.press("Escape");
    }
    for (const download of downloads) {
      const data = await fs.promises.readFile(download.filePath, "utf8");
      if (download.suggestedFilename.endsWith(".excalidraw")) {
        const scene = JSON.parse(data);
        if (
          scene.type !== "excalidraw" ||
          !Array.isArray(scene.elements) ||
          !scene.elements.some((element) => !element.isDeleted)
        )
          throw new Error("Excalidraw export contains no editable elements");
      } else if (
        !download.suggestedFilename.endsWith(".svg") ||
        !/<svg\b/.test(data) ||
        !data.includes("payload-type:application/vnd.excalidraw+json") ||
        !data.includes("payload-start")
      )
        throw new Error(
          `Invalid Excalidraw SVG export: ${
            download.suggestedFilename
          }, SVG=${/<svg\b/.test(data)}, embedded scene=${data.includes(
            "payload-start",
          )}`,
        );
    }
    await saveDownloads(
      path.join(snapshotDir, "excalidraw"),
      await page.title(),
      downloads,
      { timeoutMs: remaining() },
    );
    emitArchiveResultRecord("succeeded", "excalidraw/downloads.json");
  } finally {
    for (const { filePath } of downloads)
      await fs.promises.unlink(filePath).catch((error) => {
        if (error.code !== "ENOENT") console.error(error.message);
      });
    await browser.disconnect();
  }
}
main().catch((error) => {
  console.error(error.stack);
  emitArchiveResultRecord("failed", error.message);
  process.exitCode = 1;
});
