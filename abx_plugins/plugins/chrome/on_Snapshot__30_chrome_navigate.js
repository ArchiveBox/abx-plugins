#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries node
// /// script
// ///
/**
 * Navigate the Chrome browser to the target URL.
 *
 * This is a simple hook that ONLY navigates - nothing else.
 * Pre-load hooks (21-29) should set up their own CDP listeners.
 * Post-load hooks (31+) can then read from the loaded page.
 *
 * Usage: on_Snapshot__30_chrome_navigate.js --url=<url>
 * Output: Writes navigation.json when navigation completes
 *
 * Environment variables:
 *     CHROME_PAGELOAD_TIMEOUT: Timeout in seconds (default: 60)
 *     CHROME_DELAY_AFTER_LOAD: Extra delay after load in seconds (default: 0)
 *     CHROME_WAIT_FOR: Wait condition (default: domcontentloaded)
 */

const fs = require("fs");
const path = require("path");
const {
  ensureNodeModuleResolution,
  parseArgs,
  getEnv,
  getEnvInt,
  loadConfig,
  emitArchiveResultRecord,
  writeFileAtomic,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);
const { connectToPage, readBrowserMetadata, resolvePuppeteerModule, withTimeout } = require("./chrome_utils.js");
const puppeteer = resolvePuppeteerModule();

const PLUGIN_NAME = "chrome_navigate";
const CHROME_SESSION_DIR = ".";
const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || ".").trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

function getEnvFloat(name, defaultValue = 0) {
  const val = parseFloat(getEnv(name, String(defaultValue)));
  return isNaN(val) ? defaultValue : val;
}

