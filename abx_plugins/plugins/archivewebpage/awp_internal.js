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
 * Map a puppeteer page's CDP target id to its chrome.tabs id.
 *
 * chrome.debugger.getTargets() returns a TargetInfo with both ``id`` (CDP
 * target id, same value as puppeteer's ``page.target()._targetId``) and
 * ``tabId`` (the chrome.tabs integer id) for ``type==='page'`` targets, so
 * the mapping is a direct lookup. Evaluate from the AWP popup page because
 * MV3 service-worker targets are not guaranteed to be attachable in headless
 * Chrome even after Extensions.loadUnpacked returns the extension id.
 */
async function getChromeTabIdForPage(browser, page, extensionId, timeoutMs) {
  const targetId = chromeUtils.getTargetIdFromPage(page);
  if (!targetId) return null;

  const helperPage = await openAwpHelperTab(browser, extensionId, timeoutMs);
  try {
    return await helperPage.evaluate(async (idToFind) => {
      const targets = await new Promise((resolve, reject) => {
        chrome.debugger.getTargets((targetInfos) => {
          const error = chrome.runtime.lastError;
          if (error) {
            reject(new Error(error.message || String(error)));
            return;
          }
          resolve(targetInfos || []);
        });
      });
      const match = targets.find(
        (target) => target.type === "page" && target.id === idToFind
      );
      return match?.tabId ?? null;
    }, targetId);
  } finally {
    try {
      if (helperPage && !helperPage.isClosed()) {
        await helperPage.close({ runBeforeUnload: false });
      }
    } catch (error) {}
  }
}

/**
 * Open the AWP popup as a hidden helper tab. We use Target.createTarget at the
 * popup URL directly (rather than browser.newPage() which starts at
 * about:blank) because AWP's tabs.onCreated handler treats new about:blank
 * tabs opened while a recording is running as candidates for auto-recording,
 * which triggers a Page.reload that destroys our evaluate() context.
 */
async function openAwpHelperTab(browser, extensionId, timeoutMs = 5000) {
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
  const matchesTarget = (target) =>
    chromeUtils.getTargetIdFromTarget(target) === targetId &&
    target.type() === "page";
  const target =
    browser.targets().find(matchesTarget) ||
    (await browser.waitForTarget(matchesTarget, {
      timeout: Math.max(250, timeoutMs),
    }));
  const page = await target.page();
  if (!page) {
    throw new Error(`Helper target ${targetId} is not a page`);
  }
  await page.waitForFunction(
    (expectedUrl) =>
      location.href === expectedUrl &&
      document.readyState !== "loading" &&
      typeof chrome !== "undefined" &&
      Boolean(chrome.runtime?.connect) &&
      Boolean(document.querySelector("wr-popup-viewer")?.port),
    { timeout: Math.max(250, timeoutMs) },
    helperUrl
  );
  return page;
}

/**
 * Resolve the chrome session/plugin dir candidates for the running hook by
 * walking up from process.cwd() (which the runner sets to the plugin output
 * dir). The chrome plugin convention is that its snapshot session markers live
 * at SNAP_DIR/chrome. We also probe SNAP_DIR/chrome/chrome for standalone
 * runs where SNAP_DIR env is the literal "." and the chrome plugin nests its
 * session one level deeper.
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
 * Pick the candidate chrome session dir that owns this snapshot's exact CDP
 * target. A browser endpoint without target_id.txt is crawl-level state and
 * cannot identify a snapshot tab.
 */
function pickChromeSessionDir(candidates) {
  for (const dir of candidates) {
    if (hasSnapshotChromeSession(dir)) return dir;
  }
  return null;
}

/**
 * Return the collection id from the message AWP sends after newColl.
 *
 * Keep this small selector shared with the real-browser regression test: the
 * popup port can already contain other collections messages by the time the
 * newColl response arrives.
 */
function resolveCreatedCollectionId(
  message,
  collectionTitle,
  existingCollectionIds = []
) {
  if (message?.type !== "collections" || !Array.isArray(message.collections)) {
    return null;
  }
  const existingIds = new Set(existingCollectionIds);
  const created = message.collections.find(
    (collection) =>
      collection?.title === collectionTitle &&
      collection.id &&
      !existingIds.has(collection.id)
  );
  return created?.id || null;
}

module.exports = {
  EXTENSION_NAME,
  resolveAwpExtension,
  getChromeTabIdForPage,
  openAwpHelperTab,
  resolveChromeDirs,
  pickChromeSessionDir,
  resolveCreatedCollectionId,
};
