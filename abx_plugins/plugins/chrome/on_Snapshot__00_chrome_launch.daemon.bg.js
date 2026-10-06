#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries node
// /// script
// ///
/**
 * Launch or adopt a snapshot-scoped Chrome session when CHROME_ISOLATION=snapshot.
 *
 * In crawl isolation this hook is a no-op readiness check. In snapshot isolation
 * it owns the browser lifecycle for this snapshot and publishes snapshot-scoped
 * session markers before the tab hook runs.
 */


const installShutdownHandler = require("../base/daemon_lifecycle.js").captureShutdownSignals();

const fs = require("fs");
const path = require("path");
const {
  ensureNodeModuleResolution,
  loadConfig,
  emitArchiveResultRecord,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);

const hookConfig = loadConfig();
const CHROME_ISOLATION =
  String(hookConfig.CHROME_ISOLATION || "crawl").toLowerCase() === "snapshot"
    ? "snapshot"
    : "crawl";
if (CHROME_ISOLATION === "crawl") {
  emitArchiveResultRecord("skipped", "CHROME_ISOLATION=crawl");
  process.exit(0);
}

const {
  acquireSessionLock,
  ensureChromeSession,
  closeBrowserInChromeSession,
  getChromeSessionOptionsFromConfig,
  findChromium,
  resolvePuppeteerModule,
} = require("./chrome_utils.js");

const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || ".").trim());
const chromeSessionOptions = getChromeSessionOptionsFromConfig(hookConfig);
const CHROME_CDP_URL = chromeSessionOptions.CHROME_CDP_URL;
const CHROME_IS_LOCAL = chromeSessionOptions.CHROME_IS_LOCAL;
const CHROME_KEEPALIVE = hookConfig.CHROME_KEEPALIVE === true;
const OUTPUT_DIR = path.join(SNAP_DIR, "chrome");
if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

let chromePid = null;
let chromeCdpUrl = null;
let chromeProcessIsLocal = CHROME_IS_LOCAL;
let shouldCloseOnCleanup = false;
let cleanupPromise = null;
let launchInProgress = true;
let cleanupRequestedDuringLaunch = false;
let activeChromeDir = OUTPUT_DIR;
let launchPublished = false;
let puppeteer = null;

function recordLaunch(pid) {
  chromePid = pid;
}

function recordCdpSession(session, shouldClose) {
  chromePid = session.pid;
  chromeCdpUrl = session.cdpUrl;
  shouldCloseOnCleanup = shouldClose;
}

function publishReadiness(session, shouldClose) {
  recordCdpSession(session, shouldClose);
  if (!launchPublished) {
    launchPublished = true;
    console.log("chrome session started");
  }
}

async function cleanup() {
  if (cleanupPromise) {
    return cleanupPromise;
  }
  if (launchInProgress && !chromeCdpUrl) {
    cleanupRequestedDuringLaunch = true;
    if (!chromeProcessIsLocal) {
      console.error(
        "[*] Deferring external Chrome cleanup until launch publishes its CDP session"
      );
      return;
    }
    const launchStatePublished = ["chrome.pid", "cdp_url.txt"].some(
      (fileName) => fs.existsSync(path.join(activeChromeDir, fileName))
    );
    if (launchStatePublished) {
      console.error(
        "[*] Cleaning up in-progress local Chrome from persisted state"
      );
    } else {
      console.error(
        "[*] Deferring chrome cleanup until launch publishes local session state"
      );
      return;
    }
  }
  const cleanupDuringLocalLaunch =
    cleanupRequestedDuringLaunch && chromeProcessIsLocal;
  cleanupPromise = (async () => {
    if (shouldCloseOnCleanup || cleanupDuringLocalLaunch) {
      const closed = await closeBrowserInChromeSession({
        cdpUrl: chromeCdpUrl,
        pid: chromePid,
        outputDir: activeChromeDir,
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

installShutdownHandler(cleanup);

async function main() {
  let releaseLock = null;

  try {
    releaseLock = await acquireSessionLock(
      path.join(OUTPUT_DIR, ".launch.lock")
    );
    const keepAlive = CHROME_KEEPALIVE;
    const cdpUrlOverride = CHROME_CDP_URL;
    chromeProcessIsLocal = CHROME_IS_LOCAL;

    puppeteer = resolvePuppeteerModule();
    const chromeBinaryPath = cdpUrlOverride ? null : findChromium();
    console.error("chrome is launching...");
    activeChromeDir = OUTPUT_DIR;
    launchInProgress = true;
    const session = await ensureChromeSession({
      outputDir: OUTPUT_DIR,
      puppeteer,
      binary: chromeBinaryPath,
      ...chromeSessionOptions,
      CHROME_IS_LOCAL: chromeProcessIsLocal,
      CHROME_CDP_URL: cdpUrlOverride,
      // Keep launch state available for cancellation without telling the
      // executor that this daemon is ready before all browser setup completes.
      onSpawn: (spawnedSession) => recordLaunch(spawnedSession.pid),
      onCdpReady: (readySession) =>
        recordCdpSession(readySession, !keepAlive),
    });
    launchInProgress = false;

    publishReadiness(session, !keepAlive);

    emitArchiveResultRecord(
      "succeeded",
      `pid=${chromePid || "external"} port=${session.port || "?"}`
    );
    releaseLock();
    releaseLock = null;

    if (cleanupRequestedDuringLaunch) {
      console.error("[*] Running deferred chrome cleanup requested during launch");
      await cleanup();
      return;
    }

    if (!shouldCloseOnCleanup) {
      process.exit(0);
    }

    setInterval(() => {}, 1000000);
  } catch (error) {
    if (cleanupRequestedDuringLaunch && cleanupPromise) {
      await cleanupPromise;
      return;
    }
    if (chromeCdpUrl || chromePid) {
      try {
        await closeBrowserInChromeSession({
          cdpUrl: chromeCdpUrl,
          pid: chromePid,
          outputDir: activeChromeDir,
          puppeteer,
          processIsLocal: chromeProcessIsLocal,
        });
      } catch (cleanupError) {}
    }
    if (releaseLock) {
      releaseLock();
    }
    launchInProgress = false;
    console.error(`ERROR: ${error.name}: ${error.message}`);
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(`Fatal error: ${error.message}`);
  process.exit(1);
});
