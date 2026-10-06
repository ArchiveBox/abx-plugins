#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Capture responsive assets on the SAME recorded tab, after desktop outputs.
 *
 * WHY this exists: people should be able to replay an archive at a different
 * screen size from the one used to record it. Responsive layouts often make
 * additional requests: <picture>/srcset selects different images, CSS media
 * queries load other backgrounds, and JS resize/matchMedia handlers fetch
 * breakpoint-specific data or code. A desktop-only recording can omit those
 * responses, leaving broken images or missing content when replayed on a phone.
 * Visiting a phone width while recording is still active gives both recorders
 * a chance to save those responses without contacting the live site at replay.
 * This improves replay across screen sizes; one extra width cannot cover every
 * breakpoint, DPR, interaction, or a script that checks width only on page load.
 *
 * WHY order 60: infiniscroll (45) expands the desktop page; screenshot/PDF/DOM
 * and other desktop outputs (50-59) must finish before we resize it. Responses
 * is still monitoring, and ArchiveWeb.page does not stop until 65, so both can
 * save images/CSS/etc. requested by mobile media queries. This is foreground
 * work: stopping the recorder before this finishes would lose those requests.
 *
 * Only change viewport dimensions, not UA, touch, DPR, or mobile emulation.
 * Enabling mobile emulation may reload the page and lose its settled state.
 * Never navigate, open another tab, or repeat scrolling/capture plugins here.
 * Restore dimensions and scroll even if the wait fails. Pages with continuous
 * traffic get a bounded best-effort pass; assets arriving later can still be
 * recorded by the existing monitors. This does not fetch every srcset entry or
 * guarantee lazy content below the mobile fold is loaded.
 */
const fs = require("fs");
const path = require("path");
const {
  loadConfig, getEnvBool, getEnvInt, parseArgs, emitArchiveResultRecord,
  hasStaticFileOutput, isNonHtmlDocument,
} = require("../base/utils.js");
const {connectToPage} = require("../chrome/chrome_utils.js");
const config = loadConfig();

async function main() {
  if (!getEnvBool("MOBILESIZE_ENABLED", true)) {
    emitArchiveResultRecord("skipped", "MOBILESIZE_ENABLED=False");
    return;
  }
  const {url} = parseArgs();
  if (!url) throw new Error("Usage: on_Snapshot__60_mobilesize.js --url=<url>");
  const outputDir = path.join(path.resolve(config.SNAP_DIR || "."), "mobilesize");
  fs.mkdirSync(outputDir, {recursive:true});
  process.chdir(outputDir);
  if (hasStaticFileOutput() || isNonHtmlDocument()) {
    emitArchiveResultRecord("noresults", "Browser document is not HTML or staticfile already handled");
    return;
  }
  const width = getEnvInt("MOBILESIZE_WIDTH", 390);
  const height = getEnvInt("MOBILESIZE_HEIGHT", 844);
  const waitMs = getEnvInt("MOBILESIZE_WAIT", 3) * 1000;
  const timeoutMs = getEnvInt("MOBILESIZE_TIMEOUT", 30) * 1000;
  if (width < 1 || height < 1 || waitMs < 1000 || waitMs > 10000 || timeoutMs < 10000) {
    throw new Error("Invalid MOBILESIZE dimensions, wait, or timeout");
  }
  if (waitMs + 5000 >= timeoutMs) {
    throw new Error("MOBILESIZE_TIMEOUT must leave more than 5 seconds beyond MOBILESIZE_WAIT for connection and restoration");
  }
  const {browser, page} = await connectToPage({
    chromeSessionDir: "../chrome",
    timeoutMs: Math.min(timeoutMs - waitMs - 5000, 10000),
    waitForNavigationComplete: true,
  });
  try {
    // CDP connections use defaultViewport:null. Read the live dimensions rather
    // than CHROME_RESOLUTION: screenshot or the user may have changed them.
    const original = await page.evaluate(() => ({
      width: innerWidth, height: innerHeight, deviceScaleFactor: devicePixelRatio,
      x: scrollX, y: scrollY,
    }));
    if (original.width <= width) {
      emitArchiveResultRecord("noresults", "Viewport is already phone width");
      return;
    }
    // A dedicated session scopes the override to this hook. Unlike setViewport,
    // this never toggles touch emulation or triggers Puppeteer's reload path.
    const session = await page.createCDPSession();
    try {
      // Like screenshot capture, keep rendering active when another tab is
      // selected. Otherwise Chrome can defer srcset reevaluation indefinitely.
      await session.send("Emulation.setFocusEmulationEnabled", {enabled:true});
      await session.send("Emulation.setDeviceMetricsOverride", {
        width, height, deviceScaleFactor: original.deviceScaleFactor, mobile:false,
      });
      // Start at the mobile fold even if an earlier hook left the page scrolled;
      // Chrome can defer changing offscreen picture sources until visible.
      await page.evaluate(() => window.scrollTo({top:0, left:0, behavior:"instant"}));
      await page.waitForNetworkIdle({idleTime:1000, timeout:waitMs}).catch(error => {
        if (error.name !== "TimeoutError") throw error;
        console.error(`Mobile viewport network wait reached ${waitMs / 1000}s; continuing`);
      });
    } finally {
      try {
        await session.send("Emulation.setDeviceMetricsOverride", {
          width:original.width, height:original.height,
          deviceScaleFactor:original.deviceScaleFactor, mobile:false,
        });
        await page.evaluate(({x, y}) => window.scrollTo({left:x, top:y, behavior:"instant"}), original);
      } finally {await session.detach();}
    }
    emitArchiveResultRecord("succeeded", `resized to ${width}x${height}, restored desktop viewport`);
  } finally {browser.disconnect();}
}

main().catch(error => {
  console.error(`ERROR: ${error.name}: ${error.message}`);
  emitArchiveResultRecord("failed", error.message);
  process.exitCode = 1;
});
