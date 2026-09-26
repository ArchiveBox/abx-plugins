#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Download raw CAD, 3D, and geospatial assets linked by the settled page.
 */

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const {
  ensureNodeModuleResolution,
  emitArchiveResultRecord,
  getEnvBool,
  loadConfig,
  parseArgs,
  writeFileAtomic,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);

const chromeUtils = require("../chrome/chrome_utils.js");

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || ".").trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
const ASSET_EXTENSIONS = new Set([
  "3ds", "3mf", "amf", "blend", "dae", "dxf", "dwg", "fbx", "geojson",
  "glb", "gltf", "gpx", "igs", "iges", "kml", "kmz", "obj", "ply", "prj",
  "sat", "shp", "shx", "sldasm", "sldprt", "step", "stl", "stp", "usd",
  "usda", "usdc", "usdz", "vrm", "vrml", "wrl", "x3d",
]);

function configInteger(name, fallback) {
  const value = Number(hookConfig[name]);
  return Number.isInteger(value) && value > 0 ? value : fallback;
}

function parseSize(value) {
  const match = String(value || "").trim().match(/^(\d+)([kmg])?$/i);
  if (!match) return 750 * 1024 * 1024;
  const units = { k: 1024, m: 1024 ** 2, g: 1024 ** 3 };
  return Number(match[1]) * (units[(match[2] || "").toLowerCase()] || 1);
}

function getExtension(url) {
  try {
    return path.extname(new URL(url).pathname).slice(1).toLowerCase();
  } catch {
    return "";
  }
}

function safeFilename(filename, fallbackUrl, existing) {
  const fallback = path.basename(new URL(fallbackUrl).pathname) || "asset";
  let name = path.basename(filename || fallback)
    .replace(/[^\w.-]+/g, "_")
    .replace(/^[_ .]+|[_ .]+$/g, "") || fallback;
  if (!ASSET_EXTENSIONS.has(getExtension(`https://example.invalid/${name}`))) {
    name += `.${getExtension(fallbackUrl) || "asset"}`;
  }
  const extension = path.extname(name);
  const stem = path.basename(name, extension).slice(0, 180) || "asset";
  let candidate = `${stem}${extension}`;
  let index = 2;
  while (existing.has(candidate)) {
    candidate = `${stem}-${index}${extension}`;
    index += 1;
  }
  existing.add(candidate);
  return candidate;
}

async function collectAssets(page) {
  return await page.evaluate((extensions) => {
    const extensionSet = new Set(extensions);
    const assets = new Map();
    const add = (value, source) => {
      try {
        const url = new URL(value, document.baseURI);
        const extension = url.pathname.split(".").pop().toLowerCase();
        if (!["http:", "https:"].includes(url.protocol) || !extensionSet.has(extension)) return;
        const entry = assets.get(url.href) || { url: url.href, sources: [] };
        if (!entry.sources.includes(source)) entry.sources.push(source);
        assets.set(url.href, entry);
      } catch {}
    };
    for (const element of document.querySelectorAll(
      "a[href],area[href],source[src],model-viewer[src],iframe[src],embed[src],object[data],[data-src],[data-url],script[src],link[href]"
    )) {
      for (const attribute of ["href", "src", "data", "data-src", "data-url"]) {
        const value = element.getAttribute(attribute);
        if (value) add(value, `dom:${element.localName}`);
      }
    }
    for (const entry of performance.getEntriesByType("resource")) {
      add(entry.name, "resource");
    }
    return [...assets.values()];
  }, [...ASSET_EXTENSIONS]);
}

function waitForDownload(browser, connection, url, maxBytes, timeoutMs) {
  return new Promise((resolve, reject) => {
    let guid = null;
    const cleanup = () => {
      clearTimeout(timer);
      connection.off("Browser.downloadWillBegin", onBegin);
      connection.off("Browser.downloadProgress", onProgress);
    };
    const fail = (error) => {
      cleanup();
      reject(error);
    };
    const onBegin = (event) => {
      if (event.url === url) guid = event.guid;
    };
    const onProgress = (event) => {
      if (!guid || event.guid !== guid) return;
      if (event.receivedBytes > maxBytes) {
        chromeUtils.sendBrowserCommand(browser, "Browser.cancelDownload", { guid })
          .catch(() => {});
        fail(new Error(`download exceeds maximum size of ${maxBytes} bytes`));
      } else if (event.state === "completed") {
        cleanup();
        resolve(event);
      } else if (event.state === "canceled" || event.state === "interrupted") {
        fail(new Error(`download ${event.state}`));
      }
    };
    const timer = setTimeout(
      () => fail(new Error("download did not begin before timeout")),
      timeoutMs
    );
    connection.on("Browser.downloadWillBegin", onBegin);
    connection.on("Browser.downloadProgress", onProgress);
  });
}

async function waitForFile(filePath, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const stat = await fs.promises.stat(filePath);
      if (stat.isFile() && stat.size > 0) return stat;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`downloaded file was not published: ${path.basename(filePath)}`);
}

