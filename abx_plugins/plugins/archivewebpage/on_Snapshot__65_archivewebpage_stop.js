#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Stop the ArchiveWeb.page recording and save the resulting WACZ.
 *
 * Foreground hook that runs after all chrome-dependent extractors (screenshot,
 * pdf, dom, mhtml, infiniscroll, etc.) but before the chrome tab is torn down
 * and before post-processing parser hooks.
 *
 * Everything start-hook state (extension id, snapshot tab id, AWP collection
 * id) is rederived here rather than persisted between hooks:
 *   - extension id comes from {chrome_plugin_dir}/browser.json via
 *     chromeUtils.readBrowserMetadata + findExtensionMetadataByName
 *   - snapshot tab id comes from chromeUtils.connectToPage's target id mapped
 *     through chrome.debugger.getTargets() inside the AWP service worker
 *   - the running recorder's collId is pulled off the {type:"status"} message
 *     AWP posts back on the popup-port after a startUpdates handshake
 *
 * Steps:
 *   1. open chrome-extension://${AWP_ID}/popup.html as a hidden helper tab
 *   2. send startUpdates to capture the active recorder's collId + status
 *   3. send stopRecording and wait for {recording: false}
 *   4. download the WACZ by navigating a dedicated tab to
 *      chrome-extension://${AWP_ID}/w/api/c/:coll/dl?format=wacz&pages=all
 *      with Chrome's download dir pointed at SNAP_DIR/archivewebpage
 *   5. emit the ArchiveResult JSONL record
 *
 * Latency target: ARCHIVEWEBPAGE_HOOK_BUDGET_MS (default 2s). For large
 * captures the WACZ build + download dominates, so we treat the budget as a
 * soft optimization goal and allow it to be exceeded for big WACZs.
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
  waitForAwpExtension,
  getChromeTabIdForPage,
  openAwpHelperTab,
  resolveChromeDirs,
  waitForChromeSessionDir,
} = require("./awp_internal.js");

const hookConfig = loadConfig();
const PLUGIN_DIR = path.basename(__dirname);
const OUTPUT_FILENAME = "archivewebpage.wacz";
const {
  outputDir,
  candidates: chromeDirCandidates,
  crawlChromeDir,
} = resolveChromeDirs(process.cwd(), hookConfig.CRAWL_DIR);
process.chdir(outputDir);
const SNAP_DIR = path.resolve(outputDir, "..");

async function moveAcrossMounts(src, dest) {
  try {
    await fs.promises.rename(src, dest);
  } catch (err) {
    // Chrome may download into the shared persona dir while abx-dl writes
    // snapshot output under /out. Docker users routinely mount those as
    // different filesystems, where rename(2) fails with EXDEV even though a
    // normal user-facing "move this downloaded artifact into the snapshot"
    // operation should still succeed.
    if (err && err.code === "EXDEV") {
      await fs.promises.copyFile(src, dest);
      await fs.promises.unlink(src);
      return;
    }
    throw err;
  }
}

async function stopAndCollectCollId(helperPage, targetTabId) {
  return helperPage.evaluate(
    async ({ tabId }) => {
      const port = chrome.runtime.connect({ name: "popup-port" });
      const recvQueue = [];
      port.onMessage.addListener((msg) => recvQueue.push(msg));

      function waitFor(predicate, label, ms = 2500) {
        return new Promise((resolve, reject) => {
          while (recvQueue.length) {
            const msg = recvQueue.shift();
            if (predicate(msg)) {
              resolve(msg);
              return;
            }
          }
          const onMsg = (msg) => {
            if (predicate(msg)) {
              port.onMessage.removeListener(onMsg);
              resolve(msg);
            }
          };
          port.onMessage.addListener(onMsg);
          setTimeout(() => {
            port.onMessage.removeListener(onMsg);
            reject(new Error(`timed out waiting for ${label}`));
          }, ms);
        });
      }

      // startUpdates binds tabId in the popupHandler closure AND triggers
      // recorder.doUpdateStatus() if a recording is active on that tab. The
      // status payload includes the live recorder's collId.
      port.postMessage({ type: "startUpdates", tabId });
      await waitFor((m) => m && m.type === "collections", "collections");

      let collId = null;
      try {
        const initialStatus = await waitFor(
          (m) => m && m.type === "status",
          "initial status",
          1500
        );
        if (initialStatus && initialStatus.collId) {
          collId = initialStatus.collId;
        }
      } catch (error) {
        // No active recorder for this tab; nothing to download.
      }

      if (!collId) {
        try {
          port.disconnect();
        } catch (error) {}
        return { collId: null, drained: false, status: null };
      }

      port.postMessage({ type: "stopRecording" });

      // Drain pending fetches. AWP's detach() can wait up to 15s on big
      // captures; soft cap to 10s here.
      const drainDeadline = Date.now() + 10000;
      let finalStatus = null;
      while (Date.now() < drainDeadline) {
        let status = null;
        try {
          status = await waitFor(
            (m) => m && m.type === "status",
            "drain status",
            Math.min(2000, Math.max(250, drainDeadline - Date.now()))
          );
        } catch (error) {
          break;
        }
        if (status) finalStatus = status;
        if (status && status.recording === false) break;
      }

      try {
        port.disconnect();
      } catch (error) {}

      return {
        collId,
        drained: finalStatus?.recording === false,
        status: finalStatus,
      };
    },
    { tabId: targetTabId }
  );
}

