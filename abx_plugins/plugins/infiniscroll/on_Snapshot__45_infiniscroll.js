#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Scroll the page down to trigger infinite scroll / lazy loading.
 *
 * Scrolls down 1 page at a time, up to INFINISCROLL_SCROLL_LIMIT times,
 * ensuring at least INFINISCROLL_MIN_HEIGHT (default 16,000px) is reached.
 * Stops early if no new content loads after a scroll.
 *
 * Optionally expands <details> elements and clicks "load more" buttons.
 *
 * Usage: on_Snapshot__45_infiniscroll.js --url=<url>
 * Output: JSONL with scroll stats (no files created)
 *
 * Environment variables:
 *     INFINISCROLL_ENABLED: Enable/disable (default: true)
 *     INFINISCROLL_TIMEOUT: Max timeout in seconds (default: 120)
 *     INFINISCROLL_SCROLL_DELAY: Delay between scrolls in ms (default: 2000)
 *     INFINISCROLL_SCROLL_DISTANCE: Pixels per scroll (default: 1600)
 *     INFINISCROLL_SCROLL_LIMIT: Max scroll iterations (default: 10)
 *     INFINISCROLL_MIN_HEIGHT: Min page height to reach in px (default: 16000)
 *     INFINISCROLL_EXPAND_DETAILS: Expand <details> and comments (default: true)
 */

const fs = require("fs");
const path = require("path");
const { loadConfig } = require("../base/utils.js");

const PLUGIN_DIR = path.basename(__dirname);
// Import generic helpers from base/utils.js
const {
  ensureNodeModuleResolution,
  getEnvBool,
  getEnvInt,
  parseArgs,
  emitArchiveResultRecord,
  hasStaticFileOutput,
  isNonHtmlDocument,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);

// Import chrome-specific utilities from chrome_utils.js
const {
  connectToPage,
  resolvePuppeteerModule,
} = require("../chrome/chrome_utils.js");

const PLUGIN_NAME = "infiniscroll";
const CHROME_SESSION_DIR = "../chrome";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Broad feeds contain unrelated, effectively unbounded content. Individual
// posts and profiles retain the normal configured capture limits.
function isHomeFeed(value) {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    const site = [
      "reddit.com",
      "facebook.com",
      "instagram.com",
      "tiktok.com",
    ].find((domain) => host === domain || host.endsWith(`.${domain}`));
    const pathname = url.pathname.replace(/\/+$/, "") || "/";
    if (site === "reddit.com")
      return /^\/(?:hot|new|best|top|r\/(?:all|popular)(?:\/(?:hot|new|top|rising))?)?$/.test(
        pathname,
      );
    if (site === "facebook.com")
      return (
        /^\/(?:home(?:\.php)?|feeds?)?$/.test(pathname) &&
        !["story_fbid", "fbid", "v"].some((key) => url.searchParams.has(key))
      );
    if (site === "instagram.com")
      return /^\/(?:explore|reels)?$/.test(pathname);
    if (site === "tiktok.com")
      return /^\/(?:foryou|following|explore)?$/.test(pathname);
    return false;
  } catch {
    return false;
  }
}

