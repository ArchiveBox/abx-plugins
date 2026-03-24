#!/usr/bin/env node
/**
 * Extract and categorize outgoing links from a page's DOM.
 *
 * Categorizes links by type:
 * - hrefs: All <a> links
 * - images: <img src>
 * - css_stylesheets: <link rel=stylesheet>
 * - css_images: CSS background-image: url()
 * - js_scripts: <script src>
 * - iframes: <iframe src>
 * - links: <link> tags with rel/href
 *
 * Usage: on_Snapshot__75_parse_dom_outlinks.js --url=<url>
 * Output: Writes parse_dom_outlinks/urls.jsonl
 *
 * Environment variables:
 *     PARSE_DOM_OUTLINKS_ENABLED: Enable DOM outlinks extraction (default: true)
 */

const fs = require('fs');
const path = require('path');
const {
    ensureNodeModuleResolution,
    getEnvBool,
    getEnvInt,
    loadConfig,
    parseArgs,
    emitArchiveResultRecord,
    writeFileAtomic,
} = require('../base/utils.js');
ensureNodeModuleResolution(module);
const { connectToPage, resolvePuppeteerModule } = require('../chrome/chrome_utils.js');
const puppeteer = resolvePuppeteerModule();

// Extractor metadata
const PLUGIN_NAME = 'parse_dom_outlinks';
const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const URLS_FILE = 'urls.jsonl';  // For crawl system
const CHROME_SESSION_DIR = '../chrome';
const NORESULTS_OUTPUT = '0 URLs parsed';

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
            puppeteer,
        });
        browser = connection.browser;
        const page = connection.page;

        // Extract outlinks by category
        const outlinksData = await page.evaluate(() => {
            const LINK_REGEX = /https?:\/\/(www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b([-a-zA-Z0-9()@:%_\+.~#?&//=]*)/gi;

            const filterDataUrls = (urls) => urls.filter(url => url && !url.startsWith('data:'));
            const filterW3Urls = (urls) => urls.filter(url => url && !url.startsWith('http://www.w3.org/'));

            // Get raw links from HTML
            const html = document.documentElement.outerHTML;
            const raw = Array.from(html.matchAll(LINK_REGEX)).map(m => m[0]);

            // Get all <a href> links
            const hrefs = Array.from(document.querySelectorAll('a[href]'))
                .map(elem => elem.href)
                .filter(url => url);

            // Get all <link> tags (not just stylesheets)
            const linksMap = {};
            document.querySelectorAll('link[href]').forEach(elem => {
                const rel = elem.rel || '';
                const href = elem.href;
                if (href && rel !== 'stylesheet') {
                    linksMap[href] = { rel, href };
                }
            });
            const links = Object.values(linksMap);

            // Get iframes
            const iframes = Array.from(document.querySelectorAll('iframe[src]'))
                .map(elem => elem.src)
                .filter(url => url);

            // Get images
            const images = Array.from(document.querySelectorAll('img[src]'))
                .map(elem => elem.src)
                .filter(url => url && !url.startsWith('data:'));

            // Get CSS background images
            const css_images = Array.from(document.querySelectorAll('*'))
                .map(elem => {
                    const bgImg = window.getComputedStyle(elem).getPropertyValue('background-image');
                    const match = /url\(\s*?['"]?\s*?(\S+?)\s*?["']?\s*?\)/i.exec(bgImg);
                    return match ? match[1] : null;
                })
                .filter(url => url);

            // Get stylesheets
            const css_stylesheets = Array.from(document.querySelectorAll('link[rel=stylesheet]'))
                .map(elem => elem.href)
                .filter(url => url);

            // Get JS scripts
            const js_scripts = Array.from(document.querySelectorAll('script[src]'))
                .map(elem => elem.src)
                .filter(url => url);

            return {
                url: window.location.href,
                raw: [...new Set(filterDataUrls(filterW3Urls(raw)))],
                hrefs: [...new Set(filterDataUrls(hrefs))],
                links,
                iframes: [...new Set(iframes)],
                images: [...new Set(filterDataUrls(images))],
                css_images: [...new Set(filterDataUrls(css_images))],
                css_stylesheets: [...new Set(filterDataUrls(css_stylesheets))],
                js_scripts: [...new Set(filterDataUrls(js_scripts))],
            };
        });

        const urlsPath = path.join(OUTPUT_DIR, URLS_FILE);
        const crawlableUrls = outlinksData.hrefs.filter(href => {
            // Only include http/https URLs, exclude static assets
            if (!href.startsWith('http://') && !href.startsWith('https://')) return false;
            // Exclude common static file extensions
            const staticExts = ['.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.woff', '.woff2', '.ttf', '.eot', '.mp4', '.webm', '.mp3', '.pdf'];
            const urlPath = href.split('?')[0].split('#')[0].toLowerCase();
            return !staticExts.some(ext => urlPath.endsWith(ext));
        });

        if (crawlableUrls.length === 0) {
            unlinkIfExists(urlsPath);
            return {
                success: true,
                status: 'noresults',
                output: NORESULTS_OUTPUT,
                outlinksData,
                crawlableCount: 0,
            };
        }

        const urlsJsonl = crawlableUrls.map(href => JSON.stringify({
            type: 'Snapshot',
            url: href,
            plugin: PLUGIN_NAME,
            depth: depth + 1,
        })).join('\n');

        writeFileAtomic(urlsPath, urlsJsonl + '\n');

        return {
            success: true,
            status: 'succeeded',
            output: `${crawlableUrls.length} URLs parsed`,
            outlinksData,
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
    const depth = parseInt(args.depth || String(hookConfig.SNAPSHOT_DEPTH ?? 0), 10) || 0;

    if (!url) {
        console.error('Usage: on_Snapshot__75_parse_dom_outlinks.js --url=<url>');
        emitArchiveResultRecord('failed', 'missing required args');
        process.exit(1);
    }

    let status = 'failed';
    let output = null;
    let error = '';

    try {
        // Check if enabled
        if (!getEnvBool('PARSE_DOM_OUTLINKS_ENABLED', true)) {
            console.log('Skipping DOM outlinks (PARSE_DOM_OUTLINKS_ENABLED=False)');
            emitArchiveResultRecord('skipped', 'disabled by config');
            process.exit(0);
        }

        const timeoutMs = getEnvInt('PARSE_DOM_OUTLINKS_TIMEOUT', getEnvInt('TIMEOUT', 30)) * 1000;

        const result = await extractOutlinks(url, depth, timeoutMs);

        if (result.success) {
            status = result.status;
            output = result.output;
            const total = result.outlinksData.hrefs.length;
            const crawlable = result.crawlableCount;
            const images = result.outlinksData.images.length;
            const scripts = result.outlinksData.js_scripts.length;
            console.log(`DOM outlinks extracted: ${total} links (${crawlable} crawlable), ${images} images, ${scripts} scripts`);
        } else {
            status = 'failed';
            error = result.error;
        }
    } catch (e) {
        error = `${e.name}: ${e.message}`;
        status = 'failed';
    }

    if (error) console.error(`ERROR: ${error}`);

    emitArchiveResultRecord(status, output || error || '');

    process.exit(status === 'failed' ? 1 : 0);
}

main().catch(e => {
    console.error(`Fatal error: ${e.message}`);
    emitArchiveResultRecord('failed', `${e.name}: ${e.message}`);
    process.exit(1);
});