async function downloadAsset(browser, connection, sourcePage, url, downloadDir, maxBytes, timeoutMs) {
  const created = await chromeUtils.sendBrowserCommand(browser, "Target.createTarget", {
    url: sourcePage.url(),
  });
  let session = null;
  try {
    const target = browser.targets().find(
      (candidate) => chromeUtils.getTargetIdFromTarget(candidate) === created.targetId
    ) || await browser.waitForTarget(
      (candidate) => chromeUtils.getTargetIdFromTarget(candidate) === created.targetId,
      { timeout: timeoutMs }
    );
    const page = await target.page();
    if (!page) throw new Error("download target has no page");
    await page.waitForFunction(() => document.body !== null, { timeout: timeoutMs });
    session = await target.createCDPSession();
    const download = waitForDownload(browser, connection, url, maxBytes, timeoutMs);
    await page.evaluate((assetUrl) => {
      const link = document.createElement("a");
      link.href = assetUrl;
      link.download = "";
      document.body.append(link);
      link.click();
      link.remove();
    }, url);
    const event = await download;
    const filePath = path.join(downloadDir, event.suggestedFilename);
    await waitForFile(filePath, timeoutMs);
    return { filePath, suggestedFilename: event.suggestedFilename };
  } finally {
    await session?.detach().catch(() => {});
    await chromeUtils.sendBrowserCommand(browser, "Target.closeTarget", {
      targetId: created.targetId,
    }).catch(() => {});
  }
}

async function sha256(filePath) {
  const hash = crypto.createHash("sha256");
  await new Promise((resolve, reject) => {
    fs.createReadStream(filePath).on("data", (chunk) => hash.update(chunk))
      .on("end", resolve).on("error", reject);
  });
  return hash.digest("hex");
}

async function main() {
  const args = parseArgs();
  if (!args.url) {
    console.error("Usage: on_Snapshot__71_caddl.js --url=<url>");
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
  await fs.promises.mkdir(OUTPUT_DIR, { recursive: true });
  const downloadDir = await fs.promises.mkdtemp(path.join(OUTPUT_DIR, ".downloads-"));
  const stagedAssets = await fs.promises.mkdtemp(path.join(OUTPUT_DIR, ".assets-"));
  let browser = null;
  try {
    const { browser: connectedBrowser, page } = await chromeUtils.connectToPage({
      chromeSessionDir: "../chrome",
      timeoutMs,
      waitForNavigationComplete: true,
      puppeteer,
    });
    browser = connectedBrowser;
    const assets = (await collectAssets(page)).slice(0, maxAssets);
    if (!assets.length) {
      browser.disconnect();
      await fs.promises.rm(downloadDir, { recursive: true, force: true });
      await fs.promises.rm(stagedAssets, { recursive: true, force: true });
      emitArchiveResultRecord("noresults", "no CAD/3D asset URLs found");
      return;
    }

    const connection = chromeUtils.getBrowserConnection(browser);
    await chromeUtils.sendBrowserCommand(browser, "Browser.setDownloadBehavior", {
      behavior: "allow",
      downloadPath: downloadDir,
      eventsEnabled: true,
    });
    const manifest = { assets: [], skipped: [] };
    const filenames = new Set();
    for (const asset of assets) {
      const remainingMs = deadline - Date.now();
      if (remainingMs <= 0) {
        manifest.skipped.push({ ...asset, error: "overall timeout exceeded" });
        continue;
      }
      try {
        const download = await downloadAsset(
          browser, connection, page, asset.url, downloadDir, maxBytes, Math.min(remainingMs, 15000)
        );
        const filename = safeFilename(download.suggestedFilename, asset.url, filenames);
        const destination = path.join(stagedAssets, filename);
        await fs.promises.rename(download.filePath, destination);
        const stat = await fs.promises.stat(destination);
        manifest.assets.push({
          ...asset,
          filename,
          path: `assets/${filename}`,
          size: stat.size,
          sha256: await sha256(destination),
        });
      } catch (error) {
        manifest.skipped.push({ ...asset, error: error.message });
        console.error(`Skipping ${asset.url}: ${error.message}`);
      }
    }
    browser.disconnect();
    browser = null;

    if (!manifest.assets.length) {
      await fs.promises.rm(downloadDir, { recursive: true, force: true });
      await fs.promises.rm(stagedAssets, { recursive: true, force: true });
      writeFileAtomic(path.join(OUTPUT_DIR, "index.json"), JSON.stringify(manifest, null, 2));
      emitArchiveResultRecord("noresults", "matching assets could not be downloaded");
      return;
    }
    const assetDir = path.join(OUTPUT_DIR, "assets");
    const oldAssetDir = path.join(OUTPUT_DIR, ".assets-previous");
    await fs.promises.rm(oldAssetDir, { recursive: true, force: true });
    let movedPreviousAssets = false;
    try {
      await fs.promises.rename(assetDir, oldAssetDir);
      movedPreviousAssets = true;
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    try {
      await fs.promises.rename(stagedAssets, assetDir);
    } catch (error) {
      if (movedPreviousAssets) {
        await fs.promises.rename(oldAssetDir, assetDir).catch(() => {});
      }
      throw error;
    }
    await fs.promises.rm(oldAssetDir, { recursive: true, force: true });
    await fs.promises.rm(downloadDir, { recursive: true, force: true });
    writeFileAtomic(path.join(OUTPUT_DIR, "index.json"), JSON.stringify(manifest, null, 2));
    emitArchiveResultRecord("succeeded", `assets/${manifest.assets[0].filename} (${manifest.assets.length} assets)`);
  } catch (error) {
    if (browser) browser.disconnect();
    await fs.promises.rm(downloadDir, { recursive: true, force: true });
    await fs.promises.rm(stagedAssets, { recursive: true, force: true });
    console.error(`ERROR: ${error.name}: ${error.message}`);
    emitArchiveResultRecord("failed", error.message);
    process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error(`Fatal error: ${error.message}`);
  process.exit(1);
});
