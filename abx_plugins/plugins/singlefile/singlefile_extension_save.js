#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Save the exact Chrome snapshot tab with the SingleFile extension.
 *
 * Chrome publishes the snapshot CDP target in target_id.txt. This helper maps
 * that exact target to one chrome.tabs id inside SingleFile's service worker,
 * dispatches the extension action to that tab, and moves its correlated
 * download from the browser-owned download directory into this snapshot.
 */

const fs = require("fs");
const path = require("path");
const { createHash } = require("crypto");
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

async function moveAcrossMounts(src, dest, startedAt) {
  console.error(`[singlefile] phase=file-rename started elapsed_ms=${Date.now() - startedAt}`);
  try {
    await fs.promises.rename(src, dest);
  } catch (error) {
    if (error?.code !== "EXDEV") throw error;
    console.error(`[singlefile] phase=file-copy started elapsed_ms=${Date.now() - startedAt}`);
    await fs.promises.copyFile(src, dest);
    console.error(`[singlefile] phase=file-unlink started elapsed_ms=${Date.now() - startedAt}`);
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
  await fs.promises.mkdir(CHROME_DOWNLOADS_DIR, { recursive: true });

  const timeoutMs = options.timeoutMs || getSinglefileDownloadWaitTimeoutMs();
  const saveStartedAt = Date.now();
  const deadline = saveStartedAt + timeoutMs;
  console.error(
    `[singlefile] saving exact target=${targetId} tab=${tab.id} url=${tab.url}`
  );
  const helperPage = await page.browser().newPage();
  let releaseDownloadLock;
  let downloadedPath;
  let originalProfileOptions;
  let captureStarted = false;
  try {
    // Download behavior belongs to the live CDP connection. The launch
    // connection has already disconnected, so relying on it sends extension
    // downloads into the user's default Downloads folder. Share the existing
    // browser-download lock and use GUID names to avoid same-title collisions.
    const lockStartedAt = Date.now();
    console.error("[singlefile] phase=download-lock waiting");
    releaseDownloadLock = await chromeUtils.acquireSessionLock(
      path.join(CHROME_DOWNLOADS_DIR, ".download.lock"), timeoutMs
    );
    console.error(`[singlefile] phase=download-lock elapsed_ms=${Date.now() - lockStartedAt}`);
    await chromeUtils.sendBrowserCommand(page.browser(), "Browser.setDownloadBehavior", {
      behavior: "allowAndName", downloadPath: CHROME_DOWNLOADS_DIR, eventsEnabled: true,
    });
    console.error(`[singlefile] phase=helper-navigation started elapsed_ms=${Date.now() - saveStartedAt}`);
    await helperPage.goto(
      `chrome-extension://${extension.id}${SERVICE_WORKER_WAKE_PATH}`
    );
    // A snapshot is replayed at other viewport sizes. SingleFile's default
    // optimizations discard currently hidden images and alternate media rules.
    // Use its normal configuration API, restoring the persona's preferences
    // after this capture while still holding the browser download lock.
    console.error(`[singlefile] phase=helper-profile started elapsed_ms=${Date.now() - saveStartedAt}`);
    originalProfileOptions = await helperPage.evaluate(async (tabId) => {
      const [result] = await chrome.scripting.executeScript({
        target: { tabId },
        func: async () => chrome.runtime.sendMessage({ method: "bootstrap.init" }),
      });
      const { options } = result.result;
      const profile = {};
      const captureProfile = {};
      for (const key of ["removeHiddenElements", "removeUnusedStyles", "removeAlternativeMedias", "removeAlternativeImages"]) {
        profile[key] = options[key];
        captureProfile[key] = false;
      }
      await chrome.runtime.sendMessage({ method: "config.updateProfile", profileName: options.profileName, profile: captureProfile });
      return { profileName: options.profileName, profile };
    }, tab.id);
    // Use Chrome's actual toolbar action. Importing SingleFile's source modules
    // bypasses its bundled entrypoint and breaks bare imports in current releases.
    // Register download listeners first; a small page can finish immediately.
    console.error(`[singlefile] phase=helper-bindings started elapsed_ms=${Date.now() - saveStartedAt}`);
    const installed = (await page.browser().extensions()).get(extension.id);
    if (!installed) throw new Error("SingleFile is no longer installed in this browser");
    await helperPage.exposeFunction("archiveboxDownloadMatchesContent", async (filename, digest) => {
      const hash = createHash("sha256");
      for await (const chunk of fs.createReadStream(filename)) hash.update(chunk);
      const matches = hash.digest("hex") === digest;
      if (matches) console.error("[singlefile] matched exact-tab HTML bytes to completed download");
      return matches;
    });
    await helperPage.exposeFunction("archiveboxStartCapture", async () => {
      // Chrome actions target the selected tab. Select the exact saved CDP page;
      // opening the observer page above otherwise leaves that helper selected.
      await page.bringToFront();
      console.error(`[singlefile] phase=capture started elapsed_ms=${Date.now() - saveStartedAt}`);
      captureStarted = true;
      await page.triggerExtensionAction(installed);
    });
    const downloadTimeoutMs = deadline - Date.now();
    if (downloadTimeoutMs <= 0) throw new Error("SingleFile download deadline exceeded");
    // The capture budget belongs to this process: renderer timers can stop
    // advancing while Chrome is busy, but cancellation must precede our caller.
    downloadedPath = await chromeUtils.withTimeout(
      () => helperPage.evaluate(async ({ exactTab, extensionId }) => {
        return await new Promise((resolve, reject) => {
          let downloadUrl = null;
          let contentDigest = null;
          let contentSize = null;
          let contentParts = [];
          let settled = false;
          const startedAfter = new Date().toISOString();
          const cleanup = () => {
            chrome.downloads.onChanged.removeListener(onChanged);
            chrome.runtime.onMessage.removeListener(onMessage);
          };
          const fail = (error) => {
            if (settled) return;
            settled = true;
            cleanup();
            reject(error);
          };
          const acceptCompleted = async (item) => {
            if (settled || item?.state !== "complete" || item.byExtensionId !== extensionId ||
                new Date(item.startTime) < new Date(startedAfter)) {
              return false;
            }
            if (!downloadUrl || item.url !== downloadUrl) {
              // SingleFile resends the exact tab's HTML in chunks when the
              // background worker cannot fetch its blob URL. Its replacement
              // blob has a different URL: correlate the actual downloaded bytes,
              // never the newest download or a possibly shared page filename.
              if (!contentDigest || item.fileSize !== contentSize ||
                  !(await window.archiveboxDownloadMatchesContent(item.filename, contentDigest))) {
                return false;
              }
            }
            if (settled) return true;
            settled = true;
            cleanup();
            if (!item.filename) {
              reject(new Error(`SingleFile download ${item.id} has no filename`));
            } else {
              resolve(item.filename);
            }
            return true;
          };
          const onChanged = (change) => {
            (async () => {
              if (!change.state) return;
              const [item] = await chrome.downloads.search({ id: change.id });
              if (change.state.current === "interrupted" && downloadUrl && item?.url === downloadUrl) {
                fail(new Error(`SingleFile download ${change.id} was interrupted`));
              } else if (change.state.current === "complete") {
                await acceptCompleted(item);
              }
            })().catch(fail);
          };
          const findCompleted = async () => {
            for (const item of await chrome.downloads.search({ startedAfter })) {
              if (await acceptCompleted(item)) break;
            }
          };
          const onMessage = (message, sender) => {
            if (
              sender.tab?.id !== exactTab.id ||
              !message.method?.endsWith(".download")
            ) {
              return;
            }
            // Do not return a Promise from this listener: SingleFile's own
            // background listener must provide the download response.
            (async () => {
              if (message.blobURL) {
                downloadUrl = message.blobURL;
              } else if (typeof message.content === "string" && !message.compressContent) {
                contentParts.push(message.content);
                if (message.truncated && !message.finished) return;
                const content = new TextEncoder().encode(contentParts.join(""));
                contentParts = [];
                contentSize = content.byteLength;
                const digest = await crypto.subtle.digest("SHA-256", content);
                contentDigest = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
              } else {
                return;
              }
              await findCompleted();
            })().catch(fail);
          };
          chrome.downloads.onChanged.addListener(onChanged);
          chrome.runtime.onMessage.addListener(onMessage);
          window.archiveboxStartCapture().catch(fail);
        });
      },
      { exactTab: tab, extensionId: extension.id }),
      downloadTimeoutMs,
      `SingleFile download for tab ${tab.id} did not complete within ${downloadTimeoutMs}ms`
    );
    console.error(`[singlefile] phase=download completed elapsed_ms=${Date.now() - saveStartedAt}`);
  } catch (error) {
    if (captureStarted) {
      // Either the capture deadline or a CDP failure can end our wait.
      // Use the extension's pending-task API: the captured renderer may be
      // stalled and unable to execute a cancellation message itself.
      await chromeUtils.withTimeout(
        () => helperPage.evaluate(async (tabId) => {
          const tasks = await chrome.runtime.sendMessage({ method: "downloads.getInfo" });
          for (const task of tasks.filter(task => task.tabId === tabId)) {
            // SingleFile accepts string IDs; numeric 0 selects its tab fallback.
            await chrome.runtime.sendMessage({ method: "downloads.cancel", taskId: String(task.id) });
          }
        }, tab.id),
        DOWNLOAD_WAIT_RESERVE_MS,
        "SingleFile cancellation deadline exceeded"
      ).catch(cancelError => console.error("SingleFile cancellation failed:", cancelError));
    }
    throw error;
  } finally {
    try {
      if (originalProfileOptions) {
        await helperPage.evaluate(options => chrome.runtime.sendMessage({
          method: "config.updateProfile", ...options,
        }), originalProfileOptions);
      }
    } finally {
      if (releaseDownloadLock) {
        await chromeUtils.sendBrowserCommand(page.browser(), "Browser.setDownloadBehavior", {
          behavior: "allow", downloadPath: CHROME_DOWNLOADS_DIR, eventsEnabled: true,
        }).catch(() => {});
        releaseDownloadLock();
      }
      await helperPage.close({ runBeforeUnload: false }).catch(() => {});
    }
  }
  // The exact GUID download is complete and exclusively ours. Restore browser
  // state before publishing to an archive filesystem: a slow mount must not
  // hold the browser-wide download lock and stall other snapshot captures.
  console.error(`[singlefile] phase=browser-released elapsed_ms=${Date.now() - saveStartedAt}`);
  const stat = await fs.promises.stat(downloadedPath);
  console.error(`[singlefile] phase=file-stat completed elapsed_ms=${Date.now() - saveStartedAt}`);
  if (stat.size <= 0) {
    throw new Error(`SingleFile download is empty: ${downloadedPath}`);
  }
  if (path.resolve(downloadedPath) !== path.resolve(outputPath)) {
    await moveAcrossMounts(downloadedPath, outputPath, saveStartedAt);
  }
  console.error(`[singlefile] phase=file-saved elapsed_ms=${Date.now() - saveStartedAt}`);
  return outputPath;
}

async function main() {
  const startedAt = Date.now();
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
    console.error(`[singlefile] phase=connect elapsed_ms=${Date.now() - startedAt}`);

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
    console.error(`[singlefile] phase=extension elapsed_ms=${Date.now() - startedAt}`);

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
    // Context-loss messages alone cannot distinguish helper setup from capture.
    console.error(`[❌] ${error.stack || error.message || error}`);
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
