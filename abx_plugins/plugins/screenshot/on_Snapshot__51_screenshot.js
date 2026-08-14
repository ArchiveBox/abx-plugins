#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Take a screenshot of a URL using an existing Chrome session.
 *
 * Requires chrome plugin to have already created a Chrome session.
 * Connects to the existing session via CDP and takes a screenshot.
 *
 * Usage: on_Snapshot__51_screenshot.js --url=<url>
 * Output: Writes screenshot/screenshot.png
 *
 * Environment variables:
 *     SCREENSHOT_ENABLED: Enable screenshot capture (default: true)
 */

if (process.argv.some((arg) => arg === "--url" || arg.startsWith("--url="))) {
  console.log("Screenshot capture started");
}

const fs = require("fs");
const path = require("path");
const {
  ensureNodeModuleResolution,
    getEnvBool,
  loadConfig,
  parseArgs,
  emitArchiveResultRecord,
  hasStaticFileOutput,
  writeFileAtomic,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);
const {
  connectToPage,
  resolvePuppeteerModule,
  waitForNavigationComplete,
} = require("../chrome/chrome_utils.js");
const hookConfig = loadConfig();

// Flush V8 coverage before exiting (for NODE_V8_COVERAGE support)
function flushCoverageAndExit(exitCode) {
  if (hookConfig.NODE_V8_COVERAGE) {
    try {
      const v8 = require("v8");
      v8.takeCoverage();
    } catch (e) {
      // Ignore errors during coverage flush
    }
  }
  process.exit(exitCode);
}

function tempPathFor(filePath) {
  const dir = path.dirname(filePath);
  const base = path.basename(filePath);
  return path.join(dir, `.${base}.${process.pid}.tmp`);
}

function getScreenshotViewport() {
  const resolutionSource = Object.prototype.hasOwnProperty.call(
    process.env,
    "SCREENSHOT_RESOLUTION"
  )
    ? process.env.SCREENSHOT_RESOLUTION
    : hookConfig.SCREENSHOT_RESOLUTION;
  const match = String(resolutionSource ?? "1440,2000").match(
    /^\s*(\d+)\s*,\s*(\d+)\s*$/
  );
  if (!match) {
    throw new Error(`Invalid SCREENSHOT_RESOLUTION=${resolutionSource}`);
  }

  const width = Number(match[1]);
  const height = Number(match[2]);
  if (width <= 0 || height <= 0) {
    throw new Error(`Invalid SCREENSHOT_RESOLUTION=${resolutionSource}`);
  }
  return { width, height, deviceScaleFactor: 1 };
}

async function waitForTextInPageFrames(page, expectedText, frameUrlPart, timeoutMs) {
  if (!expectedText) return;

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const frame of page.frames()) {
      if (frameUrlPart && !frame.url().includes(frameUrlPart)) continue;
      try {
        const matched = await frame.evaluate(
          (text) => (document.body?.innerText || document.documentElement?.innerText || "").includes(text),
          expectedText
        );
        if (matched) return;
      } catch (_error) {
        // Frames may navigate or detach while an async replay viewer starts.
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }

  throw new Error(`Screenshot wait text not found before timeout: ${expectedText}`);
}

// Check if screenshot is enabled BEFORE requiring puppeteer
if (!getEnvBool("SCREENSHOT_ENABLED", true)) {
  console.error("Skipping screenshot (SCREENSHOT_ENABLED=False)");
  emitArchiveResultRecord("skipped", "SCREENSHOT_ENABLED=False");
  flushCoverageAndExit(0);
}

// Extractor metadata
const PLUGIN_NAME = "screenshot";
const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || ".").trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const OUTPUT_FILE = "screenshot.png";
const METADATA_FILE = "screenshot.json";
const CHROME_SESSION_DIR = "../chrome";

