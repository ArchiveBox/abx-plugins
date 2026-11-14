#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Download raw CAD, 3D, and geospatial assets linked by the settled page.
 */

const fs = require("fs");
const { randomUUID } = require("crypto");
const path = require("path");

const {
  ensureNodeModuleResolution,
  emitArchiveResultRecord,
  getEnvBool,
  loadConfig,
  parseArgs,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);

const chromeUtils = require("../chrome/chrome_utils.js");
const { saveDownloads } = require("../base/downloads.js");

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || ".").trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
const ASSET_EXTENSIONS = new Set([
  "3ds",
  "3mf",
  "amf",
  "blend",
  "dae",
  "dxf",
  "dwg",
  "fbx",
  "geojson",
  "glb",
  "gltf",
  "gpx",
  "igs",
  "iges",
  "kml",
  "kmz",
  "obj",
  "ply",
  "prj",
  "sat",
  "shp",
  "shx",
  "sldasm",
  "sldprt",
  "step",
  "stl",
  "stp",
  "usd",
  "usda",
  "usdc",
  "usdz",
  "vrm",
  "vrml",
  "wrl",
  "x3d",
]);

function configInteger(name, fallback) {
  const value = Number(hookConfig[name]);
  return Number.isInteger(value) && value > 0 ? value : fallback;
}

function parseSize(value) {
  const match = String(value || "")
    .trim()
    .match(/^(\d+)([kmg])?$/i);
  if (!match) return 750 * 1024 * 1024;
  const units = { k: 1024, m: 1024 ** 2, g: 1024 ** 3 };
  return Number(match[1]) * (units[(match[2] || "").toLowerCase()] || 1);
}

async function collectAssets(page) {
  return await page.evaluate(
    (extensions) => {
      const extensionSet = new Set(extensions);
      const assets = new Map();
      const add = (value, source, filename = "") => {
        try {
          const url = new URL(value, document.baseURI);
          const extension = (filename || url.pathname)
            .split(".")
            .pop()
            .toLowerCase();
          if (
            !["http:", "https:"].includes(url.protocol) ||
            !extensionSet.has(extension)
          )
            return;
          const entry = assets.get(url.href) || {
            url: url.href,
            filename,
            sources: [],
          };
          if (!entry.sources.includes(source)) entry.sources.push(source);
          assets.set(url.href, entry);
        } catch {}
      };
      for (const element of document.querySelectorAll(
        "a[href],area[href],source[src],model-viewer[src],iframe[src],embed[src],object[data],[data-src],[data-url],script[src],link[href]",
      )) {
        for (const attribute of [
          "href",
          "src",
          "data",
          "data-src",
          "data-url",
        ]) {
          const value = element.getAttribute(attribute);
          if (value)
            add(
              value,
              `dom:${element.localName}`,
              element.getAttribute("download") || "",
            );
        }
      }
      for (const entry of performance.getEntriesByType("resource")) {
        add(entry.name, "resource");
      }
      return [...assets.values()];
    },
    [...ASSET_EXTENSIONS],
  );
}

