#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Write Chrome screencast JPEGs for the admin live progress UI.
 *
 * Frames are crawl-scoped plugin output, shared by crawl setup and snapshot hooks.
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

const {
  ensureNodeModuleResolution,
  getEnv,
  getEnvBool,
  getEnvInt,
  loadConfig,
  parseArgs,
  emitArchiveResultRecord,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);

const {
  connectToBrowserEndpoint,
  getTargetIdFromTarget,
  resolvePuppeteerModule,
  waitForChromeSessionState,
} = require("../chrome/chrome_utils.js");

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const IS_CRAWL_SETUP = path
  .basename(process.argv[1] || "")
  .startsWith("on_CrawlSetup__");
const CRAWL_DIR_VALUE = (hookConfig.CRAWL_DIR || "").trim();
const CRAWL_DIR = path.resolve(CRAWL_DIR_VALUE || ".");
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || CRAWL_DIR).trim());
const CHROME_SESSION_DIR = path.join(
  IS_CRAWL_SETUP ? CRAWL_DIR : SNAP_DIR,
  "chrome"
);
const CHROME_ISOLATION =
  String(hookConfig.CHROME_ISOLATION || "crawl").toLowerCase() === "snapshot"
    ? "snapshot"
    : "crawl";
const LIVE_DIR = path.join(CRAWL_DIR, PLUGIN_DIR);
const LATEST_FRAME = path.join(LIVE_DIR, "latest.jpg");
const LIVE_FRAME_BUFFER = 10;
if (CRAWL_DIR_VALUE && !fs.existsSync(LIVE_DIR)) {
  fs.mkdirSync(LIVE_DIR, { recursive: true });
}
if (CRAWL_DIR_VALUE) {
  process.chdir(LIVE_DIR);
}

let browser = null;
let cdpSession = null;
let shuttingDown = false;
let frameCount = 0;
let nextFrameNumber = 1;
let keepFramesOnExit = 0;
let pendingFrame = null;
let pendingFrameTimer = null;

function emitResult(status, output) {
  emitArchiveResultRecord(status, output);
}

function clearPendingFrame() {
  pendingFrame = null;
  if (pendingFrameTimer) {
    clearTimeout(pendingFrameTimer);
    pendingFrameTimer = null;
  }
}

function writeFrameAtomic(filePath, data) {
  const tmpPath = path.join(
    path.dirname(filePath),
    `.${path.basename(filePath)}.${process.pid}.tmp`
  );
  fs.writeFileSync(tmpPath, data);
  fs.renameSync(tmpPath, filePath);
}

function cleanupOldFrames(maxFrames) {
  if (maxFrames <= 0 || frameCount % 5 !== 0) return;
  let frames = [];
  try {
    frames = fs
      .readdirSync(LIVE_DIR)
      .filter((name) => /^frame-\d+\.jpg$/.test(name))
      .sort();
  } catch (error) {
    return;
  }
  for (const name of frames.slice(0, Math.max(0, frames.length - maxFrames))) {
    try {
      fs.unlinkSync(path.join(LIVE_DIR, name));
    } catch (error) {}
  }
}

function cleanupFinalFrames(keepFrames) {
  let frames = [];
  try {
    frames = fs
      .readdirSync(LIVE_DIR)
      .filter((name) => /^frame-\d+\.jpg$/.test(name))
      .sort();
  } catch (error) {
    return 0;
  }
  const removeFrames = frames.slice(0, Math.max(0, frames.length - keepFrames));
  for (const name of removeFrames) {
    try {
      fs.unlinkSync(path.join(LIVE_DIR, name));
    } catch (error) {}
  }
  try {
    fs.unlinkSync(LATEST_FRAME);
  } catch (error) {}
  return Math.max(0, frames.length - removeFrames.length);
}

