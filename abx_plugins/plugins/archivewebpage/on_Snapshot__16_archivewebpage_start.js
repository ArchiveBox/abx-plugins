#!/usr/bin/env node
/**
 * Start an ArchiveWeb.page WACZ recording before the page navigates.
 *
 * Foreground hook that runs after the chrome tab is ready (priority 11) and
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

const path = require("path");

const {
  ensureNodeModuleResolution,
  loadConfig,
  parseArgs,
  getEnv,
  getEnvBool,
  getEnvInt,
  emitArchiveResultRecord,
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
const { candidates: chromeDirCandidates, crawlChromeDir } = resolveChromeDirs(
  process.cwd(),
  hookConfig.CRAWL_DIR
);
process.chdir(path.resolve(process.cwd()));

async function runStartHandshake(
  browser,
  extensionId,
  targetTabId,
  url,
  options
) {
  const { autorun, collectionTitle, timeoutMs } = options;
  let helperPage = await openAwpHelperTab(browser, extensionId);
  let result = null;
  let lastError = null;

  const handshake = async () =>
    helperPage.evaluate(
      async ({ tabId, url, autorun, collectionTitle, timeoutMs }) => {
        function withTimeout(promise, ms, message) {
          return Promise.race([
            promise,
            new Promise((_, reject) =>
              setTimeout(() => reject(new Error(message)), ms)
            ),
          ]);
        }

        return await withTimeout(
          (async () => {
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

            port.postMessage({ type: "startUpdates", tabId });
            await waitFor((m) => m && m.type === "collections", "collections");

            // Always create a fresh collection per snapshot so the resulting
            // WACZ contains only requests from this archive run and not every
            // page AWP ever recorded in this Chrome profile.
            port.postMessage({ type: "newColl", title: collectionTitle });
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

            // Wait for a status message confirming recording=true or surfacing
            // failureMsg from chrome.debugger.attach.
            let status = null;
            const deadline = Date.now() + 4000;
            while (Date.now() < deadline) {
              try {
                const remaining = Math.max(250, deadline - Date.now());
                status = await waitFor(
                  (m) => m && m.type === "status",
                  "recorder status",
                  remaining
                );
              } catch (error) {
                break;
              }
              if (status && (status.recording === true || status.failureMsg)) {
                break;
              }
            }

            try {
              port.disconnect();
            } catch (error) {}

            return { collId, status };
          })(),
          timeoutMs,
          "AWP popup-port handshake"
        );
      },
      { tabId: targetTabId, url, autorun, collectionTitle, timeoutMs }
    );

  try {
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        result = await handshake();
        result.targetTabId = targetTabId;
        break;
      } catch (err) {
        const msg = err?.message || String(err);
        if (
          attempt < 2 &&
          (msg.includes("Execution context was destroyed") ||
            msg.includes("Target closed") ||
            msg.includes("Session closed") ||
            msg.includes("AWP popup-port handshake") ||
            msg.includes("timed out waiting for"))
        ) {
          console.error(
            `[archivewebpage] start handshake failed (${msg}), reopening helper popup and retrying`
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
  } finally {
    try {
      await helperPage.close({ runBeforeUnload: false });
    } catch (error) {}
  }

  if (!result) {
    throw new Error(
      lastError ? lastError.message || String(lastError) : "AWP start failed"
    );
  }
  return result;
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

  const chromeSessionDir = await waitForChromeSessionDir(
    chromeDirCandidates,
    Math.max(2000, budgetMs * 2)
  );
  if (!chromeSessionDir) {
    const error =
      "No chrome session dir candidate found (chrome plugin must run first)";
    console.error(`ERROR: ${error}`);
    emitArchiveResultRecord("failed", error);
    process.exit(1);
  }

  const { id: extensionId } = await waitForAwpExtension(
    chromeSessionDir,
    crawlChromeDir,
    Math.max(5000, budgetMs * 5)
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
        timeoutMs: Math.min(overallTimeoutMs, Math.max(10000, budgetMs * 5)),
      }
    );
    if (handshake.status?.failureMsg) {
      throw new Error(
        `AWP recorder attach failed: ${handshake.status.failureMsg}`
      );
    }
    if (handshake.status && handshake.status.recording !== true) {
      console.error(
        `[archivewebpage] WARN: recorder status did not confirm recording=true (status=${JSON.stringify(
          handshake.status
        )})`
      );
    }

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
