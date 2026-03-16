#!/usr/bin/env node
/**
 * Extract accessibility tree and page outline from a URL.
 *
 * Extracts:
 * - Page outline (headings h1-h6, sections, articles)
 * - Iframe tree
 * - Accessibility snapshot
 * - ARIA labels and roles
 *
 * Usage: on_Snapshot__39_accessibility.js --url=<url> --snapshot-id=<uuid>
 * Output: Writes accessibility/accessibility.json
 *
 * Environment variables:
 *     SAVE_ACCESSIBILITY: Enable accessibility extraction (default: true)
 */

const fs = require('fs');
const path = require('path');
const {
    ensureNodeModuleResolution,
    getEnvBool,
    getEnvInt,
    parseArgs,
    emitArchiveResult,
} = require('../base/utils.js');
ensureNodeModuleResolution(module);
const puppeteer = require('puppeteer-core');
const {
    readCdpUrl,
    connectToPage,
    waitForPageLoaded,
} = require('../chrome/chrome_utils.js');

// Extractor metadata
const PLUGIN_NAME = 'accessibility';
const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((process.env.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const OUTPUT_FILE = 'accessibility.json';
const CHROME_SESSION_DIR = '../chrome';

// Extract accessibility info
async function extractAccessibility(url, timeoutMs) {
    // Output directory is current directory (hook already runs in output dir)
    const outputPath = path.join(OUTPUT_DIR, OUTPUT_FILE);

    let browser = null;

    try {
        if (!readCdpUrl(CHROME_SESSION_DIR)) {
            return { success: false, error: 'No Chrome session found (chrome plugin must run first)' };
        }

        const connection = await connectToPage({
            chromeSessionDir: CHROME_SESSION_DIR,
            timeoutMs,
            puppeteer,
        });
        browser = connection.browser;
        const page = connection.page;
        await waitForPageLoaded(CHROME_SESSION_DIR, timeoutMs * 4, 200);

        // Get accessibility snapshot
        const accessibilityTree = await page.accessibility.snapshot({ interestingOnly: true });

        // Extract page outline (headings, sections, etc.)
        const outline = await page.evaluate(() => {
            const headings = [];
            const elements = document.querySelectorAll(
                'h1, h2, h3, h4, h5, h6, a[name], header, footer, article, main, aside, nav, section, figure, summary, table, form, iframe'
            );

            elements.forEach(elem => {
                // Skip unnamed anchors
                if (elem.tagName.toLowerCase() === 'a' && !elem.name) return;

                const tagName = elem.tagName.toLowerCase();
                const elemId = elem.id || elem.name || elem.getAttribute('aria-label') || elem.role || '';
                const elemClasses = (elem.className || '').toString().trim().split(/\s+/).slice(0, 3).join(' .');
                const action = elem.action?.split('/').pop() || '';

                let summary = (elem.innerText || '').slice(0, 128);
                if (summary.length >= 128) summary += '...';

                let prefix = '';
                let title = '';

                // Format headings with # prefix
                const level = parseInt(tagName.replace('h', ''));
                if (!isNaN(level)) {
                    prefix = '#'.repeat(level);
                    title = elem.innerText || elemId || elemClasses;
                } else {
                    // For other elements, create breadcrumb path
                    const parents = [tagName];
                    let node = elem.parentNode;
                    while (node && parents.length < 5) {
                        if (node.tagName) {
                            const tag = node.tagName.toLowerCase();
                            if (!['div', 'span', 'p', 'body', 'html'].includes(tag)) {
                                parents.unshift(tag);
                            } else {
                                parents.unshift('');
                            }
                        }
                        node = node.parentNode;
                    }
                    prefix = parents.join('>');

                    title = elemId ? `#${elemId}` : '';
                    if (!title && elemClasses) title = `.${elemClasses}`;
                    if (action) title += ` /${action}`;
                    if (summary && !title.includes(summary)) title += `: ${summary}`;
                }

                // Clean up title
                title = title.replace(/\s+/g, ' ').trim();

                if (prefix) {
                    headings.push(`${prefix} ${title}`);
                }
            });

            return headings;
        });

        // Get iframe tree
        const iframes = [];
        function dumpFrameTree(frame, indent = '>') {
            iframes.push(indent + frame.url());
            for (const child of frame.childFrames()) {
                dumpFrameTree(child, indent + '>');
            }
        }
        dumpFrameTree(page.mainFrame(), '');

        const accessibilityData = {
            url,
            headings: outline,
            iframes,
            tree: accessibilityTree,
        };

        // Write output
        fs.writeFileSync(outputPath, JSON.stringify(accessibilityData, null, 2));

        return { success: true, output: outputPath, accessibilityData };

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
    const snapshotId = args.snapshot_id;

    if (!url || !snapshotId) {
        console.error('Usage: on_Snapshot__39_accessibility.js --url=<url> --snapshot-id=<uuid>');
        process.exit(1);
    }

    const startTs = new Date();
    let status = 'failed';
    let output = null;
    let error = '';

    try {
        // Check if enabled
        if (!getEnvBool('ACCESSIBILITY_ENABLED', true)) {
            console.log('Skipping accessibility (ACCESSIBILITY_ENABLED=False)');
            // Output clean JSONL (no RESULT_JSON= prefix)
            emitArchiveResult('skipped', 'ACCESSIBILITY_ENABLED=False');
            process.exit(0);
        }

        const timeoutMs = getEnvInt('ACCESSIBILITY_TIMEOUT', getEnvInt('TIMEOUT', 30)) * 1000;
        const result = await extractAccessibility(url, timeoutMs);

        if (result.success) {
            status = 'succeeded';
            output = result.output;
            const headingCount = result.accessibilityData.headings.length;
            const iframeCount = result.accessibilityData.iframes.length;
            console.log(`Accessibility extracted: ${headingCount} headings, ${iframeCount} iframes`);
        } else {
            status = 'failed';
            error = result.error;
        }
    } catch (e) {
        error = `${e.name}: ${e.message}`;
        status = 'failed';
    }

    const endTs = new Date();

    if (error) console.error(`ERROR: ${error}`);

    emitArchiveResult(status, output || error || '');

    process.exit(status === 'succeeded' ? 0 : 1);
}

main().catch(e => {
    console.error(`Fatal error: ${e.message}`);
    process.exit(1);
});