// Only an unambiguous article can bound scrolling. Feeds and pages whose
// structured identity disagrees with the visible page retain normal scrolling.
async function getArticle(page) {
  const handle = await page.evaluateHandle(() => {
    const visible = (el) => el.getClientRects().length > 0;
    const mains = [...document.querySelectorAll('main, [role="main"]')].filter(
      visible,
    );
    const headings = [...document.querySelectorAll("h1")].filter(visible);
    const canonical = document.querySelectorAll('link[rel="canonical"]');
    if (mains.length !== 1 || headings.length !== 1 || canonical.length !== 1)
      return null;
    // A separate semantic article may be a continuation or comments with lazy
    // assets. Do not assume main contains everything valuable on such pages.
    if (
      [...document.querySelectorAll('article, [role="article"]')].some(
        (el) => visible(el) && !mains[0].contains(el),
      )
    )
      return null;
    const normalizeUrl = (value) => {
      if (typeof value !== "string" || !value.trim()) return null;
      try {
        const url = new URL(value, location.href);
        url.hash = "";
        return url.href;
      } catch {
        return null;
      }
    };
    const identity = normalizeUrl(canonical[0].href);
    if (!identity || identity !== normalizeUrl(location.href)) return null;
    const nodes = [];
    for (const script of document.querySelectorAll(
      'script[type="application/ld+json"]',
    )) {
      try {
        const data = JSON.parse(script.textContent);
        nodes.push(...(Array.isArray(data) ? data : [data]));
      } catch {
        /* Invalid optional metadata cannot establish an article boundary. */
      }
    }
    const articles = [];
    for (const node of nodes) {
      if (!node || typeof node !== "object") continue;
      if (Array.isArray(node["@graph"])) nodes.push(...node["@graph"]);
      const types = Array.isArray(node["@type"])
        ? node["@type"]
        : [node["@type"]];
      if (
        types.some((type) =>
          ["Article", "NewsArticle", "BlogPosting"].includes(type),
        )
      )
        articles.push(node);
    }
    if (articles.length !== 1) return null;
    const article = articles[0];
    const pageIdentity = article.mainEntityOfPage;
    const articleUrl =
      typeof pageIdentity === "string" ? pageIdentity : pageIdentity?.["@id"];
    if (
      normalizeUrl(articleUrl || article.url || article["@id"]) !== identity ||
      typeof article.headline !== "string"
    )
      return null;
    const headline = document.createElement("textarea");
    headline.innerHTML = article.headline;
    const normalizeText = (value) => value.trim().replace(/\s+/g, " ");
    if (
      normalizeText(headline.value) !== normalizeText(headings[0].textContent)
    )
      return null;
    if (
      mains[0].getBoundingClientRect().bottom <
      headings[0].getBoundingClientRect().bottom
    )
      return null;
    return mains[0];
  });
  const article = handle.asElement();
  if (!article) await handle.dispose();
  return article;
}

/**
 * Expand <details> elements and click "load more" buttons for comments.
 * Based on archivebox.ts expandComments function.
 */
