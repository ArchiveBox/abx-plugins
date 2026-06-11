#!/usr/bin/env node
/**
 * Launch a shared Chromium browser session for the entire crawl.
 *
 * This runs once per crawl and keeps Chromium alive for all snapshots to share.
 * Each snapshot creates its own tab via on_Snapshot__10_chrome_tab.daemon.bg.js.
 *
 * Extension caches are loaded after startup through CDP so Chrome assigns and
 * publishes the real runtime extension IDs in browser.json.
 *
 * Usage: on_CrawlSetup__90_chrome_launch.daemon.bg.js
 * Output: Writes to current directory (executor creates chrome/ dir):
 *   - cdp_url.txt: WebSocket/HTTP URL for CDP connection
 *   - chrome.pid: Chromium process ID (for cleanup)
 *   - browser.json: Browser setup metadata and loaded extensions
 *
 * Environment variables:
 *     NODE_MODULES_DIR: Path to node_modules directory for module resolution
 *     CHROME_BINARY: Path to Chromium binary (falls back to auto-detection)
 *     CHROME_RESOLUTION: Page resolution (default: 1440,2000)
 *     CHROME_HEADLESS: Run in headless mode (default: true)
 *     CHROME_CHECK_SSL_VALIDITY: Whether to check SSL certificates (default: true)
 *     CHROME_EXTENSIONS_DIR: Directory containing Chrome extensions
 */


// Cleanup can SIGTERM the process immediately after spawn; remember early
// signals and replay them to the hook-specific cleanup handler once it exists.
let __abxEarlyShutdownSignal = null;
function __abxRememberEarlyShutdown(signal) {
  if (__abxEarlyShutdownSignal === null) {
    __abxEarlyShutdownSignal = signal;
  }
}
function __abxInstallShutdownHandler(handler) {
  process.removeAllListeners("SIGTERM");
  process.removeAllListeners("SIGINT");
  process.on("SIGTERM", () => handler("SIGTERM"));
  process.on("SIGINT", () => handler("SIGINT"));
  if (__abxEarlyShutdownSignal !== null) {
    const signal = __abxEarlyShutdownSignal;
    __abxEarlyShutdownSignal = null;
    setImmediate(() => handler(signal));
  }
}
process.on("SIGTERM", () => __abxRememberEarlyShutdown("SIGTERM"));
process.on("SIGINT", () => __abxRememberEarlyShutdown("SIGINT"));

const fs = require("fs");
const path = require("path");
const { ensureNodeModuleResolution, loadConfig } = require("../base/utils.js");
ensureNodeModuleResolution(module);
const {
  acquireSessionLock,
  ensureChromeSession,
  closeBrowserInChromeSession,
  getChromeSessionOptionsFromConfig,
  killZombieChrome,
  waitForChromeLaunchPrerequisites,
} = require("./chrome_utils.js");

// Extractor metadata
const PLUGIN_NAME = "chrome_launch";
const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const CRAWL_DIR = path.resolve((hookConfig.CRAWL_DIR || ".").trim());
const chromeSessionOptions = getChromeSessionOptionsFromConfig(hookConfig);
const CHROME_USER_DATA_DIR = chromeSessionOptions.CHROME_USER_DATA_DIR;
const CHROME_TIMEOUT_MS = chromeSessionOptions.timeoutMs;
const CHROME_INSTALL_TIMEOUT_MS =
  (Number(hookConfig.CHROME_INSTALL_TIMEOUT) || 300) * 1000;
const CHROME_CDP_URL = chromeSessionOptions.CHROME_CDP_URL;
const CHROME_IS_LOCAL = chromeSessionOptions.CHROME_IS_LOCAL;
const CHROME_KEEPALIVE = hookConfig.CHROME_KEEPALIVE === true;
const CHROME_ISOLATION =
  String(hookConfig.CHROME_ISOLATION || "crawl").toLowerCase() === "snapshot"
    ? "snapshot"
    : "crawl";
