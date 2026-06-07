#!/usr/bin/env node
/**
 * Launch or adopt a snapshot-scoped Chrome session when CHROME_ISOLATION=snapshot.
 *
 * In crawl isolation this hook is a no-op readiness check. In snapshot isolation
 * it owns the browser lifecycle for this snapshot and publishes snapshot-scoped
 * session markers before the tab hook runs.
 */

const fs = require("fs");
const path = require("path");
const {
  ensureNodeModuleResolution,
  loadConfig,
  emitArchiveResultRecord,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);
const {
  acquireSessionLock,
  waitForChromeSessionState,
  ensureChromeSession,
  closeBrowserInChromeSession,
  getChromeSessionOptionsFromConfig,
  resolvePuppeteerModule,
} = require("./chrome_utils.js");
const puppeteer = resolvePuppeteerModule();

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || ".").trim());
const CRAWL_DIR = path.resolve((hookConfig.CRAWL_DIR || ".").trim());
const chromeSessionOptions = getChromeSessionOptionsFromConfig(hookConfig);
const CHROME_USER_DATA_DIR = chromeSessionOptions.CHROME_USER_DATA_DIR;
const CHROME_TIMEOUT_MS = chromeSessionOptions.timeoutMs;
const CHROME_CDP_URL = chromeSessionOptions.CHROME_CDP_URL;
const CHROME_IS_LOCAL = chromeSessionOptions.CHROME_IS_LOCAL;
const CHROME_KEEPALIVE = hookConfig.CHROME_KEEPALIVE === true;
const CHROME_ISOLATION =
  String(hookConfig.CHROME_ISOLATION || "crawl").toLowerCase() === "snapshot"
    ? "snapshot"
    : "crawl";
const OUTPUT_DIR = path.join(SNAP_DIR, "chrome");
// Tag for log lines emitted by the auto-relaunch path — mirrors the
// CrawlSetup hook's CHROME_BINARY const so messages have a consistent
// "chromium" / "chrome" prefix regardless of how the binary was resolved.
const CHROME_BINARY = String(hookConfig.CHROME_BINARY || "chromium")
  .split("/")
  .at(-1);
if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

let chromePid = null;
let chromeCdpUrl = null;
let chromeProcessIsLocal = CHROME_IS_LOCAL;
let shouldCloseOnCleanup = false;
let cleanupPromise = null;

async function cleanup() {
  if (cleanupPromise) {
    return cleanupPromise;
  }
  cleanupPromise = (async () => {
    if (shouldCloseOnCleanup) {
      const closed = await closeBrowserInChromeSession({
        cdpUrl: chromeCdpUrl,
        pid: chromePid,
        outputDir: OUTPUT_DIR,
        puppeteer,
        processIsLocal: chromeProcessIsLocal,
      });
      if (!closed) {
        console.error(
          "Chrome cleanup did not fully stop the browser process tree"
        );
        process.exit(1);
      }
    }
    process.exit(0);
  })();
  return cleanupPromise;
}

process.on("SIGTERM", cleanup);
process.on("SIGINT", cleanup);

async function main() {
  let releaseLock = null;

  try {
    releaseLock = await acquireSessionLock(
      path.join(OUTPUT_DIR, ".launch.lock")
    );
    const isolation = CHROME_ISOLATION;
    const keepAlive = CHROME_KEEPALIVE;
    const cdpUrlOverride = CHROME_CDP_URL;
    chromeProcessIsLocal = CHROME_IS_LOCAL;

    if (isolation === "crawl") {
      const crawlChromeDir = path.join(CRAWL_DIR, "chrome");
      // Probe with requireConnectable so a dead crawl-scoped Chrome
      // returns null fast (a stale session file alone isn't enough).
      const crawlSession = await waitForChromeSessionState(crawlChromeDir, {
        timeoutMs: CHROME_TIMEOUT_MS,
        requireConnectable: true,
        puppeteer,
      });
      if (crawlSession?.cdpUrl) {
        releaseLock();
        releaseLock = null;
        emitArchiveResultRecord("skipped", "CHROME_ISOLATION=crawl");
        process.exit(0);
      }
      // Crawl Chrome is dead — relaunch it shared so subsequent snapshots
      // can use it. ensureChromeSession is idempotent (sweeps stale
      // artifacts, takes the crawl-level .launch.lock internally).
      console.error(
        `[!] crawl-scoped ${CHROME_BINARY} session is gone, relaunching in ${crawlChromeDir}...`
      );
      const relaunched = await ensureChromeSession({
        outputDir: crawlChromeDir,
        puppeteer,
        ...chromeSessionOptions,
        CHROME_IS_LOCAL: chromeProcessIsLocal,
        CHROME_CDP_URL: cdpUrlOverride,
      });
      console.error(
        `[+] relaunched crawl-scoped ${CHROME_BINARY} pid=${
          relaunched.pid || "remote"
        } cdp=${relaunched.cdpUrl.split("/devtools/")[0]}`
      );
      releaseLock();
      releaseLock = null;
      emitArchiveResultRecord(
        "succeeded",
        `relaunched crawl-scoped pid=${relaunched.pid || "external"} port=${
          relaunched.port || "?"
        }`
      );
      process.exit(0);
    }

    // console.log('launching local chrome browser...');
    console.log("chrome is launching...");
    const session = await ensureChromeSession({
      outputDir: OUTPUT_DIR,
      puppeteer,
      ...chromeSessionOptions,
      CHROME_IS_LOCAL: chromeProcessIsLocal,
      CHROME_CDP_URL: cdpUrlOverride,
    });

    chromePid = session.pid;
    chromeCdpUrl = session.cdpUrl;
    shouldCloseOnCleanup = !keepAlive;

    emitArchiveResultRecord(
      "succeeded",
      `pid=${chromePid || "external"} port=${session.port || "?"}`
    );
    releaseLock();
    releaseLock = null;

    if (!shouldCloseOnCleanup) {
      process.exit(0);
    }

    setInterval(() => {}, 1000000);
  } catch (error) {
    if (chromeCdpUrl || chromePid) {
      try {
        await closeBrowserInChromeSession({
          cdpUrl: chromeCdpUrl,
          pid: chromePid,
          outputDir: OUTPUT_DIR,
          puppeteer,
          processIsLocal: chromeProcessIsLocal,
        });
      } catch (cleanupError) {}
    }
    if (releaseLock) {
      releaseLock();
    }
    console.error(`ERROR: ${error.name}: ${error.message}`);
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(`Fatal error: ${error.message}`);
  process.exit(1);
});
