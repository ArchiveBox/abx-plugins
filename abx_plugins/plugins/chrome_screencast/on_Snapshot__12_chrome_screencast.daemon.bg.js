#!/usr/bin/env node
/**
 * Write Chrome screencast JPEGs for the admin live progress UI.
 *
 * Frames are crawl-scoped plugin output, shared by crawl setup and snapshot hooks.
 */

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
const puppeteer = resolvePuppeteerModule();

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const CRAWL_DIR_VALUE = (hookConfig.CRAWL_DIR || "").trim();
const CRAWL_DIR = path.resolve(CRAWL_DIR_VALUE || ".");
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || CRAWL_DIR).trim());
const CHROME_SESSION_DIR = path.join(SNAP_DIR, "chrome");
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
let shuttingDown = false;
let frameCount = 0;
let lastWriteAt = 0;
let nextFrameNumber = 1;
let captureTimer = null;
let keepFramesOnExit = 0;

function emitResult(status, output) {
  emitArchiveResultRecord(status, output);
}

async function captureVisibleViewportJpeg(
  browser,
  quality,
  screenshotScale,
  targetId = null
) {
  const pageTargets = browser.targets().filter((target) => {
    const url = target.url() || "";
    return (
      target.type() === "page" &&
      (url.startsWith("http://") || url.startsWith("https://"))
    );
  });
  const target =
    (targetId &&
      pageTargets.find(
        (candidate) => getTargetIdFromTarget(candidate) === targetId
      )) ||
    pageTargets[pageTargets.length - 1];
  if (!target) {
    throw new Error("No HTTP(S) Chrome page target found");
  }
  const page = await target.page();
  if (!page) {
    throw new Error("Last Chrome page target has no page handle");
  }
  await page.bringToFront();
  const cdpSession = await page.target().createCDPSession();
  let result;
  try {
    const metrics = await cdpSession.send("Page.getLayoutMetrics");
    const viewport = metrics.visualViewport || metrics.layoutViewport || {};
    const width = Math.max(1, Math.floor(viewport.clientWidth || 1440));
    const height = Math.max(1, Math.floor(viewport.clientHeight || 900));
    const x = Math.max(0, Math.floor(viewport.pageX || 0));
    const y = Math.max(0, Math.floor(viewport.pageY || 0));
    // Capture the current rendered viewport without resizing/emulating device
    // metrics. CDP's clip.scale downscales the captured bitmap without changing
    // the page viewport, avoiding both page reflow and JS-side image transforms.
    result = await cdpSession.send("Page.captureScreenshot", {
      format: "jpeg",
      quality,
      optimizeForSpeed: true,
      fromSurface: true,
      captureBeyondViewport: false,
      clip: { x, y, width, height, scale: screenshotScale },
    });
  } finally {
    try {
      await cdpSession.detach();
    } catch (error) {}
  }
  return Buffer.from(result.data, "base64");
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

  fs.mkdirSync(LIVE_DIR, { recursive: true });
  try {
    fs.unlinkSync(LATEST_FRAME);
  } catch (error) {}

  const timeoutMs =
    getEnvInt("CHROME_TIMEOUT", getEnvInt("TIMEOUT", 60)) * 1000;
  const chromeSession = await waitForChromeSessionState(CHROME_SESSION_DIR, {
    timeoutMs,
    requireTargetId: false,
  });
  if (!chromeSession?.cdpUrl) {
    throw new Error("No Chrome session found (chrome plugin must run first)");
  }
  browser = await connectToBrowserEndpoint(puppeteer, chromeSession.cdpUrl, {
    // Keep the real Chrome viewport created by the chrome plugin.
    defaultViewport: null,
  });
  const targetId = chromeSession.targetId || null;

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

  async function captureFrame() {
    if (shuttingDown) return;
    const now = Date.now();
    if (now - lastWriteAt < minFrameMs) return;
    lastWriteAt = now;
    try {
      const jpeg = await captureVisibleViewportJpeg(
        browser,
        quality,
        screenshotScale,
        targetId
      );
      writeFrame(jpeg);
    } catch (error) {
      if (error.message === "No HTTP(S) Chrome page target found") return;
      console.error(`WARN: failed to write screencast frame: ${error.message}`);
    }
  }

  await captureFrame();
  captureTimer = setInterval(captureFrame, minFrameMs);
  console.log(`screencast frames: ${LIVE_DIR}`);
}

async function stopScreencast(status = "succeeded", output = "") {
  if (shuttingDown) return;
  shuttingDown = true;
  if (captureTimer) {
    clearInterval(captureTimer);
    captureTimer = null;
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
      "Usage: on_Snapshot__12_chrome_screencast.daemon.bg.js --url=<url>"
    );
    process.exit(1);
  }
  process.on("SIGTERM", () => handleShutdown("SIGTERM"));
  process.on("SIGINT", () => handleShutdown("SIGINT"));

  try {
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
