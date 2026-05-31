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
  getEnv,
  getEnvBool,
  getEnvInt,
  acquireSessionLock,
  waitForChromeSessionState,
  ensureChromeSession,
  closeBrowserInChromeSession,
  resolvePuppeteerModule,
} = require("./chrome_utils.js");
const puppeteer = resolvePuppeteerModule();

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || ".").trim());
const OUTPUT_DIR = path.join(SNAP_DIR, "chrome");
if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

let chromePid = null;
let chromeCdpUrl = null;
let chromeProcessIsLocal = getEnv("CHROME_CDP_URL", "")
  ? false
  : getEnvBool("CHROME_IS_LOCAL", true);
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
    const isolation =
      getEnv("CHROME_ISOLATION", "crawl").toLowerCase() === "snapshot"
        ? "snapshot"
        : "crawl";
    const keepAlive = getEnvBool("CHROME_KEEPALIVE", false);
    const cdpUrlOverride = getEnv("CHROME_CDP_URL", "");
    chromeProcessIsLocal = cdpUrlOverride
      ? false
      : getEnvBool("CHROME_IS_LOCAL", true);

    if (isolation === "crawl") {
      const crawlChromeDir = path.join(
        path.resolve(getEnv("CRAWL_DIR", ".")),
        "chrome"
      );
      // Probe for a *connectable* crawl-level session, not just a session
      // file with a cdpUrl string. ``waitForChromeSessionState`` without
      // ``requireConnectable: true`` returns as soon as the artifacts
      // exist, even if Chrome has since crashed or been killed (this is
      // what masked the rc51 cabbage failure where Chrome died mid-crawl
      // and every subsequent snapshot reported "No Chrome session found"
      // for ~60s before the throw). With liveness probing, a dead session
      // returns null fast and we fall through to relaunch instead of
      // failing the whole snapshot.
      const crawlSession = await waitForChromeSessionState(crawlChromeDir, {
        timeoutMs: getEnvInt("CHROME_TIMEOUT", 60) * 1000,
        requireConnectable: true,
        puppeteer,
      });
      if (crawlSession?.cdpUrl) {
        releaseLock();
        releaseLock = null;
        emitArchiveResultRecord("skipped", "CHROME_ISOLATION=crawl");
        process.exit(0);
      }
      // Crawl-level Chrome is dead or missing. Auto-relaunch it in the
      // crawl chrome dir so this snapshot — and any subsequent snapshots
      // in the same crawl — can use it. ``ensureChromeSession`` is
      // idempotent: it sweeps any stale session artifacts and spawns a
      // fresh Chromium (with the same persona/user-data-dir, so cookies
      // and downloaded files persist across the relaunch). Holding our
      // own .launch.lock here serializes concurrent snapshot-level
      // recovery attempts; ``ensureChromeSession`` also takes the
      // crawl-level .launch.lock internally so we don't race the
      // original on_CrawlSetup__90 daemon if it's still alive but slow.
      console.error(
        `[!] crawl-scoped ${CHROME_BINARY} session is gone, relaunching in ${crawlChromeDir}...`
      );
      const relaunched = await ensureChromeSession({
        outputDir: crawlChromeDir,
        puppeteer,
        processIsLocal: chromeProcessIsLocal,
        cdpUrl: cdpUrlOverride,
        timeoutMs: getEnvInt("CHROME_TIMEOUT", 60) * 1000,
      });
      console.error(
        `[+] relaunched crawl-scoped ${CHROME_BINARY} pid=${relaunched.pid || "remote"} cdp=${relaunched.cdpUrl.split("/devtools/")[0]}`
      );
      releaseLock();
      releaseLock = null;
      emitArchiveResultRecord(
        "succeeded",
        `relaunched crawl-scoped pid=${relaunched.pid || "external"} port=${relaunched.port || "?"}`
      );
      process.exit(0);
    }

    // console.log('launching local chrome browser...');
    console.log("chrome is launching...");
    const session = await ensureChromeSession({
      outputDir: OUTPUT_DIR,
      puppeteer,
      processIsLocal: chromeProcessIsLocal,
      cdpUrl: cdpUrlOverride,
      timeoutMs: getEnvInt("CHROME_TIMEOUT", 60) * 1000,
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
