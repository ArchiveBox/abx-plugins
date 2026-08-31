#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Stop the exact ArchiveWeb.page recording started for this snapshot and save
 * its WACZ. The start hook persists the extension, CDP target, chrome.tabs id,
 * and collection id in recording.json; this hook never rediscovers any of
 * those identities from active browser state.
 */

const fs = require("fs");
const path = require("path");

const {
  ensureNodeModuleResolution,
  parseArgs,
  getEnvBool,
  getEnvInt,
  emitArchiveResultRecord,
  loadConfig,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);

const chromeUtils = require("../chrome/chrome_utils.js");
const puppeteer = chromeUtils.resolvePuppeteerModule();
const {
  openAwpHelperTab,
  resolveChromeDirs,
  pickChromeSessionDir,
} = require("./awp_internal.js");

const hookConfig = loadConfig();
const PLUGIN_DIR = path.basename(__dirname);
const OUTPUT_FILENAME = "archivewebpage.wacz";
const RECORDING_STATE_FILENAME = "recording.json";
const {
  outputDir,
  candidates: chromeDirCandidates,
} = resolveChromeDirs(process.cwd(), hookConfig.CRAWL_DIR);
process.chdir(outputDir);
const SNAP_DIR = path.resolve(outputDir, "..");

async function moveAcrossMounts(src, dest) {
  try {
    await fs.promises.rename(src, dest);
  } catch (error) {
    if (error?.code !== "EXDEV") throw error;
    await fs.promises.copyFile(src, dest);
    await fs.promises.unlink(src);
  }
}

function observePublishedFile(filePath, timeoutMs) {
  const directory = path.dirname(filePath);
  const expectedName = path.basename(filePath);
  let settled = false;
  let watcher = null;
  let timer = null;

  const close = () => {
    if (timer) clearTimeout(timer);
    timer = null;
    if (watcher) watcher.close();
    watcher = null;
  };
  const promise = new Promise((resolve, reject) => {
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      close();
      callback(value);
    };
    watcher = fs.watch(directory, async (_eventType, filename) => {
      if (filename?.toString() !== expectedName) return;
      try {
        const stat = await fs.promises.stat(filePath);
        if (stat.size > 0) finish(resolve, stat);
      } catch (error) {
        // Chrome can announce the temporary-file rename before its final name
        // is visible. A later filesystem event is the publication boundary.
        if (error?.code !== "ENOENT") finish(reject, error);
      }
    });
    timer = setTimeout(
      () =>
        finish(
          reject,
          new Error(
            `Download ${expectedName} was not published within ${timeoutMs}ms`
          )
        ),
      timeoutMs
    );
  });

  return { promise, close };
}

function readRecordingState() {
  const statePath = path.join(outputDir, RECORDING_STATE_FILENAME);
  const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
  if (
    state?.version !== 1 ||
    !state.extensionId ||
    !state.snapshotTargetId ||
    !Number.isInteger(state.chromeTabId) ||
    !state.collId
  ) {
    throw new Error(`Invalid ArchiveWeb.page recording state in ${statePath}`);
  }
  return state;
}

async function stopExactRecording(helperPage, state, timeoutMs) {
  return await helperPage.evaluate(
    async ({ tabId, expectedCollId, timeoutMs }) => {
      const port = document.querySelector("wr-popup-viewer")?.port;
      if (!port) throw new Error("AWP popup port is not ready");
      const queuedMessages = [];
      const queueMessage = (message) => queuedMessages.push(message);
      port.onMessage.addListener(queueMessage);

      function waitFor(predicate, label) {
        return new Promise((resolve, reject) => {
          const queuedIndex = queuedMessages.findIndex(predicate);
          if (queuedIndex !== -1) {
            resolve(queuedMessages.splice(queuedIndex, 1)[0]);
            return;
          }
          const onMessage = (message) => {
            if (!predicate(message)) return;
            clearTimeout(timer);
            port.onMessage.removeListener(onMessage);
            resolve(message);
          };
          const timer = setTimeout(() => {
            port.onMessage.removeListener(onMessage);
            reject(new Error(`timed out waiting for ${label}`));
          }, timeoutMs);
          port.onMessage.addListener(onMessage);
        });
      }

      try {
        port.postMessage({ type: "startUpdates", tabId });
        await waitFor(
          (message) => message?.type === "collections",
          "collections"
        );
        const initialStatus = await waitFor(
          (message) =>
            message?.type === "status" && Boolean(message.collId),
          "active recorder status"
        );
        if (initialStatus.collId !== expectedCollId) {
          throw new Error(
            `recording collection changed: expected ${expectedCollId}, got ${initialStatus.collId}`
          );
        }
        if (initialStatus.recording !== true) {
          throw new Error(
            `recording is not active for tab ${tabId} and collection ${expectedCollId}`
          );
        }

        port.postMessage({ type: "stopRecording" });
        const finalStatus = await waitFor(
          (message) =>
            message?.type === "status" && message.recording === false,
          "recording stop"
        );
        return finalStatus;
      } finally {
        port.onMessage.removeListener(queueMessage);
      }
    },
    {
      tabId: state.chromeTabId,
      expectedCollId: state.collId,
      timeoutMs,
    }
  );
}

