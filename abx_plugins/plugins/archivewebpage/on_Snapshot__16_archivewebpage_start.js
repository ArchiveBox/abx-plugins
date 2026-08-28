#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Start an ArchiveWeb.page WACZ recording before the page navigates.
 *
 * Foreground hook that runs after the chrome tab is ready (priority 16) and
 * after pre-load extension setup hooks (12-15) but BEFORE chrome_navigate
 * (30). The page is still on about:blank, so the recorder gets every request
 * including the very first navigation.
 *
 * The hook drives AWP's popup-port message API from a directly-spawned popup
 * tab (Target.createTarget at chrome-extension://${AWP_ID}/popup.html, not
 * browser.newPage()) so AWP does not auto-attach a child recorder to it. It
 * then asks AWP to create a fresh collection and start recording against the
 * existing chrome plugin tab. The matching stop hook
 * (on_Snapshot__65_archivewebpage_stop.js) ends recording and saves the WACZ.
 *
 * Latency target: ARCHIVEWEBPAGE_HOOK_BUDGET_MS (default 2s). Almost all the
 * time is spent on puppeteer/CDP setup and the AWP popup-port round trip; the
 * recorder hand-off itself is small.
 */

const fs = require("fs");
const path = require("path");

const {
  ensureNodeModuleResolution,
  loadConfig,
  parseArgs,
  getEnv,
  getEnvBool,
  getEnvInt,
  emitArchiveResultRecord,
  writeFileAtomic,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);

const chromeUtils = require("../chrome/chrome_utils.js");
const puppeteer = chromeUtils.resolvePuppeteerModule();
const {
  resolveAwpExtension,
  getChromeTabIdForPage,
  openAwpHelperTab,
  resolveChromeDirs,
  pickChromeSessionDir,
} = require("./awp_internal.js");

const hookConfig = loadConfig();
const {
  outputDir,
  candidates: chromeDirCandidates,
  crawlChromeDir,
} = resolveChromeDirs(process.cwd(), hookConfig.CRAWL_DIR);
const RECORDING_STATE_PATH = path.join(outputDir, "recording.json");
process.chdir(path.resolve(process.cwd()));

async function runStartHandshake(
  browser,
  extensionId,
  targetTabId,
  url,
  options
) {
  const { autorun, collectionTitle, timeoutMs } = options;
  const helperPage = await openAwpHelperTab(browser, extensionId, timeoutMs);
  try {
    const result = await helperPage.evaluate(
      async ({ tabId, url, autorun, collectionTitle, timeoutMs }) => {
        let handshakeStage = "connect";
        function withTimeout(promise, ms, message) {
          return Promise.race([
            promise,
            new Promise((_, reject) =>
              setTimeout(
                () => reject(new Error(`${message} (${handshakeStage})`)),
                ms
              )
            ),
          ]);
        }

        return await withTimeout(
          (async () => {
            const port = document.querySelector("wr-popup-viewer")?.port;
            if (!port) throw new Error("AWP popup port is not ready");
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

            port.postMessage({ type: "startUpdates", tabId });
            handshakeStage = "collections";
            await waitFor((m) => m && m.type === "collections", "collections");

            // Always create a fresh collection per snapshot so the resulting
            // WACZ contains only requests from this archive run and not every
            // page AWP ever recorded in this Chrome profile.
            port.postMessage({ type: "newColl", title: collectionTitle });
            handshakeStage = "new collection";
            const created = await waitFor(
              (m) => m && m.type === "collections" && m.collId,
              "new collection"
            );
            const collId = created.collId;
            if (!collId) {
              throw new Error("AWP did not return a collection id");
            }

            port.postMessage({
              type: "startRecording",
              collId,
              url,
              autorun: !!autorun,
            });

            handshakeStage = "recording status";
            const status = await waitFor(
              (message) =>
                message?.type === "status" &&
                (message.recording === true || Boolean(message.failureMsg)),
              "recording status",
              timeoutMs
            );

            return { collId, status };
          })(),
          timeoutMs,
          "AWP popup-port handshake"
        );
      },
      { tabId: targetTabId, url, autorun, collectionTitle, timeoutMs }
    );
    return { ...result, targetTabId };
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
    console.error("Usage: on_Snapshot__16_archivewebpage_start.js --url=<url>");
    process.exit(1);
  }

  if (!getEnvBool("ARCHIVEWEBPAGE_ENABLED", true)) {
    emitArchiveResultRecord("skipped", "ARCHIVEWEBPAGE_ENABLED=False");
    process.exit(0);
  }

  const budgetMs = getEnvInt("ARCHIVEWEBPAGE_HOOK_BUDGET_MS", 2000);
  const overallTimeoutMs = Math.max(
    budgetMs * 3,
    getEnvInt("CHROME_TIMEOUT", getEnvInt("TIMEOUT", 60)) * 1000
  );

  console.log("starting archiveweb.page recording...");
  fs.rmSync(RECORDING_STATE_PATH, { force: true });

  const chromeSessionDir = pickChromeSessionDir(chromeDirCandidates);
  if (!chromeSessionDir) {
    const error =
      "No chrome session dir candidate found (chrome plugin must run first)";
    console.error(`ERROR: ${error}`);
    emitArchiveResultRecord("failed", error);
    process.exit(1);
  }

  const { id: extensionId } = resolveAwpExtension(
    chromeSessionDir,
    crawlChromeDir
  );
  if (!extensionId) {
    const error =
      "archiveweb.page extension is not loaded (chrome plugin must run first with chromewebstore extension installed)";
    console.error(`ERROR: ${error}`);
    emitArchiveResultRecord("failed", error);
    process.exit(1);
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
    const snapshotTargetId = chromeUtils.getTargetIdFromPage(page);
    if (!snapshotTargetId) {
      throw new Error("Chrome target_id.txt did not resolve to a page");
    }

    const chromeTabId = await getChromeTabIdForPage(
      browser,
      page,
      extensionId,
      overallTimeoutMs
    );
    if (!chromeTabId) {
      throw new Error("Could not resolve chrome.tabs id for snapshot tab");
    }

    const handshake = await runStartHandshake(
      browser,
      extensionId,
      chromeTabId,
      url,
      {
        autorun: getEnvBool("ARCHIVEWEBPAGE_AUTORUN_BEHAVIORS", false),
        collectionTitle: `${getEnv(
          "ARCHIVEWEBPAGE_COLLECTION_TITLE",
          "abx-dl"
        )} - ${url}`,
        timeoutMs: overallTimeoutMs,
      }
    );
    await page.bringToFront();
    if (handshake.status?.failureMsg) {
      throw new Error(
        `AWP recorder attach failed: ${handshake.status.failureMsg}`
      );
    }
    if (handshake.status?.recording !== true) {
      throw new Error("AWP recorder did not confirm recording=true");
    }

    writeFileAtomic(
      RECORDING_STATE_PATH,
      `${JSON.stringify({
        version: 1,
        extensionId,
        snapshotTargetId,
        chromeTabId,
        collId: handshake.collId,
      })}\n`
    );

    const elapsed = Date.now() - startedAt;
    if (elapsed > budgetMs) {
      console.error(
        `[archivewebpage] WARN: start hook took ${elapsed}ms (budget=${budgetMs}ms)`
      );
    }
    console.log(
      `archiveweb.page recording started (coll=${handshake.collId}, tab=${handshake.targetTabId}, ${elapsed}ms)`
    );
    emitArchiveResultRecord(
      "succeeded",
      `recording started coll=${handshake.collId} tab=${handshake.targetTabId}`
    );
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
