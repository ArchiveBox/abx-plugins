#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Save the exact Chrome snapshot tab with the SingleFile extension.
 *
 * Chrome publishes the snapshot CDP target in target_id.txt. This helper maps
 * that exact target to one chrome.tabs id inside SingleFile's service worker,
 * dispatches the extension action to that tab, and moves its uniquely named
 * download from the browser-owned download directory into this snapshot.
 */

const fs = require("fs");
const path = require("path");
const {
  ensureNodeModuleResolution,
  loadConfig,
  parseArgs,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);

const chromeUtils = require("../chrome/chrome_utils.js");

const EXTENSION = {
  webstore_id: "mpiodijhokgodhhofbcjdecpffjipkle",
  name: "singlefile",
};
const SNAPSHOT_OUTPUT_DIR = process.cwd();
const CHROME_SESSION_DIR = path.resolve(SNAPSHOT_OUTPUT_DIR, "..", "chrome");
const hookConfig = loadConfig();
const CHROME_DOWNLOADS_DIR =
  chromeUtils.resolveChromeLaunchOptions(hookConfig).CHROME_DOWNLOADS_DIR;
const DOWNLOAD_WAIT_RESERVE_MS = 10000;
const SERVICE_WORKER_WAKE_PATH = "/src/ui/pages/offscreen-document.html";

function getSinglefileDownloadWaitTimeoutMs(
  config = hookConfig,
  elapsedMs = process.uptime() * 1000
) {
  const configuredTimeoutSeconds = Number(
    config.SINGLEFILE_TIMEOUT || config.TIMEOUT || 60
  );
  const totalTimeoutMs =
    Number.isFinite(configuredTimeoutSeconds) && configuredTimeoutSeconds > 0
      ? configuredTimeoutSeconds * 1000
      : 60000;
  return Math.max(3000, totalTimeoutMs - DOWNLOAD_WAIT_RESERVE_MS - elapsedMs);
}

async function moveAcrossMounts(src, dest) {
  try {
    await fs.promises.rename(src, dest);
  } catch (error) {
    if (error?.code !== "EXDEV") throw error;
    await fs.promises.copyFile(src, dest);
    await fs.promises.unlink(src);
  }
}

async function getExactChromeTab(extension, page) {
  const targetId = chromeUtils.getTargetIdFromPage(page);
  if (!targetId) {
    throw new Error("Chrome target_id.txt did not resolve to a page");
  }
  const worker = await extension.target.worker();
  if (!worker) {
    throw new Error("SingleFile service worker target is unavailable");
  }
  const expectedUrl = await page.url();
  await page.evaluate((marker) => {
    Object.defineProperty(globalThis, "__ABX_SINGLEFILE_TARGET_ID__", {
      value: marker,
      configurable: true,
    });
  }, targetId);
  let tab;
  try {
    tab = await worker.evaluate(
      async ({ expectedTargetId, expectedUrl }) => {
        const tabs = (await chrome.tabs.query({})).filter(
          (candidate) => candidate.url === expectedUrl
        );
        const matches = (
          await Promise.all(
            tabs.map(async (candidate) => {
              try {
                const results = await chrome.scripting.executeScript({
                  target: { tabId: candidate.id },
                  world: "MAIN",
                  func: (marker) =>
                    globalThis.__ABX_SINGLEFILE_TARGET_ID__ === marker,
                  args: [expectedTargetId],
                });
                return results[0]?.result === true ? candidate : null;
              } catch (error) {
                return null;
              }
            })
          )
        ).filter(Boolean);
        if (matches.length !== 1) {
          throw new Error(
            `expected one Chrome tab for target ${expectedTargetId}, found ${matches.length}`
          );
        }
        return matches[0];
      },
      { expectedTargetId: targetId, expectedUrl }
    );
  } finally {
    await page.evaluate(() => {
      delete globalThis.__ABX_SINGLEFILE_TARGET_ID__;
    });
  }
  if (!Number.isInteger(tab?.id)) {
    throw new Error(`No chrome.tabs id maps to target ${targetId}`);
  }
  if (tab.status !== "complete") {
    throw new Error(`Chrome navigation has not settled for target ${targetId}`);
  }
  return { targetId, tab };
}

