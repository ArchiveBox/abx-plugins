/**
 * Internal helpers for the archivewebpage plugin's start/stop hooks.
 *
 * These are AWP-specific glue (popup-port handshake, helper tab spawning,
 * tab-id resolution) and intentionally live in the plugin directory rather
 * than in chrome_utils.js.
 */

const path = require("path");

const chromeUtils = require("../chrome/chrome_utils.js");

const EXTENSION_NAME = "archivewebpage";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Look up the AWP extension id in the chrome plugin's browser.json. The
 * snapshot- and crawl-scoped chrome dirs both write the same metadata, so we
 * try both.
 */
function resolveAwpExtension(chromeSessionDir, crawlChromeDir = null) {
  const sources = [chromeSessionDir, crawlChromeDir].filter(
    (dir, idx, arr) => dir && arr.indexOf(dir) === idx
  );
  for (const dir of sources) {
    const metadata = chromeUtils.readBrowserMetadata(dir);
    const extensions = metadata?.extensions;
    if (!extensions) continue;
    const entry = chromeUtils.findExtensionMetadataByName(
      extensions,
      EXTENSION_NAME
    );
    if (entry?.id) return { id: entry.id, entry };
  }
  return { id: null, entry: null };
}

/**
 * Poll resolveAwpExtension until the metadata appears (chrome plugin writes it
 * asynchronously after extension load) or the deadline is reached.
 */
async function waitForAwpExtension(
  chromeSessionDir,
  crawlChromeDir,
  timeoutMs
) {
  const deadline = Date.now() + Math.max(500, timeoutMs);
  let resolved = resolveAwpExtension(chromeSessionDir, crawlChromeDir);
  while (!resolved.id && Date.now() < deadline) {
    await sleep(100);
    resolved = resolveAwpExtension(chromeSessionDir, crawlChromeDir);
  }
  return resolved;
}

/**
 * Map a puppeteer page's CDP target id to its chrome.tabs id.
 *
 * chrome.debugger.getTargets() returns a TargetInfo with both ``id`` (CDP
 * target id, same value as puppeteer's ``page.target()._targetId``) and
 * ``tabId`` (the chrome.tabs integer id) for ``type==='page'`` targets, so
 * the mapping is a direct lookup. Polls because the AWP service worker may
 * be in the middle of waking up when we first call.
 */
async function getChromeTabIdForPage(browser, page, extensionId, timeoutMs) {
  const targetId = chromeUtils.getTargetIdFromPage(page);
  if (!targetId) return null;

  const deadline = Date.now() + Math.max(1000, timeoutMs);
  while (Date.now() < deadline) {
    const swTarget = await chromeUtils
      .waitForExtensionTargetHandle(
        browser,
        extensionId,
        Math.min(deadline - Date.now(), 2000)
      )
      .catch(() => null);
    if (!swTarget) {
      await sleep(50);
      continue;
    }
    const ctx = await swTarget.worker().catch(() => null);
    if (!ctx) {
      await sleep(50);
      continue;
    }

    try {
      const tabId = await ctx.evaluate(async (idToFind) => {
        const targets = await new Promise((resolve) => {
          chrome.debugger.getTargets((t) => resolve(t || []));
        });
        const match = targets.find(
          (t) => t.type === "page" && t.id === idToFind
        );
        return match?.tabId ?? null;
      }, targetId);
      if (tabId) return tabId;
    } catch (error) {
      // SW may have suspended between calls; loop and try again
    }
    await sleep(75);
  }
  return null;
}

/**
 * Open the AWP popup as a hidden helper tab. We use Target.createTarget at the
 * popup URL directly (rather than browser.newPage() which starts at
 * about:blank) because AWP's tabs.onCreated handler treats new about:blank
 * tabs opened while a recording is running as candidates for auto-recording,
 * which triggers a Page.reload that destroys our evaluate() context.
 */
async function openAwpHelperTab(browser, extensionId) {
  const helperUrl = `chrome-extension://${extensionId}/popup.html`;
  const browserSession = await browser.target().createCDPSession();
  let targetId = null;
  try {
    const result = await browserSession.send("Target.createTarget", {
      url: helperUrl,
    });
    targetId = result.targetId;
  } finally {
    try {
      await browserSession.detach();
    } catch (error) {}
  }
  if (!targetId) {
    throw new Error("Target.createTarget did not return a targetId");
  }
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    const match = browser
      .targets()
      .find(
        (t) =>
          chromeUtils.getTargetIdFromTarget(t) === targetId &&
          t.type() === "page"
      );
    if (match) {
      const page = await match.page();
      if (page) return page;
    }
    await sleep(50);
  }
  throw new Error(`Helper tab target ${targetId} did not become a page`);
}

/**
 * Resolve the chrome session/plugin dir candidates for the running hook by
 * walking up from process.cwd() (which the runner sets to the plugin output
 * dir). The chrome plugin convention is that its snapshot session markers live
 * at SNAP_DIR/chrome. We also probe SNAP_DIR/chrome/chrome because abx-dl's
 * --dir=. mode (where SNAP_DIR env is the literal ".") causes the chrome
 * plugin to nest its session one level deeper.
 */
function resolveChromeDirs(cwd, crawlDirEnv) {
  const outputDir = path.resolve(cwd);
  const siblingChromePluginDir = path.resolve(outputDir, "..", "chrome");
  const crawlChromeDir = crawlDirEnv
    ? path.join(path.resolve(String(crawlDirEnv)), "chrome")
    : null;
  const candidates = [
    siblingChromePluginDir,
    path.join(siblingChromePluginDir, "chrome"),
    crawlChromeDir,
  ].filter((dir, idx, arr) => dir && arr.indexOf(dir) === idx);
  return {
    outputDir,
    crawlChromeDir,
    chromePluginDir: siblingChromePluginDir,
    candidates,
  };
}

const fs = require("fs");

function hasSnapshotChromeSession(dir) {
  if (!dir) return false;
  // The snapshot-level chrome session is identified by target_id.txt being
  // present (cdp_url.txt alone is also written for crawl-level sessions that
  // don't have a snapshot tab yet).
  return fs.existsSync(path.join(dir, "target_id.txt"));
}

/**
 * Pick the candidate chrome session dir that has the snapshot tab's session
 * markers (target_id.txt). Falls back to the first candidate that at least
 * has cdp_url.txt, then to the first candidate.
 */
function pickChromeSessionDir(candidates) {
  for (const dir of candidates) {
    if (hasSnapshotChromeSession(dir)) return dir;
  }
  for (const dir of candidates) {
    if (dir && fs.existsSync(path.join(dir, "cdp_url.txt"))) return dir;
  }
  return candidates[0] || null;
}

/**
 * Wait for one of the candidate chrome session dirs to publish target_id.txt,
 * returning that dir. Falls back to whichever candidate exists if the deadline
 * is reached so the downstream chromeUtils.connectToPage call surfaces a
 * specific error rather than us aborting blindly.
 */
async function waitForChromeSessionDir(candidates, timeoutMs) {
  const deadline = Date.now() + Math.max(500, timeoutMs);
  while (Date.now() < deadline) {
    for (const dir of candidates) {
      if (hasSnapshotChromeSession(dir)) return dir;
    }
    await sleep(100);
  }
  return pickChromeSessionDir(candidates);
}

module.exports = {
  EXTENSION_NAME,
  sleep,
  resolveAwpExtension,
  waitForAwpExtension,
  getChromeTabIdForPage,
  openAwpHelperTab,
  resolveChromeDirs,
  pickChromeSessionDir,
  waitForChromeSessionDir,
};