async function sendStopAndDownload(
  browser,
  extensionId,
  targetTabId,
  destPath,
  timeoutMs
) {
  let helperPage = await openAwpHelperTab(browser, extensionId);
  let stopOutcome = null;
  let lastError = null;

  try {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        stopOutcome = await stopAndCollectCollId(helperPage, targetTabId);
        break;
      } catch (err) {
        const msg = err?.message || String(err);
        if (
          attempt === 0 &&
          (msg.includes("Execution context was destroyed") ||
            msg.includes("Target closed") ||
            msg.includes("Session closed"))
        ) {
          console.error(
            `[archivewebpage] stop evaluate destroyed mid-flight, reopening helper popup and retrying`
          );
          try {
            await helperPage.close({ runBeforeUnload: false });
          } catch (closeError) {}
          helperPage = await openAwpHelperTab(browser, extensionId);
          continue;
        }
        lastError = err;
        break;
      }
    }

    if (!stopOutcome) {
      throw new Error(
        lastError ? lastError.message || String(lastError) : "AWP stop failed"
      );
    }

    if (!stopOutcome.collId) {
      return {
        skipped: true,
        reason: "no active AWP recorder for snapshot tab",
      };
    }

    const chromeLaunchOptions = chromeUtils.resolveChromeLaunchOptions(hookConfig);
    const downloadDir = chromeLaunchOptions.CHROME_DOWNLOADS_DIR
      ? path.resolve(chromeLaunchOptions.CHROME_DOWNLOADS_DIR)
      : path.dirname(destPath);
    fs.mkdirSync(downloadDir, { recursive: true });
    const downloadFilename = `archivewebpage-${process.pid}-${Date.now()}`;
    const dlUrl = `chrome-extension://${extensionId}/w/api/c/${encodeURIComponent(
      stopOutcome.collId
    )}/dl?format=wacz&pages=all&filename=${encodeURIComponent(
      downloadFilename
    )}`;

    await chromeUtils
      .setBrowserDownloadBehavior({
        page: helperPage,
        downloadPath: downloadDir,
      })
      .catch((error) => {
        console.error(
          `[archivewebpage] download dir setup failed: ${
            error.message || error
          }`
        );
      });

    const beforeFiles = new Set(fs.readdirSync(downloadDir));

    // Open a dedicated download tab directly at the WACZ URL via
    // Target.createTarget. Going through createTarget (rather than newPage()
    // + page.goto) keeps the navigation single-step so Chrome cleanly switches
    // into download mode without us racing against an about:blank intermediate.
    const dlBrowserSession = await browser
      .target()
      .createCDPSession()
      .catch(() => null);
    if (dlBrowserSession) {
      try {
        await dlBrowserSession.send("Target.createTarget", { url: dlUrl });
      } catch (error) {
        console.error(
          `[archivewebpage] download tab createTarget failed: ${
            error.message || error
          }`
        );
      } finally {
        try {
          await dlBrowserSession.detach();
        } catch (error) {}
      }
    }

    let waczPath = null;
    const downloadDeadline = Date.now() + Math.min(timeoutMs, 8000);
    while (Date.now() < downloadDeadline) {
      const after = fs.readdirSync(downloadDir);
      const newFiles = after.filter(
        (n) => !beforeFiles.has(n) && n.toLowerCase().endsWith(".wacz")
      );
      const candidate = newFiles
        .map((n) => path.join(downloadDir, n))
        .find((p) => {
          if (fs.existsSync(`${p}.crdownload`)) return false;
          try {
            return fs.statSync(p).size > 0;
          } catch (error) {
            return false;
          }
        });
      if (candidate) {
        waczPath = candidate;
        break;
      }
      await new Promise((r) => setTimeout(r, 200));
    }

    if (!waczPath) {
      throw new Error(
        `WACZ download did not produce a .wacz file in ${downloadDir} within ${
          downloadDeadline - Date.now() + Math.min(timeoutMs, 8000)
        }ms`
      );
    }

    if (path.resolve(waczPath) !== path.resolve(destPath)) {
      await moveAcrossMounts(waczPath, destPath);
    }
    const stat = fs.statSync(destPath);
    return {
      skipped: false,
      size: stat.size,
      collId: stopOutcome.collId,
      drained: stopOutcome.drained,
    };
  } finally {
    try {
      await helperPage.close({ runBeforeUnload: false });
    } catch (error) {}
  }
}