async function main() {
  const args = parseArgs();
  if (!args.url) {
    console.error("Usage: on_Snapshot__53_caddl.js --url=<url>");
    process.exit(1);
  }
  if (!getEnvBool("CADDL_ENABLED", true)) {
    emitArchiveResultRecord("skipped", "CADDL_ENABLED=False");
    return;
  }

  const puppeteer = chromeUtils.resolvePuppeteerModule();
  const timeoutMs = configInteger("CADDL_TIMEOUT", 60) * 1000;
  const maxAssets = configInteger("CADDL_MAX_ASSETS", 50);
  const maxBytes = parseSize(hookConfig.CADDL_MAX_SIZE);
  const deadline = Date.now() + timeoutMs;
  const remaining = () => {
    const ms = deadline - Date.now();
    if (ms <= 0) throw new Error("CADDL download deadline exceeded");
    return ms;
  };
  await fs.promises.mkdir(OUTPUT_DIR, { recursive: true });
  const staging = await fs.promises.mkdtemp(
    path.join(OUTPUT_DIR, ".downloads-"),
  );
  let browser = null;
  let session = null;
  let exportPage = null;
  const downloads = [];
  try {
    const { browser: connectedBrowser, page } = await chromeUtils.connectToPage(
      {
        chromeSessionDir: path.join(SNAP_DIR, "chrome"),
        timeoutMs,
        waitForNavigationComplete: true,
        puppeteer,
      },
    );
    browser = connectedBrowser;
    const assets = (await collectAssets(page)).slice(0, maxAssets);
    const buttonLabels = await page.$$eval(
      'button[aria-label^="Download "]',
      (buttons, extensions) =>
        buttons
          .map((button) => button.getAttribute("aria-label"))
          .filter((label) =>
            extensions.includes(label.split(".").pop().toLowerCase()),
          ),
      [...ASSET_EXTENSIONS],
    );
    if (!assets.length && !buttonLabels.length) {
      browser.disconnect();
      emitArchiveResultRecord("noresults", "no CAD/3D asset URLs found");
      return;
    }

    session = await page.target().createCDPSession();
    for (const asset of assets) {
      const remainingMs = deadline - Date.now();
      if (remainingMs <= 0) {
        break;
      }
      let destination;
      try {
        const filename =
          asset.filename ||
          decodeURIComponent(path.basename(new URL(asset.url).pathname));
        destination = path.join(staging, randomUUID());
        const response = await chromeUtils.downloadBrowserResource({
          cdpSession: session,
          url: asset.url,
          outputPath: destination,
          timeoutMs: remainingMs,
          maxBytes,
        });
        const stat = await fs.promises.stat(destination);
        if (!stat.size) throw new Error("Downloaded asset is empty");
        if (
          String(
            response.headers["content-type"] ||
              response.headers["Content-Type"] ||
              "",
          ).includes("text/html")
        ) {
          throw new Error("Asset URL returned an HTML page");
        }
        downloads.push({ filePath: destination, suggestedFilename: filename });
      } catch (error) {
        if (destination) await fs.promises.unlink(destination).catch(() => {});
        console.error(`Skipping ${asset.url}: ${error.message}`);
      }
    }
    if (
      buttonLabels.length &&
      downloads.length < maxAssets &&
      Date.now() < deadline
    ) {
      try {
        exportPage = await chromeUtils.openExportPage({
          page,
          chromeSessionDir: path.join(SNAP_DIR, "chrome"),
          timeoutMs: remaining(),
        });
        await exportPage.goto(page.url(), {
          waitUntil: "domcontentloaded",
          timeout: remaining(),
        });
        for (const label of buttonLabels.slice(
          0,
          maxAssets - downloads.length,
        )) {
          if (Date.now() >= deadline) break;
          try {
            const button = await exportPage.waitForSelector(
              `button[aria-label=${JSON.stringify(label)}]`,
              { timeout: remaining() },
            );
            const captured = await chromeUtils.captureBrowserDownloads({
              browser,
              page: exportPage,
              downloadPath:
                chromeUtils.resolveChromeLaunchOptions(hookConfig)
                  .CHROME_DOWNLOADS_DIR,
              timeoutMs: remaining(),
              maxBytes,
              trigger: () => button.click(),
            });
            downloads.push(...captured);
          } catch (error) {
            console.error(`Skipping ${label}: ${error.message}`);
          }
        }
      } catch (error) {
        console.error(`Skipping download buttons: ${error.message}`);
      } finally {
        if (exportPage)
          await chromeUtils.closeExportPage({
            page: exportPage,
            chromeSessionDir: path.join(SNAP_DIR, "chrome"),
          });
        exportPage = null;
      }
    }
    await session.detach();
    session = null;
    browser.disconnect();
    browser = null;

    if (!downloads.length)
      throw new Error("No discovered CAD/3D assets could be downloaded");
    await saveDownloads(OUTPUT_DIR, args.url, downloads, {
      timeoutMs: remaining(),
    });
    emitArchiveResultRecord("succeeded", "caddl/downloads.json");
  } catch (error) {
    if (exportPage)
      await chromeUtils
        .closeExportPage({
          page: exportPage,
          chromeSessionDir: path.join(SNAP_DIR, "chrome"),
        })
        .catch(() => {});
    if (session) await session.detach().catch(() => {});
    if (browser) browser.disconnect();
    console.error(`ERROR: ${error.name}: ${error.message}`);
    emitArchiveResultRecord("failed", error.message);
    process.exitCode = 1;
  } finally {
    for (const download of downloads)
      await fs.promises.rm(download.filePath, { force: true });
    await fs.promises.rm(staging, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(`Fatal error: ${error.message}`);
  process.exit(1);
});