async function expandDetails(page, options = {}) {
  const { timeout = 30000, limit = 500, delay = 500, article = null } = options;

  const startTime = Date.now();

  // First, expand all <details> elements
  const details = await page.evaluate((article) => {
    let count = 0;
    // Comments and other expandable content may belong to this story even
    // when the publisher places them outside main. Keep the full-page path.
    const bounded =
      !!article &&
      [...document.querySelectorAll("details")].every((el) =>
        article.contains(el),
      );
    // Generic <details> elements
    document.querySelectorAll("details:not([open])").forEach((el) => {
      el.open = true;
      count++;
    });
    // Github README details sections
    document.querySelectorAll("article details:not([open])").forEach((el) => {
      el.open = true;
      count++;
    });
    // Github issue discussion hidden comments
    document
      .querySelectorAll(
        "div.js-discussion details:not(.details-overlay):not([open])",
      )
      .forEach((el) => {
        el.open = true;
        count++;
      });
    // HedgeDoc/Markdown details sections
    document
      .querySelectorAll(".markdown-body details:not([open])")
      .forEach((el) => {
        el.open = true;
        count++;
      });
    return { count, bounded };
  }, article);
  const detailsExpanded = details.count;

  if (detailsExpanded > 0) {
    console.error(`Expanded ${detailsExpanded} <details> elements`);
  }

  // Then click "load more" buttons for comments
  const expansion = await page.evaluate(
    async ({ timeout, limit, delay }, article) => {
      let bounded = !!article;
      const wait = (ms) => new Promise((res) => setTimeout(res, ms));

      const inactive = new WeakSet();

      // Select expansion actions from their current state, not an ambiguous
      // toggle handler. A disclosure that is already open must stay open.
      const canExpand = (element) => {
        const label = (element.textContent || "")
          .trim()
          .replace(/\s+/g, " ")
          .toLowerCase();
        const expansionAction =
          element.matches(
            'faceplate-partial[loading=action], a[onclick^="return morechildren"], a.morelink',
          ) ||
          label === "show replies" ||
          label.includes("show more") ||
          label.includes("load more") ||
          /^\[\s*\+\s*\]$/.test(label);
        if (
          !expansionAction ||
          inactive.has(element) ||
          element.getAttribute("aria-expanded") === "true" ||
          element.control?.checked ||
          (element.matches("summary") && element.parentElement?.open) ||
          element.slot === "children" ||
          element.disabled ||
          !element.getClientRects().length
        )
          return false;
        // ARIA true means a menu popup, including labels for native controls.
        if (
          [element, element.control].some((control) =>
            ["true", "menu"].includes(control?.getAttribute("aria-haspopup")),
          )
        )
          return false;
        // "Show more" can be a navigation link (e.g. X's recommended
        // people sidebar), not an expansion control. Never leave the
        // captured document to expand content, including nested controls.
        const anchor = element.closest("a[href]");
        if (anchor) {
          const href = anchor.getAttribute("href").trim();
          if (href && !href.startsWith("#") && !/^javascript:/i.test(href)) {
            return false;
          }
        }
        return true;
      };
      const getLoadMoreLinks = () =>
        [
          ...document.querySelectorAll(
            'faceplate-partial[loading=action], a, button, [role="button"]',
          ),
        ].filter(canExpand);

      let expanded = 0;
      let loadMoreLinks = getLoadMoreLinks();
      if (bounded && loadMoreLinks.some((link) => !article.contains(link)))
        bounded = false;
      const startTime = Date.now();

      while (loadMoreLinks.length > 0) {
        for (const link of loadMoreLinks) {
          // An earlier click can change another control in this same batch.
          if (!link.isConnected || !canExpand(link)) continue;

          try {
            const before = document.body.innerText;
            if (bounded && (!article.isConnected || !article.contains(link)))
              bounded = false;
            if (bounded) {
              window.scrollTo({
                top: Math.min(
                  link.getBoundingClientRect().top + scrollY,
                  Math.max(
                    0,
                    article.getBoundingClientRect().bottom +
                      scrollY -
                      innerHeight,
                  ),
                ),
                left: 0,
                behavior: "instant",
              });
            } else {
              link.scrollIntoView({ behavior: "instant" });
            }
            link.click();
            expanded++;
            await wait(delay);
            if (document.body.innerText === before) inactive.add(link);
          } catch (e) {
            inactive.add(link);
          }

          // Check limits
          if (expanded >= limit) return { expanded, bounded };
          if (Date.now() - startTime >= timeout) return { expanded, bounded };
        }

        // Check for new load more links after clicking
        await wait(delay);
        loadMoreLinks = getLoadMoreLinks();
      }

      return { expanded, bounded };
    },
    { timeout, limit, delay },
    details.bounded ? article : null,
  );
  const numExpanded = expansion.expanded;

  if (numExpanded > 0) {
    console.error(`Clicked ${numExpanded} "load more" buttons`);
  }

  return {
    article: expansion.bounded ? article : null,
    detailsExpanded,
    commentsExpanded: numExpanded,
    total: detailsExpanded + numExpanded,
  };
}