async function saveSinglefileWithExtension(page, extension, options = {}) {
  if (!extension?.version) {
    throw new Error("SingleFile extension not found or not loaded");
  }

  const { targetId, tab } = await getExactChromeTab(extension, page);
  const currentUrl = await page.url();
  if (tab.url !== currentUrl) {
    throw new Error(
      `Chrome target URL mismatch for ${targetId}: page=${currentUrl}, tab=${tab.url}`
    );
  }
  const ignoredSchemes = new Set([
    "about",
    "chrome",
    "chrome-extension",
    "data",
    "javascript",
    "blob",
  ]);
  if (ignoredSchemes.has(new URL(currentUrl).protocol.replace(":", ""))) {
    return null;
  }

  const outputPath =
    options.outputPath || path.join(SNAPSHOT_OUTPUT_DIR, "singlefile.html");
  const requestedFilename = `archivebox-singlefile-${process.pid}-${Date.now()}.html`;
  await fs.promises.mkdir(CHROME_DOWNLOADS_DIR, { recursive: true });

  const timeoutMs = options.timeoutMs || getSinglefileDownloadWaitTimeoutMs();
  console.error(
    `[singlefile] saving exact target=${targetId} tab=${tab.id} url=${tab.url}`
  );
  const helperPage = await page.browser().newPage();
  let downloadedPath;
  try {
    await helperPage.goto(
      `chrome-extension://${extension.id}${SERVICE_WORKER_WAKE_PATH}`
    );
    downloadedPath = await helperPage.evaluate(
      async ({ exactTab, expectedFilename, timeoutMs }) => {
        const normalize = (value) => String(value || "").replace(/\\/g, "/");
        const previousIds = new Set(
          (await chrome.downloads.search({})).map((download) => download.id)
        );

        return await new Promise((resolve, reject) => {
          const timer = setTimeout(() => {
            cleanup();
            reject(
              new Error(
                `SingleFile download for tab ${exactTab.id} did not complete within ${timeoutMs}ms`
              )
            );
          }, timeoutMs);
          const cleanup = () => {
            clearTimeout(timer);
            chrome.downloads.onChanged.removeListener(onChanged);
          };
          const onChanged = async (change) => {
            if (previousIds.has(change.id) || !change.state) {
              return;
            }
            const matches = await chrome.downloads.search({ id: change.id });
            const filename = normalize(matches[0]?.filename);
            if (filename.split("/").pop() !== expectedFilename) {
              return;
            }
            if (change.state.current === "interrupted") {
              cleanup();
              reject(
                new Error(`SingleFile download ${change.id} was interrupted`)
              );
              return;
            }
            if (change.state.current !== "complete") return;
            cleanup();
            if (!matches[0]?.filename) {
              reject(
                new Error(`SingleFile download ${change.id} has no filename`)
              );
              return;
            }
            resolve(matches[0].filename);
          };

          chrome.downloads.onChanged.addListener(onChanged);
          import(chrome.runtime.getURL("src/core/bg/business.js"))
            .then((business) =>
              business.saveTabs([exactTab], {
                filenameTemplate: expectedFilename,
              })
            )
            .catch((error) => {
              cleanup();
              reject(error);
            });
        });
      },
      { exactTab: tab, expectedFilename: requestedFilename, timeoutMs }
    );
  } finally {
    await helperPage.close({ runBeforeUnload: false }).catch(() => {});
  }

  const stat = await fs.promises.stat(downloadedPath);
  if (stat.size <= 0) {
    throw new Error(`SingleFile download is empty: ${downloadedPath}`);
  }
  if (path.resolve(downloadedPath) !== path.resolve(outputPath)) {
    await moveAcrossMounts(downloadedPath, outputPath);
  }
  return outputPath;
}

async function main() {
  const args = parseArgs();
  const url = args.url;
  const outputPath =
    args.output_path || path.join(SNAPSHOT_OUTPUT_DIR, "singlefile.html");
  if (!url) {
    console.error("Usage: singlefile_extension_save.js --url=<url>");
    process.exit(1);
  }

  let browser = null;
  try {
    const connection = await chromeUtils.connectToPage({
      chromeSessionDir: CHROME_SESSION_DIR,
      timeoutMs: getSinglefileDownloadWaitTimeoutMs(hookConfig, 0),
      requireTargetId: true,
      requireBrowserReady: true,
      waitForNavigationComplete: true,
    });
    browser = connection.browser;

    const sessionEntry = chromeUtils.findExtensionMetadataByName(
      connection.extensions || [],
      EXTENSION.name
    );
    if (!sessionEntry?.id) {
      throw new Error("SingleFile extension metadata is missing from browser.json");
    }
    const extension = { ...sessionEntry };
    const manifest =
      chromeUtils.loadExtensionManifest(extension.unpacked_path) || {};
    const preferredTargetUrl = sessionEntry.target_url ||
      (manifest.background?.service_worker
        ? `chrome-extension://${extension.id}/${manifest.background.service_worker}`
        : null);
    const extensionTarget = await chromeUtils.waitForExtensionTargetHandle(
      browser,
      extension.id,
      getSinglefileDownloadWaitTimeoutMs(),
      preferredTargetUrl,
      { wakePath: SERVICE_WORKER_WAKE_PATH }
    );
    await chromeUtils.loadExtensionFromTarget([extension], extensionTarget);

    const savedPath = await saveSinglefileWithExtension(
      connection.page,
      extension,
      { outputPath, timeoutMs: getSinglefileDownloadWaitTimeoutMs() }
    );
    if (!savedPath) {
      console.error(`[singlefile] unsupported URL scheme: ${url}`);
      process.exit(3);
    }
    console.log(savedPath);
    process.exit(0);
  } catch (error) {
    console.error(`[❌] ${error.message || error}`);
    process.exit(4);
  } finally {
    if (browser) await browser.disconnect().catch(() => {});
  }
}

if (require.main === module) main();

module.exports = {
  EXTENSION,
  getSinglefileDownloadWaitTimeoutMs,
  saveSinglefileWithExtension,
};