function getWaitCondition() {
  const waitFor = getEnv("CHROME_WAIT_FOR", "domcontentloaded").toLowerCase();
  const valid = ["domcontentloaded", "load", "networkidle0", "networkidle2"];
  return valid.includes(waitFor) ? waitFor : "domcontentloaded";
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Cloudflare Turnstile interstitials answer the real URL with a challenge page
// that reloads into the site once the browser passes. Later extractors must see
// the site, not the interstitial, so give it a fixed window to clear. Past
// that, keep the challenge page as this snapshot's (visible) result.
const CHALLENGE_WAIT_MS = 90000;

// Each poll asks the current main document for its own HTTP status over this
// hook's raw CDP session. The challenge document is the cf-mitigated 403; the
// cleared site is whatever replaces it, including a form POST back to the URL.
// (window._cf_chl_opt is no signal: protected pages define it too.)
async function waitForChallenge(session, deadline, waitUntil) {
  // The cleared document must load as far as goto() would have waited.
  const readyStates = waitUntil === "domcontentloaded" ? ["interactive", "complete"] : ["complete"];
  while (Date.now() < deadline) {
    await sleep(1000);
    const state = await session.send("Runtime.evaluate", {
      expression: `({
        readyState: document.readyState,
        status: performance.getEntriesByType("navigation")[0]?.responseStatus || null,
      })`,
      returnByValue: true,
    }).then(({ result }) => result.value, () => null);
    if (state?.status && state.status < 400 && readyStates.includes(state.readyState)) return state;
  }
  return null;
}

async function navigateMainDocument(session, page, url, timeout) {
  // Puppeteer's DOMContentLoaded waiter also waits for every loading iframe.
  // The snapshot's main document can be ready while an embedded site never replies.
  await session.send("Page.enable");
  await session.send("Page.setLifecycleEventsEnabled", { enabled: true });
  const completedLoaders = new Set();
  let navigation = null;
  let resolveReady;
  let rejectReady;
  const ready = new Promise((resolve, reject) => {
    resolveReady = resolve;
    rejectReady = reject;
  });
  const checkReady = () => {
    if (navigation && (!navigation.loaderId || completedLoaders.has(`${navigation.frameId}:${navigation.loaderId}`))) {
      resolveReady();
    }
  };
  const onLifecycle = (event) => {
    if (event.name !== "DOMContentLoaded") return;
    completedLoaders.add(`${event.frameId}:${event.loaderId}`);
    checkReady();
  };
  const onClosed = () => rejectReady(new Error("Navigation target closed or disconnected"));
  const browser = page.browser();
  const timer = setTimeout(() => rejectReady(new Error(`Navigation timeout of ${timeout} ms exceeded`)), timeout);
  session.on("Page.lifecycleEvent", onLifecycle);
  page.on("close", onClosed);
  page.on("error", rejectReady);
  browser.on("disconnected", onClosed);
  // Arm the listener before issuing navigation: localhost can finish before
  // Page.navigate returns its loader ID. That ID excludes the previous document.
  session.send("Page.navigate", { url }).then((result) => {
    if (result.errorText || result.isDownload) {
      rejectReady(new Error(result.errorText || "net::ERR_ABORTED"));
      return;
    }
    navigation = result;
    checkReady();
  }, rejectReady);
  try {
    await ready;
  } finally {
    clearTimeout(timer);
    session.off("Page.lifecycleEvent", onLifecycle);
    page.off("close", onClosed);
    page.off("error", rejectReady);
    browser.off("disconnected", onClosed);
  }
}

async function navigate(url) {
  const hookTimeoutSeconds =
    getEnvInt("CHROME_TIMEOUT") || getEnvInt("TIMEOUT", 60);
  const requestedPageLoadTimeoutSeconds =
    getEnvInt("CHROME_PAGELOAD_TIMEOUT") || hookTimeoutSeconds;
  const timeoutGraceSeconds = Math.min(
    5,
    Math.max(1, Math.floor(hookTimeoutSeconds / 10))
  );
  const hookBudget = Math.max(
    1000,
    (hookTimeoutSeconds - timeoutGraceSeconds) * 1000
  );
  const requestedPageLoadTimeout = requestedPageLoadTimeoutSeconds * 1000;
  const delayAfterLoad = getEnvFloat("CHROME_DELAY_AFTER_LOAD", 0) * 1000;
  const waitUntil = getWaitCondition();

  let browser = null;
  let observedResponse = null;
  const navStartTime = Date.now();

  try {
    const conn = await connectToPage({
      chromeSessionDir: CHROME_SESSION_DIR,
      timeoutMs: hookBudget,
      requireTargetId: true,
      puppeteer,
    });
    browser = conn.browser;
    const page = conn.page;
    const network = await page.createCDPSession();
    // Chrome takes navigator.platform for each new document from the newest
    // DevTools session, which is this one: carry the persona identity that
    // hydration applies to every other session (UA, client hints, platform).
    const userAgentOverride = readBrowserMetadata(CHROME_SESSION_DIR)?.userAgentOverride;
    if (userAgentOverride) await network.send("Emulation.setUserAgentOverride", userAgentOverride);
    const { frameTree } = await network.send("Page.getFrameTree");
    let mainRequestId = null;
    let mainDocumentResponse = null;
    const earlyResponses = new Map();
    const rememberResponse = (requestId, status, headers, mimeType = null) => {
      if (status < 200 || status >= 300) return;
      const response = {
        status,
        contentType: Object.entries(headers || {}).find(
          ([name]) => name.toLowerCase() === "content-type"
        )?.[1] || mimeType,
      };
      if (requestId === mainRequestId) observedResponse = response;
      else if (mainRequestId === null) earlyResponses.set(requestId, response);
    };
    network.on("Network.requestWillBeSent", (event) => {
      if (event.type !== "Document" || event.frameId !== frameTree.frame.id) return;
      mainRequestId = event.requestId;
      mainDocumentResponse = null;
      observedResponse = earlyResponses.get(mainRequestId) || null;
      earlyResponses.clear();
    });
    network.on("Network.responseReceived", ({ requestId, response }) => {
      rememberResponse(requestId, response.status, response.headers, response.mimeType);
      // ExtraInfo exposes wire-level 304/redirect statuses; responseReceived
      // owns the final document's effective status and challenge headers.
      if (requestId === mainRequestId) mainDocumentResponse = response;
    });
    // Downloads abort page navigation and may never become Puppeteer Response
    // objects. ExtraInfo still carries their actual HTTP status and headers.
    // Correlate by the main document request, not URL suffix or another tab's
    // download. Buffer metadata until its request is identified instead of
    // assigning responses by arrival order. HTML-only hooks need this metadata to
    // avoid treating the leftover about:blank document as the downloaded page.
    network.on("Network.responseReceivedExtraInfo", (event) => {
      rememberResponse(event.requestId, event.statusCode, event.headers);
    });
    await network.send("Network.enable");

    const remainingBudget = hookBudget - (Date.now() - navStartTime);
    if (remainingBudget <= 0) {
      throw new Error("Timed out before page navigation could start");
    }
    const timeout = Math.max(
      1000,
      Math.min(requestedPageLoadTimeout, remainingBudget)
    );

    // Navigate
    console.log(
      `Navigating to ${url} (wait: ${waitUntil}, timeout: ${timeout}ms)`
    );
    if (waitUntil === "domcontentloaded") {
      await navigateMainDocument(network, page, url, timeout);
    } else {
      await page.goto(url, { waitUntil, timeout });
    }
    const responseStatus = mainDocumentResponse?.status || null;
    const responseHeaders = Object.fromEntries(Object.entries(mainDocumentResponse?.headers || {}).map(([name, value]) => [name.toLowerCase(), value]));
    let challenge = null;
    let cleared = null;
    if (responseHeaders["cf-mitigated"] === "challenge") {
      const deadline = Math.min(
        Date.now() + CHALLENGE_WAIT_MS,
        navStartTime + hookBudget - 5000
      );
      console.log(`Waiting up to ${Math.max(0, deadline - Date.now())}ms for Cloudflare challenge to clear...`);
      cleared = await waitForChallenge(network, deadline, waitUntil);
      challenge = cleared ? "cleared" : "unsolved";
      console.log(`Cloudflare challenge ${challenge} after ${Date.now() - navStartTime}ms`);
    }

    // Optional delay
    if (delayAfterLoad > 0) {
      console.log(`Waiting ${delayAfterLoad}ms after load...`);
      await sleep(delayAfterLoad);
    }

    const finalUrl = page.url();
    const status = cleared?.status || responseStatus;
    // Use the browser's interpretation, including MIME sniffing, rather than
    // URL extensions or response headers. Persist once for Python and JS hooks.
    const mimeTimeoutMs = Math.max(0, Math.min(10000, hookBudget - (Date.now() - navStartTime)));
    const contentType = mimeTimeoutMs > 0
      ? await withTimeout(
          () => page.evaluate(() => document.contentType),
          mimeTimeoutMs,
          "Document MIME lookup timed out"
        ).catch(() => null)
      : null;
    const elapsed = Date.now() - navStartTime;

    // Write navigation state as JSON
    const navigationState = {
      waitUntil,
      elapsed,
      url,
      finalUrl,
      status,
      content_type: contentType,
      ...(challenge ? { challenge } : {}),
      timestamp: new Date().toISOString(),
    };
    writeFileAtomic(
      path.join(OUTPUT_DIR, "navigation.json"),
      JSON.stringify(navigationState, null, 2)
    );

    browser.disconnect();

    return { success: true, finalUrl, status, waitUntil, elapsed, challenge };
  } catch (e) {
    if (browser) browser.disconnect();
    const elapsed = Date.now() - navStartTime;
    return {
      success: false,
      error: `${e.name}: ${e.message}`,
      status: observedResponse?.status || null,
      contentType: observedResponse?.contentType || null,
      waitUntil,
      elapsed,
    };
  }
}

async function main() {
  const args = parseArgs();
  const url = args.url;

  if (!url) {
    console.error("Usage: on_Snapshot__30_chrome_navigate.js --url=<url>");
    process.exit(1);
  }

  const startTs = new Date();
  let status = "failed";
  let output = null;
  let error = "";

  const result = await navigate(url);

  if (result.success) {
    status = "succeeded";
    output = result.status
      ? `page loaded http=${result.status}`
      : "page loaded";
    if (result.challenge === "unsolved") output += " (Cloudflare challenge unsolved)";
    console.log(
      `Page loaded: ${result.finalUrl} (HTTP ${result.status}) in ${result.elapsed}ms (waitUntil: ${result.waitUntil})`
    );
  } else {
    error = result.error;
    // Save navigation state even on failure
    const navigationState = {
      waitUntil: result.waitUntil,
      elapsed: result.elapsed,
      url,
      error: result.error,
      status: result.status,
      content_type: result.contentType,
      timestamp: new Date().toISOString(),
    };
    writeFileAtomic(
      path.join(OUTPUT_DIR, "navigation.json"),
      JSON.stringify(navigationState, null, 2)
    );
  }

  const endTs = new Date();

  if (error) console.error(`ERROR: ${error}`);

  // Output clean JSONL (no RESULT_JSON= prefix)
  emitArchiveResultRecord(status, output || error || "");

  process.exit(status === "succeeded" ? 0 : 1);
}

if (require.main === module) {
  main().catch((e) => {
    console.error(`Fatal error: ${e.message}`);
    process.exit(1);
  });
}