async function scrollDown(page, options = {}) {
  const {
    timeout = 120000,
    scrollDelay = 2000,
    scrollDistance = 1600,
    scrollLimit = 10,
    minHeight = 16000,
    expandedTotal = 0,
    article = null,
  } = options;

  const startTime = Date.now();

  // Get page height using multiple methods (some pages use different scroll containers)
  const getPageHeight = () =>
    page.evaluate(() => {
      return Math.max(
        document.body.scrollHeight || 0,
        document.body.offsetHeight || 0,
        document.documentElement.scrollHeight || 0,
        document.documentElement.offsetHeight || 0,
      );
    });

  const startingHeight = await getPageHeight();
  let lastHeight = startingHeight;
  let scrollCount = 0;
  let scrollPosition = 0;

  console.error(`Initial page height: ${startingHeight}px`);
  console.log(
    `Scrolled ${startingHeight.toLocaleString()}px | expanded ${expandedTotal.toLocaleString()} sections`,
  );

  // Instant scrolling also works in background tabs where animation frames pause.
  // Scroll to top first
  await page.evaluate(() => {
    window.scrollTo({ top: 0, left: 0, behavior: "instant" });
  });
  await sleep(500);

  while (scrollCount < scrollLimit) {
    // Check timeout
    const elapsed = Date.now() - startTime;
    if (elapsed >= timeout) {
      console.error(`Timeout reached after ${scrollCount} scrolls`);
      break;
    }

    scrollPosition = await page.evaluate(
      (article, requested) => {
        // Use the live element: lazy assets and expanded sections can grow it.
        if (!article?.isConnected) return requested;
        return Math.min(
          requested,
          Math.max(
            0,
            article.getBoundingClientRect().bottom + scrollY - innerHeight,
          ),
        );
      },
      article,
      (scrollCount + 1) * scrollDistance,
    );
    console.error(
      `Scrolling down ${
        scrollCount + 1
      }x ${scrollDistance}px... (${scrollPosition}/${lastHeight})`,
    );

    await page.evaluate((yOffset) => {
      window.scrollTo({ top: yOffset, left: 0, behavior: "instant" });
    }, scrollPosition);

    scrollCount++;
    await sleep(scrollDelay);

    // Check if new content was added (infinite scroll detection)
    const newHeight = await getPageHeight();
    const addedPx = newHeight - lastHeight;

    if (addedPx > 0) {
      console.error(
        `Detected infini-scrolling: ${lastHeight}+${addedPx} => ${newHeight}`,
      );
    } else if (scrollPosition >= newHeight + scrollDistance) {
      // Reached the bottom
      if (scrollCount > 2) {
        console.error(`Reached bottom of page at ${newHeight}px`);
        break;
      }
    }

    lastHeight = newHeight;
    console.log(
      `Scrolled ${lastHeight.toLocaleString()}px | expanded ${expandedTotal.toLocaleString()} sections`,
    );

    if (
      article &&
      (await article.evaluate(
        (el) =>
          el.isConnected &&
          el.getBoundingClientRect().bottom <= innerHeight + 1,
      ))
    ) {
      console.error("Reached end of original article");
      break;
    }

    // Check if we've reached minimum height and can stop
    if (lastHeight >= minHeight && scrollPosition >= lastHeight) {
      console.error(`Reached minimum height target (${minHeight}px)`);
      break;
    }
  }

  // Scroll back to top
  console.error(`Finished ${scrollCount} scrolls, scrolling back to top...`);
  await page.evaluate(() => {
    window.scrollTo({ top: 0, left: 0, behavior: "instant" });
  });
  await sleep(scrollDelay);

  const totalElapsed = Date.now() - startTime;

  return {
    scrollCount,
    finalHeight: lastHeight,
    startingHeight,
    elapsedMs: totalElapsed,
  };
}