async function downloadExactWacz(
  browser,
  extensionId,
  collId,
  destPath,
  timeoutMs
) {
  const chromeLaunchOptions = chromeUtils.resolveChromeLaunchOptions(hookConfig);
  const downloadDir = chromeLaunchOptions.CHROME_DOWNLOADS_DIR
    ? path.resolve(chromeLaunchOptions.CHROME_DOWNLOADS_DIR)
    : path.dirname(destPath);
  fs.mkdirSync(downloadDir, { recursive: true });

  const requestedFilename = `archivewebpage-${process.pid}-${Date.now()}.wacz`;
  const dlUrl = `chrome-extension://${extensionId}/w/api/c/${encodeURIComponent(
    collId
  )}/dl?format=wacz&pages=all&filename=${encodeURIComponent(
    requestedFilename.replace(/\.wacz$/i, "")
  )}`;
  const downloadedPath = path.join(downloadDir, requestedFilename);
  const browserConnection = chromeUtils.getBrowserConnection(browser);
  let publishedFile = null;
  let targetId = null;
  let downloadSession = null;

  try {
    await chromeUtils.sendBrowserCommand(browser, "Browser.setDownloadBehavior", {
      behavior: "allow",
      downloadPath: downloadDir,
      eventsEnabled: true,
    });
    const created = await chromeUtils.sendBrowserCommand(
      browser,
      "Target.createTarget",
      { url: "about:blank" }
    );
    targetId = created.targetId;
    const matchesTarget = (target) =>
      chromeUtils.getTargetIdFromTarget(target) === targetId;
    const target =
      browser.targets().find(matchesTarget) ||
      (await browser.waitForTarget(matchesTarget, { timeout: timeoutMs }));
    const downloadPage = await target.page();
    if (!downloadPage) {
      throw new Error(`WACZ download target ${targetId} has no page`);
    }
    downloadSession = await target.createCDPSession();
    publishedFile = observePublishedFile(downloadedPath, timeoutMs);
    const downloadCompleted = chromeUtils.waitForBrowserDownload(
      browserConnection,
      requestedFilename,
      timeoutMs
    );
    await downloadSession.send("Page.navigate", { url: dlUrl });
    // Chrome's CDP contract explicitly does not guarantee that the final path
    // exists when Browser.downloadProgress reports "completed". Require both
    // browser completion and the exact filesystem publication event before we
    // take ownership of the WACZ.
    await Promise.all([downloadCompleted, publishedFile.promise]);
    if (path.resolve(downloadedPath) !== path.resolve(destPath)) {
      await moveAcrossMounts(downloadedPath, destPath);
    }
    return (await fs.promises.stat(destPath)).size;
  } finally {
    publishedFile?.close();
    await downloadSession?.detach().catch(() => {});
    if (targetId) {
      await chromeUtils
        .sendBrowserCommand(browser, "Target.closeTarget", { targetId })
        .catch(() => {});
    }
  }
}

async function main() {
  const startedAt = Date.now();
  const args = parseArgs();
  if (!args.url) {
    console.error("Usage: on_Snapshot__65_archivewebpage_stop.js --url=<url>");
    process.exit(1);
  }
  if (!getEnvBool("ARCHIVEWEBPAGE_ENABLED", true)) {
    emitArchiveResultRecord("skipped", "ARCHIVEWEBPAGE_ENABLED=False");
    process.exit(0);
  }

  const budgetMs = getEnvInt("ARCHIVEWEBPAGE_HOOK_BUDGET_MS", 2000);
  const timeoutMs = Math.max(
    budgetMs * 3,
    getEnvInt("ARCHIVEWEBPAGE_TIMEOUT", getEnvInt("TIMEOUT", 60)) * 1000
  );
  console.log("stopping archiveweb.page recording...");

  let browser = null;
  try {
    const state = readRecordingState();
    const chromeSessionDir = pickChromeSessionDir(chromeDirCandidates);
    if (!chromeSessionDir) {
      throw new Error("Chrome target_id.txt is missing for this snapshot");
    }
    const connection = await chromeUtils.connectToPage({
      chromeSessionDir,
      timeoutMs,
      requireTargetId: true,
      puppeteer,
    });
    browser = connection.browser;
    const connectedTargetId = chromeUtils.getTargetIdFromPage(connection.page);
    if (connectedTargetId !== state.snapshotTargetId) {
      throw new Error(
        `Chrome target identity changed: expected ${state.snapshotTargetId}, got ${connectedTargetId}`
      );
    }

    const helperPage = await openAwpHelperTab(
      browser,
      state.extensionId,
      timeoutMs
    );
    try {
      await stopExactRecording(helperPage, state, timeoutMs);
    } finally {
      await helperPage.close({ runBeforeUnload: false }).catch(() => {});
    }

    const destPath = path.join(outputDir, OUTPUT_FILENAME);
    const outputSize = await downloadExactWacz(
      browser,
      state.extensionId,
      state.collId,
      destPath,
      timeoutMs
    );
    const elapsed = Date.now() - startedAt;
    if (elapsed > budgetMs) {
      console.error(
        `[archivewebpage] stop hook took ${elapsed}ms (budget=${budgetMs}ms, wacz size=${outputSize} bytes)`
      );
    }
    console.log(
      `archiveweb.page recording saved to ${path.relative(
        SNAP_DIR,
        destPath
      )} (${outputSize} bytes)`
    );
    emitArchiveResultRecord("succeeded", `${PLUGIN_DIR}/${OUTPUT_FILENAME}`, {
      output_size: outputSize,
    });
    process.exit(0);
  } catch (error) {
    const detail = `${error.name || "Error"}: ${error.message || error}`;
    console.error(`ERROR: ${detail}`);
    emitArchiveResultRecord("failed", detail);
    process.exit(1);
  } finally {
    if (browser) await browser.disconnect().catch(() => {});
  }
}

main().catch((error) => {
  console.error(`Fatal error: ${error.message || error}`);
  process.exit(1);
});