async function takeScreenshot(url) {
  // Output directory is current directory (hook already runs in output dir)
  const outputPath = path.join(OUTPUT_DIR, OUTPUT_FILE);
  const tempOutputPath = tempPathFor(outputPath);

  // Wait for chrome_navigate to complete (writes navigation.json)
  // Keep runtime default aligned with config.json (default: 60s).
  const timeoutSource = Object.prototype.hasOwnProperty.call(
    process.env,
    "SCREENSHOT_TIMEOUT"
  )
    ? process.env.SCREENSHOT_TIMEOUT
    : hookConfig.SCREENSHOT_TIMEOUT;
  const timeoutSeconds = Number(timeoutSource ?? 60);
  if (!Number.isFinite(timeoutSeconds) || timeoutSeconds <= 0) {
    throw new Error(`Invalid SCREENSHOT_TIMEOUT=${timeoutSource}`);
  }
  const timeoutMs = timeoutSeconds * 1000;

  // Navigation is the lifecycle gate for screenshot capture. Check that marker
  // before loading Puppeteer or opening a CDP connection so a missing/failed
  // chrome_navigate result fails on SCREENSHOT_TIMEOUT itself instead of paying
  // browser module startup cost first.
  await waitForNavigationComplete(CHROME_SESSION_DIR, timeoutMs);

  const puppeteer = resolvePuppeteerModule();
  const { browser, page } = await connectToPage({
    chromeSessionDir: CHROME_SESSION_DIR,
    timeoutMs,
    puppeteer,
  });

  try {
    const captureTimeoutMs = Math.max(timeoutMs, 10000);
    const viewport = getScreenshotViewport();
    await page.setViewport(viewport);
    const waitForText = Object.prototype.hasOwnProperty.call(
      process.env,
      "SCREENSHOT_WAIT_FOR_TEXT"
    )
      ? process.env.SCREENSHOT_WAIT_FOR_TEXT
      : hookConfig.SCREENSHOT_WAIT_FOR_TEXT;
    const waitForFrameUrl = Object.prototype.hasOwnProperty.call(
      process.env,
      "SCREENSHOT_WAIT_FOR_FRAME_URL"
    )
      ? process.env.SCREENSHOT_WAIT_FOR_FRAME_URL
      : hookConfig.SCREENSHOT_WAIT_FOR_FRAME_URL;
    await waitForTextInPageFrames(
      page,
      String(waitForText || ""),
      String(waitForFrameUrl || ""),
      captureTimeoutMs
    );
    await Promise.race([
      page.screenshot({ path: tempOutputPath, fullPage: false }),
      new Promise((_, reject) => {
        setTimeout(
          () => reject(new Error("Screenshot capture timed out")),
          captureTimeoutMs
        );
      }),
    ]);

    fs.renameSync(tempOutputPath, outputPath);
    const navigationPath = path.resolve(CHROME_SESSION_DIR, "navigation.json");
    const navigation = fs.existsSync(navigationPath)
      ? JSON.parse(fs.readFileSync(navigationPath, "utf8"))
      : {};
    writeFileAtomic(
      path.join(OUTPUT_DIR, METADATA_FILE),
      JSON.stringify(
        {
          requestedUrl: url,
          finalUrl: page.url(),
          status: navigation.status ?? null,
          width: viewport.width,
          height: viewport.height,
        },
        null,
        2
      )
    );

    return OUTPUT_FILE;
  } finally {
    // Disconnect from browser (don't close it - we're connected to a shared session)
    // The chrome_launch hook manages the browser lifecycle
    await browser.disconnect();
  }
}

async function main() {
  const args = parseArgs();
  const url = args.url;

  if (!url) {
    console.error("Usage: on_Snapshot__51_screenshot.js --url=<url>");
    emitArchiveResultRecord("failed", "missing required args");
    flushCoverageAndExit(1);
  }

  // Check if staticfile extractor already handled this (permanent skip)
  if (hasStaticFileOutput()) {
    console.error(
      `Skipping screenshot - staticfile extractor already downloaded this`
    );
    emitArchiveResultRecord("noresults", "staticfile already handled");
    flushCoverageAndExit(0);
  }

  // Take screenshot (throws on error)
  const outputPath = await takeScreenshot(url);

  // Success - emit ArchiveResult
  const size = fs.statSync(path.join(OUTPUT_DIR, outputPath)).size;
  console.error(`Screenshot saved (${size} bytes)`);
  emitArchiveResultRecord("succeeded", `${PLUGIN_DIR}/${outputPath}`);
  flushCoverageAndExit(0);
}

main().catch((e) => {
  console.error(`ERROR: ${e.message}`);
  emitArchiveResultRecord("failed", e.message || "unknown error");
  flushCoverageAndExit(1);
});