async function main() {
  const hookConfig = loadConfig();
  const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || ".").trim());
  const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
  if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  }
  process.chdir(OUTPUT_DIR);

  // Check if infiniscroll is enabled BEFORE requiring puppeteer
  if (!getEnvBool("INFINISCROLL_ENABLED", true)) {
    console.error("Skipping infiniscroll (INFINISCROLL_ENABLED=False)");
    emitArchiveResultRecord("skipped", "INFINISCROLL_ENABLED=False");
    process.exit(0);
  }

  const puppeteer = resolvePuppeteerModule();

  const args = parseArgs();
  const url = args.url;

  if (!url) {
    console.error("Usage: on_Snapshot__45_infiniscroll.js --url=<url>");
    process.exit(1);
  }

  const timeout = getEnvInt("INFINISCROLL_TIMEOUT", 120) * 1000;
  const scrollDelay = getEnvInt("INFINISCROLL_SCROLL_DELAY", 2000);
  let scrollDistance = getEnvInt("INFINISCROLL_SCROLL_DISTANCE", 1600);
  let scrollLimit = getEnvInt("INFINISCROLL_SCROLL_LIMIT", 10);
  const minHeight = getEnvInt("INFINISCROLL_MIN_HEIGHT", 16000);
  let expandDetailsEnabled = getEnvBool("INFINISCROLL_EXPAND_DETAILS", true);

  let browser = null;
  try {
    if (hasStaticFileOutput()) {
      console.error(
        "Skipping infiniscroll - staticfile extractor already downloaded this",
      );
      emitArchiveResultRecord("noresults", "staticfile already handled");
      process.exit(0);
    }
    if (isNonHtmlDocument()) {
      console.error("Browser document is not HTML");
      emitArchiveResultRecord("noresults", "Browser document is not HTML");
      process.exit(0);
    }

    const connectTimeoutMs = Math.min(timeout, getEnvInt("TIMEOUT", 30) * 1000);
    const connection = await connectToPage({
      chromeSessionDir: CHROME_SESSION_DIR,
      timeoutMs: connectTimeoutMs,
      waitForNavigationComplete: true,
      postLoadDelayMs: 200,
      puppeteer,
    });
    browser = connection.browser;
    const page = connection.page;
    if (isHomeFeed(url) || isHomeFeed(page.url())) {
      scrollLimit = Math.min(scrollLimit, 2);
      scrollDistance = Math.min(
        scrollDistance,
        await page.evaluate(() => window.innerHeight),
      );
      expandDetailsEnabled = false;
      console.error("Home feed: limiting capture to three screenfuls");
    }

    console.error(`Starting infinite scroll on ${url}`);
    console.log("Scrolled 0px | expanded 0 sections | scrolling...");

    const initialArticle = await getArticle(page);
    let article = initialArticle;

    // Expand <details> and comments before scrolling (if enabled)
    let expandResult = { total: 0, detailsExpanded: 0, commentsExpanded: 0 };
    if (expandDetailsEnabled) {
      console.error("Expanding <details> and comments...");
      expandResult = await expandDetails(page, {
        timeout: Math.min(timeout / 4, 30000),
        limit: 500,
        delay: scrollDelay / 4,
        article,
      });
      article = expandResult.article;
    }

    const result = await scrollDown(page, {
      timeout,
      scrollDelay,
      scrollDistance,
      scrollLimit,
      minHeight,
      expandedTotal: expandResult.total,
      article,
    });

    // Expand again after scrolling (new content may have loaded)
    if (expandDetailsEnabled) {
      const expandResult2 = await expandDetails(page, {
        timeout: Math.min(timeout / 4, 30000),
        limit: 500,
        delay: scrollDelay / 4,
        article,
      });
      expandResult.total += expandResult2.total;
      expandResult.detailsExpanded += expandResult2.detailsExpanded;
      expandResult.commentsExpanded += expandResult2.commentsExpanded;
    }

    if (initialArticle) await initialArticle.dispose();
    browser.disconnect();

    const finalHeightStr = result.finalHeight.toLocaleString();
    const outputStr = `scrolled ${finalHeightStr}px`;

    console.error(`Success: ${outputStr}`);
    emitArchiveResultRecord("succeeded", outputStr);
    process.exit(0);
  } catch (e) {
    if (browser) browser.disconnect();
    console.error(`ERROR: ${e.name}: ${e.message}`);
    process.exit(1);
  }
}

module.exports = { isHomeFeed };

if (require.main === module)
  main().catch((e) => {
    console.error(`Fatal error: ${e.message}`);
    process.exit(1);
  });
