#!/usr/bin/env node
/**
 * Write low-resolution Chrome screencast JPEGs for the admin live progress UI.
 *
 * Frames are cache-only and intentionally not stored as snapshot artifacts.
 */

const fs = require("fs");
const path = require("path");

const {
  ensureNodeModuleResolution,
  getEnvBool,
  getEnvInt,
  loadConfig,
  parseArgs,
  emitArchiveResultRecord,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);

const {
  connectToBrowserEndpoint,
  resolvePuppeteerModule,
  waitForChromeSessionState,
} = require("../chrome/chrome_utils.js");
const puppeteer = resolvePuppeteerModule();

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const DATA_DIR = path.resolve((hookConfig.DATA_DIR || "").trim() || ".");
const IS_CRAWL_SETUP = path.basename(process.argv[1] || "").startsWith("on_CrawlSetup__");
const RUN_DIR = path.resolve(
  (
    (IS_CRAWL_SETUP ? hookConfig.CRAWL_DIR : hookConfig.SNAP_DIR) ||
    hookConfig.CRAWL_DIR ||
    hookConfig.SNAP_DIR ||
    "."
  ).trim()
);
const OUTPUT_DIR = path.join(RUN_DIR, PLUGIN_DIR);
const LIVE_ID = path.basename(RUN_DIR);
const CHROME_SESSION_DIR = path.join(RUN_DIR, "chrome");
const LIVE_DIR = path.join(DATA_DIR, "cache", "chrome_screencast", LIVE_ID);
const LATEST_FRAME = path.join(LIVE_DIR, "latest.jpg");
if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

let browser = null;
let shuttingDown = false;
let frameCount = 0;
let lastWriteAt = 0;
let nextFrameNumber = 1;
let captureTimer = null;

function emitResult(status, output) {
  if (IS_CRAWL_SETUP) {
    console.error(output);
  } else {
    emitArchiveResultRecord(status, output);
  }
}

async function getLastOpenPage(browser) {
  const pageTargets = browser
    .targets()
    .filter((target) => target.type() === "page");
  const target = pageTargets[pageTargets.length - 1];
  if (!target) {
    throw new Error("No live Chrome page target found");
  }
  const page = await target.page();
  if (!page) {
    throw new Error("Last Chrome page target has no page handle");
  }
  return page;
}

async function captureVisibleViewportJpeg(browser, quality) {
  const page = await getLastOpenPage(browser);
  const cdpSession = await page.target().createCDPSession();
  let result;
  try {
    const metrics = await cdpSession.send("Page.getLayoutMetrics");
    const viewport = metrics.visualViewport || metrics.layoutViewport || {};
    const width = Math.max(1, Math.floor(viewport.clientWidth || 1440));
    const height = Math.max(1, Math.floor(viewport.clientHeight || 900));
    const x = Math.max(0, Math.floor(viewport.pageX || 0));
    const y = Math.max(0, Math.floor(viewport.pageY || 0));
    result = await cdpSession.send("Page.captureScreenshot", {
      format: "jpeg",
      quality,
      fromSurface: true,
      captureBeyondViewport: false,
      clip: { x, y, width, height, scale: 1 },
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

async function startScreencast() {
  if (!getEnvBool("CHROME_SCREENCAST_ENABLED", true)) {
    emitResult("skipped", "CHROME_SCREENCAST_ENABLED=False");
    process.exit(0);
  }
  if (!hookConfig.DATA_DIR) {
    emitResult("skipped", "DATA_DIR is not set");
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
    defaultViewport: null,
  });

  const quality = Math.max(
    1,
    Math.min(100, getEnvInt("CHROME_SCREENCAST_QUALITY", 35))
  );
  const fps = Math.max(1, Math.min(5, getEnvInt("CHROME_SCREENCAST_FPS", 1)));
  const bufferSize = Math.max(
    1,
    Math.min(120, getEnvInt("CHROME_SCREENCAST_BUFFER", 20))
  );
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
    cleanupOldFrames(bufferSize);
  };

  async function captureFrame() {
    if (shuttingDown) return;
    const now = Date.now();
    if (now - lastWriteAt < minFrameMs) return;
    lastWriteAt = now;
    try {
      const jpeg = await captureVisibleViewportJpeg(browser, quality);
      writeFrame(jpeg);
    } catch (error) {
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
  emitResult(status, output || `${frameCount} screencast frames`);
  try {
    fs.rmSync(LIVE_DIR, { recursive: true, force: true });
  } catch (error) {}
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
