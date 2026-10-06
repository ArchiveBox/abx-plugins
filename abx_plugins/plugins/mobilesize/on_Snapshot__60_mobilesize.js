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
const path = require("path");
const {
  loadConfig, getEnvBool, getEnvInt, parseArgs, emitArchiveResultRecord,
  hasStaticFileOutput, isNonHtmlDocument,
} = require("../base/utils.js");
const {connectToPage, waitForNavigationComplete, withTimeout} = require("../chrome/chrome_utils.js");
const config = loadConfig();

async function main() {
  if (!getEnvBool("MOBILESIZE_ENABLED", true)) {
    emitArchiveResultRecord("skipped", "MOBILESIZE_ENABLED=False");
    return;
  }
  const {url} = parseArgs();
  if (!url) throw new Error("Usage: on_Snapshot__60_mobilesize.js --url=<url>");
  const snapDir = path.resolve(config.SNAP_DIR || ".");
  const chromeSessionDir = path.join(snapDir, "chrome");
  // No artifacts of our own: use absolute sibling paths without making/chdiring
  // into an empty plugin directory on either success or skip paths.
  if (hasStaticFileOutput(path.join(snapDir, "staticfile")) ||
      isNonHtmlDocument(path.join(chromeSessionDir, "navigation.json"))) {
    emitArchiveResultRecord("noresults", "Browser document is not HTML or staticfile already handled");
    return;
  }
  let width = getEnvInt("MOBILESIZE_WIDTH", 390);
  const height = getEnvInt("MOBILESIZE_HEIGHT", 844);
  const waitMs = getEnvInt("MOBILESIZE_WAIT", 3) * 1000;
  const timeoutMs = getEnvInt("MOBILESIZE_TIMEOUT", 30) * 1000;
  if (width < 1 || height < 1 || waitMs < 1000 || waitMs > 10000 || timeoutMs < 16000) {
    throw new Error("Invalid MOBILESIZE dimensions, wait, or timeout");
  }
  // One deadline, not a fresh timeout per operation. Reserve the final five
  // seconds for cleanup so a hung renderer/CDP request cannot consume the time
  // needed to restore the tab before the runner terminates this hook.
  const deadline = Date.now() + timeoutMs;
  const workDeadline = deadline - 5000;
  const remaining = (end = workDeadline) => Math.max(1, end - Date.now());
  const step = (operation, end = workDeadline) => {
    if (Date.now() >= end) throw new Error("MOBILESIZE_TIMEOUT exceeded");
    return withTimeout(operation, remaining(end), "MOBILESIZE_TIMEOUT exceeded");
  };
  // Wait separately so navigation and attachment consume the SAME budget.
  // connectToPage's waitForNavigationComplete option grants each a full timeout.
  await step(() => waitForNavigationComplete(chromeSessionDir, remaining()));
  const {browser, page, cdpSession} = await step(async () => {
    const connection = await connectToPage({
      chromeSessionDir, timeoutMs: remaining(),
    });
    if (Date.now() >= workDeadline) {
      await connection.browser.disconnect();
      throw new Error("MOBILESIZE_TIMEOUT exceeded");
    }
    return connection;
  });
  try {
    // CDP connections use defaultViewport:null. Read the live dimensions rather
    // than CHROME_RESOLUTION: screenshot or the user may have changed them.
    const original = await step(() => page.evaluate(() => ({
      width: innerWidth, height: innerHeight, deviceScaleFactor: devicePixelRatio,
      x: scrollX, y: scrollY,
    })));
    // Never widen an already narrow tab, but still visit height breakpoints.
    width = Math.min(width, original.width);
    if (original.width === width && original.height === height) {
      emitArchiveResultRecord("noresults", "Viewport already has the requested dimensions");
      return;
    }
    // Focus must have its own session: explicit Target.detachFromTarget after
    // setting device metrics can reset dimensions on Linux, including a prior
    // screenshot hook's override. Detach focus FIRST, then restore dimensions
    // on the connection's existing CDP session. browser.disconnect releases
    // that connection without the explicit detach that undoes restoration.
    const focusSession = await step(() => page.createCDPSession());
    try {
      // Background tabs can otherwise defer picture/srcset reevaluation.
      await step(() => focusSession.send("Emulation.setFocusEmulationEnabled", {enabled:true}));
      // step owns the deadline; disable the connection's shorter per-command
      // timeout here so it cannot preempt the reserved restoration budget.
      await step(() => cdpSession.send("Emulation.setDeviceMetricsOverride", {
        width, height, deviceScaleFactor: original.deviceScaleFactor, mobile:false,
      }, {timeout:0}));
      // Chrome may defer offscreen picture sources, so start at the mobile fold.
      await step(() => page.evaluate(() => window.scrollTo({top:0, left:0, behavior:"instant"})));
      await step(() => page.waitForNetworkIdle({idleTime:1000, timeout:Math.min(waitMs, remaining())})).catch(error => {
        if (error.name !== "TimeoutError") throw error;
        console.error(`Mobile viewport network wait reached ${waitMs / 1000}s; continuing`);
      });
    } finally {
      // Each cleanup operation shares the same remaining deadline. A dead tab
      // cannot be restored, but must not leave this hook hanging indefinitely.
      try {
        await step(() => focusSession.detach(), deadline);
      } finally {
        await step(() => cdpSession.send("Emulation.setDeviceMetricsOverride", {
          width:original.width, height:original.height,
          deviceScaleFactor:original.deviceScaleFactor, mobile:false,
        }, {timeout:0}), deadline);
        await step(() => page.evaluate(({x, y}) => window.scrollTo({left:x, top:y, behavior:"instant"}), original), deadline);
      }
    }
    emitArchiveResultRecord("succeeded", `resized to ${width}x${height}, restored desktop viewport`);
  } finally {await browser.disconnect();}
}

main().catch(error => {
  console.error(`ERROR: ${error.name}: ${error.message}`);
  emitArchiveResultRecord("failed", error.message);
  process.exitCode = 1;
});