async function startScreencast() {
  if (!getEnvBool("CHROME_SCREENCAST_ENABLED", true)) {
    emitResult("skipped", "CHROME_SCREENCAST_ENABLED=False");
    process.exit(0);
  }
  if (!CRAWL_DIR_VALUE) {
    emitResult("skipped", "CRAWL_DIR is not set");
    process.exit(0);
  }

  console.error("chrome screencast starting");
  fs.mkdirSync(LIVE_DIR, { recursive: true });

  const timeoutMs =
    getEnvInt("CHROME_TIMEOUT", getEnvInt("TIMEOUT", 60)) * 1000;
  const chromeSession = await waitForChromeSessionState(CHROME_SESSION_DIR, {
    timeoutMs,
    requireTargetId: true,
  });
  if (!chromeSession?.cdpUrl) {
    throw new Error("No Chrome session found (chrome plugin must run first)");
  }
  const puppeteer = resolvePuppeteerModule();
  browser = await connectToBrowserEndpoint(puppeteer, chromeSession.cdpUrl, {
    // Keep the real Chrome viewport created by the chrome plugin.
    defaultViewport: null,
  });
  const targetId = chromeSession.targetId;
  const target = browser.targets().find(
    (candidate) =>
      candidate.type() === "page" &&
      getTargetIdFromTarget(candidate) === targetId
  );
  if (!target) {
    throw new Error(`Chrome page target ${targetId} not found`);
  }
  const page = await target.page();
  if (!page) {
    throw new Error(`Chrome target ${targetId} has no page handle`);
  }

  const quality = Math.max(
    1,
    Math.min(100, getEnvInt("CHROME_SCREENCAST_QUALITY", 65))
  );
  const fps = Math.max(1, Math.min(5, getEnvInt("CHROME_SCREENCAST_FPS", 1)));
  keepFramesOnExit = Math.max(
    0,
    getEnvInt("CHROME_SCREENCAST_KEEP", 0)
  );
  const rawScale = Number.parseFloat(
    getEnv("CHROME_SCREENCAST_SCALE", "0.5")
  );
  const screenshotScale = Number.isFinite(rawScale)
    ? Math.max(0.1, Math.min(1, rawScale))
    : 0.5;
  const minFrameMs = Math.floor(1000 / fps);
  let lastFrameAt = 0;

  const writeFrame = (jpeg) => {
    const framePath = path.join(
      LIVE_DIR,
      `frame-${String(nextFrameNumber).padStart(6, "0")}.jpg`
    );
    nextFrameNumber += 1;
    frameCount += 1;
    writeFrameAtomic(framePath, jpeg);
    writeFrameAtomic(LATEST_FRAME, jpeg);
    cleanupOldFrames(LIVE_FRAME_BUFFER);
  };
  const writeNativeFrame = (jpeg) => {
    writeFrame(jpeg);
    lastFrameAt = Date.now();
  };

  await page.bringToFront();
  cdpSession = await page.target().createCDPSession();
  await cdpSession.send("Page.enable");
  const metrics = await cdpSession.send("Page.getLayoutMetrics");
  const viewport = metrics.visualViewport || metrics.layoutViewport || {};
  const width = Math.max(1, Math.floor(viewport.clientWidth || 1440));
  const height = Math.max(1, Math.floor(viewport.clientHeight || 900));

  cdpSession.on("Page.screencastFrame", (frame) => {
    try {
      const now = Date.now();
      if (!shuttingDown && now - lastFrameAt >= minFrameMs) {
        clearPendingFrame();
        writeNativeFrame(Buffer.from(frame.data, "base64"));
      } else if (!shuttingDown) {
        pendingFrame = Buffer.from(frame.data, "base64");
        if (!pendingFrameTimer) {
          pendingFrameTimer = setTimeout(() => {
            pendingFrameTimer = null;
            if (!shuttingDown && pendingFrame) {
              writeNativeFrame(pendingFrame);
            }
            pendingFrame = null;
          }, Math.max(0, minFrameMs - (now - lastFrameAt)));
        }
      }
    } catch (error) {
      console.error(`WARN: failed to write screencast frame: ${error.message}`);
    } finally {
      if (cdpSession) {
        void cdpSession
          .send("Page.screencastFrameAck", { sessionId: frame.sessionId })
          .catch(() => {});
      }
    }
  });
  await cdpSession.send("Page.startScreencast", {
    format: "jpeg",
    quality,
    maxWidth: Math.max(1, Math.floor(width * screenshotScale)),
    maxHeight: Math.max(1, Math.floor(height * screenshotScale)),
    everyNthFrame: 1,
  });
  const initialFrame = await cdpSession.send("Page.captureScreenshot", {
    format: "jpeg",
    quality,
    optimizeForSpeed: true,
    fromSurface: true,
    captureBeyondViewport: false,
    clip: {
      x: Math.max(0, Math.floor(viewport.pageX || 0)),
      y: Math.max(0, Math.floor(viewport.pageY || 0)),
      width,
      height,
      scale: screenshotScale,
    },
  });
  writeNativeFrame(Buffer.from(initialFrame.data, "base64"));

  console.log("chrome screencast attached");
  console.error(`screencast frames: ${LIVE_DIR}`);
}

async function publishCrawlScreencastReady() {
  if (!getEnvBool("CHROME_SCREENCAST_ENABLED", true)) {
    console.error("Skipping crawl screencast (CHROME_SCREENCAST_ENABLED=False)");
    return;
  }
  if (CHROME_ISOLATION !== "crawl") {
    console.error("Skipping crawl screencast (CHROME_ISOLATION=snapshot)");
    return;
  }
  if (!CRAWL_DIR_VALUE) {
    console.error("Skipping crawl screencast (CRAWL_DIR is not set)");
    return;
  }

  fs.mkdirSync(LIVE_DIR, { recursive: true });
  console.error("chrome screencast starting");
  console.log("chrome screencast ready");
}

async function stopScreencast(status = "succeeded", output = "") {
  if (shuttingDown) return;
  shuttingDown = true;
  clearPendingFrame();
  if (cdpSession) {
    try {
      await cdpSession.send("Page.stopScreencast");
    } catch (error) {}
    cdpSession.removeAllListeners("Page.screencastFrame");
    try {
      await cdpSession.detach();
    } catch (error) {}
    cdpSession = null;
  }
  if (browser) {
    try {
      browser.disconnect();
    } catch (error) {}
    browser = null;
  }
  const remainingFrames = cleanupFinalFrames(keepFramesOnExit);
  emitResult(
    status,
    output || `${frameCount} screencast frames (${remainingFrames} kept)`
  );
}

async function handleShutdown(signal) {
  console.error(`\nReceived ${signal}, stopping screencast...`);
  await stopScreencast();
  process.exit(0);
}

async function main() {
  const args = parseArgs();
  if (!args.url) {
    console.error(
      "Usage: on_Snapshot__02_chrome_screencast.daemon.bg.js --url=<url>"
    );
    process.exit(1);
  }
  __abxInstallShutdownHandler(handleShutdown);

  try {
    if (IS_CRAWL_SETUP) {
      await publishCrawlScreencastReady();
      process.exit(0);
    }
    await startScreencast();
    await new Promise(() => {});
  } catch (error) {
    const message = `${error.name}: ${error.message}`;
    console.error(`ERROR: ${message}`);
    await stopScreencast("failed", message);
    process.exit(1);
  }
}

main().catch(async (error) => {
  const message = `${error.name}: ${error.message}`;
  console.error(`Fatal error: ${message}`);
  await stopScreencast("failed", message);
  process.exit(1);
});
