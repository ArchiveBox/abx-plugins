#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Extract SEO metadata from a URL.
 *
 * Extracts all <meta> tags including:
 * - og:* (Open Graph)
 * - twitter:*
 * - description, keywords, author
 * - Any other meta tags
 *
 * Usage: on_Snapshot__38_seo.js --url=<url>
 * Output: Writes seo/seo.json
 *
 * Environment variables:
 *     SAVE_SEO: Enable SEO extraction (default: true)
 */

const fs = require("fs");
const path = require("path");

// Import generic helpers from base/utils.js
const {
  ensureNodeModuleResolution,
  getEnvBool,
  getEnvInt,
  loadConfig,
  parseArgs,
  emitArchiveResultRecord,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);

// Import chrome-specific utilities from chrome_utils.js
const { connectToPage } = require("../chrome/chrome_utils.js");

// Extractor metadata
const PLUGIN_NAME = "seo";
const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || ".").trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const OUTPUT_FILE = "seo.json";
const OUTPUT_PATH_STR = `${PLUGIN_DIR}/${OUTPUT_FILE}`;
const CHROME_SESSION_DIR = "../chrome";

// Extract SEO metadata
async function extractSeo(url) {
  // Output directory is current directory (hook already runs in output dir)
  const outputPath = path.join(OUTPUT_DIR, OUTPUT_FILE);
  const timeout = getEnvInt("SEO_TIMEOUT", getEnvInt("TIMEOUT", 30)) * 1000;
  let browser = null;

  try {
    // Connect to existing Chrome session and get target page
    const connection = await connectToPage({
      chromeSessionDir: CHROME_SESSION_DIR,
      timeoutMs: timeout,
      waitForNavigationComplete: true,
      postLoadDelayMs: 200,
    });
    browser = connection.browser;
    const page = connection.page;
    console.log("extracting seo metadata...");

    // Extract all meta tags
    const seoData = await page.evaluate(() => {
      const metaTags = Array.from(document.querySelectorAll("meta"));
      const seo = {
        url: window.location.href,
        title: document.title || "",
      };

      // Process each meta tag
      metaTags.forEach((tag) => {
        // Get the key (name or property attribute)
        const key =
          tag.getAttribute("name") || tag.getAttribute("property") || "";
        const content = tag.getAttribute("content") || "";

        if (key && content) {
          // Store by key
          seo[key] = content;
        }
      });

      // Also get canonical URL if present
      const canonical = document.querySelector('link[rel="canonical"]');
      if (canonical) {
        seo.canonical = canonical.getAttribute("href");
      }

      // Get language
      const htmlLang = document.documentElement.lang;
      if (htmlLang) {
        seo.language = htmlLang;
      }

      return seo;
    });

    // Archive the social preview image even when it is not displayed in the page.
    for (const name of fs.readdirSync(OUTPUT_DIR)) {
      if (/^featured-image\.[a-z0-9]+$/i.test(name)) fs.unlinkSync(path.join(OUTPUT_DIR, name));
    }
    const imageTypes = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif", "image/avif": "avif", "image/svg+xml": "svg", "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico"};
    const imageURLs = [...new Set([seoData["og:image:secure_url"], seoData["og:image"], seoData["og:image:url"], seoData["twitter:image"], seoData["twitter:image:src"], seoData.image].filter(Boolean))];
    for (const candidate of imageURLs) {
      try {
        const imageURL = new URL(candidate, seoData.url);
        if (!["http:", "https:"].includes(imageURL.protocol)) continue;
        const response = await fetch(imageURL, {signal: AbortSignal.timeout(Math.min(timeout, 10000))});
        const extension = imageTypes[(response.headers.get("content-type") || "").split(";")[0].trim().toLowerCase()];
        if (!response.ok || !extension) { await response.body?.cancel(); continue; }
        const bytes = Buffer.from(await response.arrayBuffer());
        if (!bytes.length) continue;
        fs.writeFileSync(path.join(OUTPUT_DIR, `featured-image.${extension}`), bytes);
        break;
      } catch (error) {
        console.warn(`Could not archive featured image: ${error.message}`);
      }
    }

    // Write output
    fs.writeFileSync(outputPath, JSON.stringify(seoData, null, 2));

    return { success: true, output: OUTPUT_PATH_STR, seoData };
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

  if (!url) {
    console.error("Usage: on_Snapshot__38_seo.js --url=<url>");
    process.exit(1);
  }

  const startTs = new Date();
  let status = "failed";
  let output = null;
  let error = "";

  try {
    // Check if enabled
    if (!getEnvBool("SEO_ENABLED", true)) {
      console.log("Skipping SEO (SEO_ENABLED=False)");
      emitArchiveResultRecord("skipped", "SEO_ENABLED=False");
      process.exit(0);
    }

    const result = await extractSeo(url);

    if (result.success) {
      status = "succeeded";
      output = result.output;
      const metaCount = Object.keys(result.seoData).length - 2; // Subtract url and title
      console.log(`SEO metadata extracted: ${metaCount} meta tags`);
    } else {
      error = result.error;
      status = "failed";
    }
  } catch (e) {
    error = `${e.name}: ${e.message}`;
    status = "failed";
  }

  const endTs = new Date();

  if (error) console.error(`ERROR: ${error}`);

  emitArchiveResultRecord(status, output || error || "");

  process.exit(status === "succeeded" ? 0 : 1);
}

main().catch((e) => {
  console.error(`Fatal error: ${e.message}`);
  process.exit(1);
});