async function main() {
  const startedAt = Date.now();
  const args = parseArgs();
  const url = args.url;

  if (!url) {
    console.error("Usage: on_Snapshot__65_archivewebpage_stop.js --url=<url>");
    process.exit(1);
  }

  if (!getEnvBool("ARCHIVEWEBPAGE_ENABLED", true)) {
    emitArchiveResultRecord("skipped", "ARCHIVEWEBPAGE_ENABLED=False");
    process.exit(0);
  }

  const budgetMs = getEnvInt("ARCHIVEWEBPAGE_HOOK_BUDGET_MS", 2000);
  const overallTimeoutSeconds = getEnvInt(
    "ARCHIVEWEBPAGE_TIMEOUT",
    getEnvInt("TIMEOUT", 60)
  );
  const overallTimeoutMs = Math.max(budgetMs * 3, overallTimeoutSeconds * 1000);

  console.log("stopping archiveweb.page recording...");

  const chromeSessionDir = await waitForChromeSessionDir(
    chromeDirCandidates,
    Math.max(1000, budgetMs)
  );
  if (!chromeSessionDir) {
    emitArchiveResultRecord(
      "skipped",
      "no chrome session dir candidate (start hook did not run)"
    );
    process.exit(0);
  }

  const { id: extensionId } = await waitForAwpExtension(
    chromeSessionDir,
    crawlChromeDir,
    Math.min(overallTimeoutMs, Math.max(30000, budgetMs * 10))
  );
  if (!extensionId) {
    emitArchiveResultRecord(
      "skipped",
      "archiveweb.page extension not loaded (start hook did not run)"
    );
    process.exit(0);
  }

  let browser = null;
  try {
    const connection = await chromeUtils.connectToPage({
      chromeSessionDir,
      timeoutMs: overallTimeoutMs,
      requireTargetId: true,
      puppeteer,
    });
    browser = connection.browser;
    const page = connection.page;

    const tabResolutionTimeoutMs = Math.min(
      overallTimeoutMs,
      Math.max(10000, budgetMs * 5)
    );
    const chromeTabId = await getChromeTabIdForPage(
      browser,
      page,
      extensionId,
      tabResolutionTimeoutMs
    );
    if (!chromeTabId) {
      throw new Error("Could not resolve chrome.tabs id for snapshot tab");
    }

    const destPath = path.join(outputDir, OUTPUT_FILENAME);
    const outcome = await sendStopAndDownload(
      browser,
      extensionId,
      chromeTabId,
      destPath,
      overallTimeoutMs
    );

    if (outcome.skipped) {
      emitArchiveResultRecord("skipped", outcome.reason);
      process.exit(0);
    }

    const elapsed = Date.now() - startedAt;
    if (elapsed > budgetMs) {
      console.error(
        `[archivewebpage] stop hook took ${elapsed}ms (budget=${budgetMs}ms, wacz size=${outcome.size} bytes)`
      );
    }
    console.log(
      `archiveweb.page recording saved to ${path.relative(
        SNAP_DIR,
        destPath
      )} (${outcome.size} bytes)`
    );
    emitArchiveResultRecord("succeeded", `${PLUGIN_DIR}/${OUTPUT_FILENAME}`, {
      output_size: outcome.size,
    });
    process.exit(0);
  } catch (error) {
    const detail = `${error.name || "Error"}: ${error.message || error}`;
    console.error(`ERROR: ${detail}`);
    emitArchiveResultRecord("failed", detail);
    process.exit(1);
  } finally {
    if (browser) {
      try {
        await browser.disconnect();
      } catch (error) {}
    }
  }
}

main().catch((error) => {
  console.error(`Fatal error: ${error.message || error}`);
  process.exit(1);
});