const OUTPUT_DIR = path.join(CRAWL_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const CHROME_BINARY = String(hookConfig.CHROME_BINARY || "chromium")
  .split("/")
  .at(-1);
const PERSONA_DIR =
  CHROME_USER_DATA_DIR ||
  path.join(
    hookConfig.PERSONAS_DIR || "~/.config/abx/personas",
    hookConfig.ACTIVE_PERSONA || "Default"
  );

// Global state for cleanup
let chromePid = null;
let chromeCdpUrl = null;
let chromeProcessIsLocal = CHROME_IS_LOCAL;
let shouldCloseOnCleanup = false;
let puppeteer = null;
let cleanupPromise = null;
let launchInProgress = false;
let cleanupRequestedDuringLaunch = false;

function getPortFromCdpUrl(cdpUrl) {
  if (!cdpUrl) return null;
  const match = cdpUrl.match(/:(\d+)\/devtools\//);
  return match ? match[1] : null;
}

// Cleanup handler for SIGTERM
async function cleanup() {
  if (cleanupPromise) {
    return cleanupPromise;
  }
  if (launchInProgress && !chromeCdpUrl) {
    cleanupRequestedDuringLaunch = true;
    console.error(
      "[*] Deferring chrome cleanup until launch publishes a CDP session"
    );
    return;
  }
  cleanupPromise = (async () => {
    if (shouldCloseOnCleanup) {
      console.log(`shutting down ${CHROME_BINARY} cleanly...`);
      const closed = await closeBrowserInChromeSession({
        cdpUrl: chromeCdpUrl,
        pid: chromePid,
        outputDir: OUTPUT_DIR,
        puppeteer,
        processIsLocal: chromeProcessIsLocal,
      });
      if (!closed) {
        console.error(
          `${CHROME_BINARY} cleanup did not fully stop the browser process tree`
        );
        process.exit(1);
      }
      await killZombieChrome(CRAWL_DIR, {
        quiet: true,
        excludeCurrentRuntimeDirs: false,
        CHROME_USER_DATA_DIR,
      });
      console.log(`${CHROME_BINARY} exited successfully`);
      console.log(JSON.stringify({ succeeded: true, skipped: false })); // we launched and we killed it (nothing was skipped)
    } else {
      if (!chromeCdpUrl) {
        console.error(
          `${CHROME_BINARY} cleanup requested before a CDP session was published`
        );
        process.exit(1);
      }
      console.log(`leaving ${CHROME_BINARY} running (CHROME_KEEPALIVE=True)`);
      console.log(
        JSON.stringify({
          succeeded: true,
          skipped: chromeCdpUrl ? true : false,
        })
      ); // we didn't launch it (we connected over CDP), and we didn't kill it, so we skipped basically the whole hook
    }
    process.exit(0);
  })();
  return cleanupPromise;
}

// Register signal handlers
__abxInstallShutdownHandler(cleanup);

async function main() {
  let releaseLock = null;

  try {
    console.error("waiting for other chrome instances to finish launching...");
    releaseLock = await acquireSessionLock(
      path.join(OUTPUT_DIR, ".launch.lock")
    );
    const isolation = CHROME_ISOLATION;
    const keepAlive = CHROME_KEEPALIVE;
    const cdpUrlOverride = CHROME_CDP_URL;
    chromeProcessIsLocal = CHROME_IS_LOCAL;
    const prerequisiteTimeoutMs = Math.max(
      CHROME_TIMEOUT_MS,
      CHROME_INSTALL_TIMEOUT_MS
    );

    if (isolation === "snapshot") {
      console.error(
        "skipping crawl-scoped browser launch (CHROME_ISOLATION=snapshot)"
      );
      releaseLock();
      releaseLock = null;
      process.exit(0);
    }

    console.error(`waiting for ${CHROME_BINARY} to be installed...`);
    const prerequisites = await waitForChromeLaunchPrerequisites({
      requireLocalBinary: !cdpUrlOverride && chromeProcessIsLocal,
      timeoutMs: prerequisiteTimeoutMs,
    });
    puppeteer = prerequisites.puppeteer;

    console.error(
      cdpUrlOverride
        ? `connecting ${CHROME_BINARY} ${cdpUrlOverride}...`
        : `launching ${CHROME_BINARY} ${PERSONA_DIR}...`
    );
    launchInProgress = true;
    const session = await ensureChromeSession({
      outputDir: OUTPUT_DIR,
      puppeteer,
      binary: prerequisites.binary || null,
      ...chromeSessionOptions,
      CHROME_IS_LOCAL: chromeProcessIsLocal,
      CHROME_CDP_URL: cdpUrlOverride,
    });
    launchInProgress = false;

    chromePid = session.pid;
    chromeCdpUrl = session.cdpUrl;
    shouldCloseOnCleanup = !keepAlive;

    for (const extension of session.installedExtensions) {
      console.error(
        `loading extension: ${
          extension.name || extension.id || extension.unpacked_path
        }...`
      );
    }
    if (session.reusedExisting) {
      console.error(`reusing live ${CHROME_BINARY} session in ${OUTPUT_DIR}`);
    }

    console.error(`[+] ${CHROME_BINARY} session started`);
    console.error(`[+] CDP URL: ${chromeCdpUrl}`);
    releaseLock();
    releaseLock = null;
    // Background hook stdout is the parent scheduler's readiness contract.
    // Emit only after browser.json/cdp_url.txt exist and the launch lock is
    // released so snapshot hooks can safely consume the shared Chrome session.
    console.log(
      JSON.stringify({
        type: "ProcessReady",
        plugin: PLUGIN_DIR,
        hook: "on_CrawlSetup__90_chrome_launch.daemon.bg",
        cdp_url: chromeCdpUrl,
        pid: chromePid || null,
      })
    );

    if (cleanupRequestedDuringLaunch) {
      cleanupRequestedDuringLaunch = false;
      console.error(
        `[*] Running deferred ${CHROME_BINARY} cleanup requested during launch`
      );
      await cleanup();
      return;
    }

    if (!shouldCloseOnCleanup) {
      process.exit(0);
    }

    console.error(
      `${CHROME_BINARY} running pid=${
        chromePid || "remote"
      }, waiting for cleanup...`
    );
    setInterval(() => {}, 1000000);
  } catch (e) {
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
    launchInProgress = false;
    console.error(`ERROR: ${e.name}: ${e.message}`);
    process.exit(1);
  }
}

main().catch((e) => {
  console.error(`Fatal error: ${e.message}`);
  process.exit(1);
});
