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

// A server refusal is not a successfully captured document. Retry a transient
// GET once, without changing browser identity or extending the hook deadline.
const TRANSIENT_RETRY_DELAY_MS = 60000;
const MAX_NAVIGATION_ATTEMPTS = 2;

function parseHttpDate(value) {
  // HTTP-date permits IMF-fixdate and the two obsolete wire formats, not the
  // permissive calendar strings ("Jan 1", "1.5", ISO dates) Date.parse accepts.
  const day = "(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)";
  const month = "(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)";
  const time = "\\d{2}:\\d{2}:\\d{2}";
  const formats = [
    `${day}, \\d{2} ${month} \\d{4} ${time} GMT`,
    `(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), \\d{2}-${month}-\\d{2} ${time} GMT`,
    `${day} ${month} (?: \\d|\\d{2}) ${time} \\d{4}`,
  ];
  const format = formats.findIndex((pattern) => new RegExp(`^${pattern}$`).test(value));
  // asctime omits its timezone on the wire; HTTP still defines it as GMT.
  return format < 0 ? NaN : Date.parse(format === 2 ? `${value} GMT` : value);
}

function retryDelayMs(headers) {
  const value = String(headers["retry-after"] || "").trim();
  if (/^\d+$/.test(value)) return Math.max(1000, Number(value) * 1000);
  // Date.parse accepts strings such as "1.5" as calendar dates, but those are
  // not HTTP dates or valid delay-seconds. Malformed hints use the default.
  const retryAt = parseHttpDate(value);
  if (Number.isFinite(retryAt)) {
    const serverDate = parseHttpDate(headers.date || "");
    // Do not retry early when the local clock is ahead of the server's clock.
    return Math.max(1000, retryAt - Date.now(), Number.isFinite(serverDate) ? retryAt - serverDate : 0);
  }
  return TRANSIENT_RETRY_DELAY_MS;
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

async function navigateMainDocument(session, page, url, timeout, mainFrameId, reloadLoaderId = null) {
  // Puppeteer's DOMContentLoaded waiter also waits for every loading iframe.
  // The snapshot's main document can be ready while an embedded site never replies.
  await session.send("Page.enable");
  await session.send("Page.setLifecycleEventsEnabled", { enabled: true });
  const completedLoaders = new Set();
  let navigation = null;
  let committedLoaderId = null;
  let resolveReady;
  let rejectReady;
  const ready = new Promise((resolve, reject) => {
    resolveReady = resolve;
    rejectReady = reject;
  });
  const checkReady = () => {
    const loader = committedLoaderId || navigation?.loaderId;
    if (navigation && ((loader && completedLoaders.has(`${mainFrameId}:${loader}`)) || (!loader && !reloadLoaderId))) {
      resolveReady();
    }
  };
  const onFrameNavigated = ({ frame }) => {
    if (frame.id !== mainFrameId) return;
    if (frame.unreachableUrl) {
      rejectReady(new Error("Navigation committed an unreachable error page"));
      return;
    }
    // An inline location.replace can cancel the original loader before its
    // DOMContentLoaded. Follow the committed replacement in the same main frame.
    committedLoaderId = frame.loaderId;
    checkReady();
  };
  const onLifecycle = (event) => {
    if (event.name !== "DOMContentLoaded") return;
    completedLoaders.add(`${event.frameId}:${event.loaderId}`);
    checkReady();
  };
  const onClosed = () => rejectReady(new Error("Navigation target closed or disconnected"));
  let documentRequestUrl = null;
  const onRequest = (event) => {
    if (event.type === "Document" && event.frameId === mainFrameId) {
      documentRequestUrl = event.request.url.split("#")[0];
    }
  };
  const onDownload = (event) => {
    if (reloadLoaderId && event.frameId === mainFrameId &&
        event.url.split("#")[0] === documentRequestUrl) {
      rejectReady(new Error("net::ERR_ABORTED"));
    }
  };
  const browser = page.browser();
  const timer = setTimeout(() => rejectReady(new Error(`Navigation timeout of ${timeout} ms exceeded`)), timeout);
  session.on("Page.lifecycleEvent", onLifecycle);
  session.on("Page.frameNavigated", onFrameNavigated);
  session.on("Page.downloadWillBegin", onDownload);
  session.on("Network.requestWillBeSent", onRequest);
  page.on("close", onClosed);
  page.on("error", rejectReady);
  browser.on("disconnected", onClosed);
  // Arm the listener before issuing navigation: localhost can finish before
  // Page.navigate returns its loader ID. That ID excludes the previous document.
  session.send(reloadLoaderId ? "Page.reload" : "Page.navigate", reloadLoaderId ? { loaderId: reloadLoaderId } : { url }).then((result) => {
    if (result.errorText || result.isDownload) {
      const error = new Error(result.errorText || "net::ERR_ABORTED");
      if (typeof result.isDownload === "boolean") error.isDownload = result.isDownload;
      rejectReady(error);
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
    session.off("Page.frameNavigated", onFrameNavigated);
    session.off("Page.downloadWillBegin", onDownload);
    session.off("Network.requestWillBeSent", onRequest);
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
  let page = null;
  let observedResponse = null;
  let attempts = 0;
  let retryAfterMs = null;
  const navStartTime = Date.now();

  try {
    const conn = await connectToPage({
      chromeSessionDir: CHROME_SESSION_DIR,
      timeoutMs: hookBudget,
      requireTargetId: true,
      puppeteer,
    });
    browser = conn.browser;
    page = conn.page;
    const network = await page.createCDPSession();
    // Chrome takes navigator.platform for each new document from the newest
    // DevTools session, which is this one: carry the persona identity that
    // hydration applies to every other session (UA, client hints, platform).
    const userAgentOverride = readBrowserMetadata(CHROME_SESSION_DIR)?.userAgentOverride;
    if (userAgentOverride) await network.send("Emulation.setUserAgentOverride", userAgentOverride);
    const { frameTree } = await network.send("Page.getFrameTree");
    let mainRequestId = null;
    let mainRequestMethod = null;
    let mainDocumentResponse = null;
    let mainDocumentLoaderId = null;
    const earlyResponses = new Map();
    const rememberResponse = (requestId, status, headers, mimeType = null) => {
      if (status < 200) return;
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
      mainRequestMethod = event.request.method;
      mainDocumentResponse = null;
      mainDocumentLoaderId = null;
      observedResponse = earlyResponses.get(mainRequestId) || null;
      earlyResponses.clear();
    });
    network.on("Network.responseReceived", ({ requestId, response, loaderId }) => {
      rememberResponse(requestId, response.status, response.headers, response.mimeType);
      // ExtraInfo exposes wire-level 304/redirect statuses; responseReceived
      // owns the final document's effective status and challenge headers.
      if (requestId === mainRequestId) {
        mainDocumentResponse = response;
        mainDocumentLoaderId = loaderId;
      }
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

    let navigationUrl = url;
    let reloadLoaderId = null;
    let status = null;
    let error = null;
    let challenge = null;
    while (attempts < MAX_NAVIGATION_ATTEMPTS) {
      const remainingBudget = hookBudget - (Date.now() - navStartTime);
      if (remainingBudget <= 0) {
        throw new Error("Timed out before page navigation could start");
      }
      const timeout = Math.min(requestedPageLoadTimeout, remainingBudget);
      attempts += 1;
      console.log(
        `Navigating to ${navigationUrl} (wait: ${waitUntil}, timeout: ${timeout}ms)`
      );
      if (waitUntil === "domcontentloaded") {
        await navigateMainDocument(network, page, navigationUrl, timeout, frameTree.frame.id, reloadLoaderId);
      } else if (attempts > 1) {
        const controller = new AbortController();
        const ready = page.waitForNavigation({ waitUntil, timeout, signal: controller.signal });
        ready.catch(() => {});
        try {
          // Chrome rejects the reload if another document committed after our
          // guard. The shared waiter also detects the download terminal which
          // Page.reload does not report in its command response.
          await Promise.all([
            navigateMainDocument(network, page, navigationUrl, timeout, frameTree.frame.id, reloadLoaderId),
            ready,
          ]);
        } finally {
          controller.abort();
          await ready.catch(() => {});
        }
      } else {
        await page.goto(navigationUrl, { waitUntil, timeout });
      }
      status = mainDocumentResponse?.status || null;
      const responseHeaders = Object.fromEntries(Object.entries(mainDocumentResponse?.headers || {}).map(([name, value]) => [name.toLowerCase(), value]));
      if (responseHeaders["cf-mitigated"] === "challenge") {
        const deadline = Math.min(
          Date.now() + CHALLENGE_WAIT_MS,
          navStartTime + hookBudget - 5000
        );
        console.log(`Waiting up to ${Math.max(0, deadline - Date.now())}ms for Cloudflare challenge to clear...`);
        const cleared = await waitForChallenge(network, deadline, waitUntil);
        challenge = cleared ? "cleared" : "unsolved";
        status = cleared?.status || status;
        console.log(`Cloudflare challenge ${challenge} after ${Date.now() - navStartTime}ms`);
      }
      if (status !== 429 && status !== 503) break;
      // Do not resubmit a form, or turn an access challenge into retry traffic.
      if (mainRequestMethod !== "GET" || challenge) break;
      retryAfterMs = retryDelayMs(responseHeaders);
      if (attempts >= MAX_NAVIGATION_ATTEMPTS) {
        error = `HTTP ${status}: stopped after one delayed retry`;
        break;
      }
      if (retryAfterMs + 1000 >= hookBudget - (Date.now() - navStartTime)) {
        error = `HTTP ${status}: server cooldown exceeds remaining navigation budget`;
        break;
      }
      console.log(`HTTP ${status}: waiting ${retryAfterMs}ms before one retry`);
      const refusedRequestId = mainRequestId;
      const refusedUrl = page.url();
      const refusedLoaderId = mainDocumentLoaderId;
      await sleep(retryAfterMs);
      // A page may submit a form or redirect itself during the cooldown. Do not
      // turn that new document into another GET or repeat its side effects.
      if (mainRequestId !== refusedRequestId || page.url() !== refusedUrl) {
        status = mainDocumentResponse?.status || null;
        error = "Document changed during server cooldown; not retrying";
        break;
      }
      // Retry the refused document, not an earlier redirect or a different tab.
      navigationUrl = refusedUrl;
      reloadLoaderId = refusedLoaderId;
    }

    // Optional delay
    if (!(status >= 400) && delayAfterLoad > 0) {
      if (delayAfterLoad >= hookBudget - (Date.now() - navStartTime)) {
        throw new Error("Post-load delay exceeds remaining navigation budget");
      }
      console.log(`Waiting ${delayAfterLoad}ms after load...`);
      await sleep(delayAfterLoad);
    }

    // The page can recover itself during a cooldown or navigate again during
    // the requested post-load delay. Read the committed document, not a stale
    // earlier status paired with its replacement's URL. Never request it again.
    const remainingBudget = hookBudget - (Date.now() - navStartTime);
    if (remainingBudget <= 0) throw new Error("Navigation budget exhausted before document settled");
    const readyStates = waitUntil === "domcontentloaded" ? ["interactive", "complete"] : ["complete"];
    const finalDocument = await withTimeout(async () => {
      while (true) {
        const requestId = mainRequestId;
        const response = mainDocumentResponse;
        const loaderId = mainDocumentLoaderId;
        const { frameTree: currentTree } = await network.send("Page.getFrameTree");
        const state = await page.evaluate(() => ({
          url: location.href,
          readyState: document.readyState,
          contentType: document.contentType,
        })).catch(() => null);
        if (requestId === mainRequestId && readyStates.includes(state?.readyState) &&
            (!requestId || (response && loaderId === currentTree.frame.loaderId))) {
          return { ...state, status: response?.status || null };
        }
        await sleep(50);
      }
    }, remainingBudget, "Current document did not settle within navigation budget");
    const finalUrl = finalDocument.url;
    const contentType = finalDocument.contentType;
    status = finalDocument.status;
    error = status >= 400 ? (error || `HTTP ${status}: document request failed`) : null;
    const elapsed = Date.now() - navStartTime;

    browser.disconnect();

    return { success: !error, error, finalUrl, status, waitUntil, elapsed, challenge, contentType, attempts, retryAfterMs };
  } catch (e) {
    const pageUrl = page && !page.isClosed() ? page.url() : null;
    if (browser) browser.disconnect();
    const elapsed = Date.now() - navStartTime;
    return {
      success: false,
      error: `${e.name}: ${e.message}`,
      isDownload: e.isDownload,
      pageUrl,
      status: observedResponse?.status || null,
      contentType: observedResponse?.contentType || null,
      attempts,
      retryAfterMs,
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
  }
  // Preserve the actual document status and attempt history on failure too.
  const navigationState = {
    waitUntil: result.waitUntil,
    elapsed: result.elapsed,
    url,
    ...(result.finalUrl ? { finalUrl: result.finalUrl } : { pageUrl: result.pageUrl }),
    ...(result.error ? { error: result.error } : {}),
    ...(result.challenge ? { challenge: result.challenge } : {}),
    status: result.status,
    content_type: result.contentType,
    attempts: result.attempts,
    ...(result.retryAfterMs !== null && Number.isFinite(result.retryAfterMs) ? { retry_after_ms: result.retryAfterMs } : {}),
    timestamp: new Date().toISOString(),
  };
  writeFileAtomic(
    path.join(OUTPUT_DIR, "navigation.json"),
    JSON.stringify(navigationState, null, 2)
  );

  const endTs = new Date();

  if (error) {
    const download = result.isDownload === true ? "yes" : result.isDownload === false ? "no" : "unknown";
    const detail = /\bnet::ERR_ABORTED\b/.test(error) ? ` (isDownload=${download})` : "";
    console.error(`ERROR: ${error}${detail}`);
  }

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
