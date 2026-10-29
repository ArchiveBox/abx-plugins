#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Extract crawlable anchor links from the rendered page.
 * Runs before final-file PDF consumers flush background hooks and close the
 * snapshot tab; link extraction still needs the live rendered DOM.
 *
 * Usage: on_Snapshot__64_parse_dom_outlinks.js --url=<url>
 * Output: Writes parse_dom_outlinks/urls.jsonl
 *
 * Environment variables:
 *     PARSE_DOM_OUTLINKS_ENABLED: Enable DOM outlinks extraction (default: true)
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
  hasStaticFileOutput,
  isNonHtmlDocument,
  writeFileAtomic,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);
const { connectToPage } = require("../chrome/chrome_utils.js");

// Extractor metadata
const PLUGIN_NAME = "parse_dom_outlinks";
const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || ".").trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const URLS_FILE = "urls.jsonl"; // For crawl system
const CHROME_SESSION_DIR = "../chrome";
const NORESULTS_OUTPUT = "0 URLs parsed";

function unlinkIfExists(filePath) {
  if (fs.existsSync(filePath)) {
    fs.unlinkSync(filePath);
  }
}

// Extract outlinks
async function extractOutlinks(url, depth, timeoutMs) {
  // Output directory is current directory (hook already runs in output dir)
  let browser = null;

  try {
    const connection = await connectToPage({
      chromeSessionDir: CHROME_SESSION_DIR,
      timeoutMs,
      waitForNavigationComplete: true,
      postLoadDelayMs: 200,
    });
    browser = connection.browser;
    const page = connection.page;
    console.log("parsing 1 files for urls...");

    // This hook emits only anchor links in urls.jsonl. Serializing the whole
    // DOM and reading every element's computed style collected unused asset
    // categories, allocating large strings and style objects on expanded pages.
    // Query just the anchors while preserving resolution, order and deduplication.
    const hrefs = await page.evaluate(() => {
      const urls = new Set();
      for (const elem of document.querySelectorAll("a[href]")) {
        const raw = elem.getAttribute("href") || "";
        if (!raw) continue;
        try {
          urls.add(new URL(raw, window.location.href).href);
        } catch {
          urls.add(raw);
        }
      }
      return [...urls];
    });
    const urlsPath = path.join(OUTPUT_DIR, URLS_FILE);
    const crawlableUrls = hrefs.filter((href) => {
      // Only include http/https URLs, exclude static assets
      if (!href.startsWith("http://") && !href.startsWith("https://"))
        return false;
      // Exclude common static file extensions
      const staticExts = [
        ".css",
        ".js",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".mp4",
        ".webm",
        ".mp3",
        ".pdf",
      ];
      const urlPath = href.split("?")[0].split("#")[0].toLowerCase();
      return !staticExts.some((ext) => urlPath.endsWith(ext));
    });

    if (crawlableUrls.length === 0) {
      unlinkIfExists(urlsPath);
      return {
        success: true,
        status: "noresults",
        output: NORESULTS_OUTPUT,
        crawlableCount: 0,
      };
    }

    const urlsJsonl = crawlableUrls
      .map((href) =>
        JSON.stringify({
          type: "Snapshot",
          url: href,
          plugin: PLUGIN_NAME,
          depth: depth + 1,
        })
      )
      .join("\n");

    writeFileAtomic(urlsPath, urlsJsonl + "\n");

    return {
      success: true,
      status: "succeeded",
      output: `${crawlableUrls.length} URLs parsed`,
      crawlableCount: crawlableUrls.length,
    };
  } catch (e) {
    return { success: false, error: `${e.name}: ${e.message}` };
  } finally {
    if (browser) {
      browser.disconnect();
    }
  }
}

async function main() {
  const args = parseArgs();
  const url = args.url;
  const depthValue = args.depth ?? 0;
  const depth = parseInt(String(depthValue), 10) || 0;

  if (!url) {
    console.error("Usage: on_Snapshot__64_parse_dom_outlinks.js --url=<url>");
    emitArchiveResultRecord("failed", "missing required args");
    process.exit(1);
  }

  let status = "failed";
  let output = null;
  let error = "";

  try {
    // Check if enabled
    if (!getEnvBool("PARSE_DOM_OUTLINKS_ENABLED", true)) {
      console.log("Skipping DOM outlinks (PARSE_DOM_OUTLINKS_ENABLED=False)");
      emitArchiveResultRecord("skipped", "disabled by config");
      process.exit(0);
    }

    if (hasStaticFileOutput()) {
      console.error("Skipping parse_dom_outlinks - staticfile extractor already downloaded this");
      emitArchiveResultRecord("noresults", "staticfile already handled");
      process.exit(0);
    }
    if (isNonHtmlDocument()) {
      console.error("Browser document is not HTML");
      emitArchiveResultRecord("noresults", "Browser document is not HTML");
      process.exit(0);
    }

    const timeoutMs =
      getEnvInt("PARSE_DOM_OUTLINKS_TIMEOUT", getEnvInt("TIMEOUT", 30)) * 1000;
    const result = await extractOutlinks(url, depth, timeoutMs);

    if (result.success) {
      status = result.status;
      output = result.output;
      console.log(output);
    } else {
      status = "failed";
      error = result.error;
    }
  } catch (e) {
    error = `${e.name}: ${e.message}`;
    status = "failed";
  }

  if (error) console.error(`ERROR: ${error}`);

  emitArchiveResultRecord(status, output || error || "");

  process.exit(status === "failed" ? 1 : 0);
}

main().catch((e) => {
  console.error(`Fatal error: ${e.message}`);
  emitArchiveResultRecord("failed", `${e.name}: ${e.message}`);
  process.exit(1);
});
