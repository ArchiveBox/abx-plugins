#!/usr/bin/env node
/**
 * Capture redirect chain using CDP during page navigation.
 *
 * This hook sets up CDP listeners BEFORE chrome_navigate to capture the
 * redirect chain from the initial request. It stays alive through navigation
 * and emits JSONL on SIGTERM.
 *
 * Usage: on_Snapshot__25_redirects.bg.js --url=<url> --snapshot-id=<uuid>
 * Output: Writes redirects.jsonl
 */

const fs = require('fs');
const path = require('path');

// Add NODE_MODULES_DIR to module resolution paths if set
if (process.env.NODE_MODULES_DIR) module.paths.unshift(process.env.NODE_MODULES_DIR);

const puppeteer = require('puppeteer-core');

// Import generic helpers from base/utils.js
const {
    getEnvBool,
    getEnvInt,
    parseArgs,
    emitArchiveResult,
} = require('../base/utils.js');

// Import chrome-specific utilities from chrome_utils.js
const {
    connectToPage,
    waitForPageLoaded,
} = require('../chrome/chrome_utils.js');

const PLUGIN_NAME = 'redirects';
const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((process.env.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const OUTPUT_FILE = 'redirects.jsonl';
const CHROME_SESSION_DIR = '../chrome';
const JS_REDIRECT_SETTLE_MS = 10000;

// Global state
let redirectChain = [];
let originalUrl = '';
let finalUrl = '';
let page = null;
let initialRecorded = false;
let lastObservedUrl = '';
const seenTransitions = new Set();

function appendRedirectEntry(outputPath, entry) {
    const key = JSON.stringify([
        entry.from_url || null,
        entry.to_url || null,
        entry.status || null,
    ]);
    if (seenTransitions.has(key)) {
        return false;
    }
    seenTransitions.add(key);
    redirectChain.push(entry);
    fs.appendFileSync(outputPath, JSON.stringify(entry) + '\n');
    if (entry.to_url && entry.to_url.startsWith('http')) {
        finalUrl = entry.to_url;
        lastObservedUrl = entry.to_url;
    }
    return true;
}

async function setupRedirectListener() {
    const outputPath = path.join(OUTPUT_DIR, OUTPUT_FILE);
    const timeout = getEnvInt('REDIRECTS_TIMEOUT', 30) * 1000;

    fs.writeFileSync(outputPath, ''); // Clear existing

    // Connect to Chrome page using shared utility
    const connection = await connectToPage({
        chromeSessionDir: CHROME_SESSION_DIR,
        timeoutMs: timeout,
        puppeteer,
    });
    page = connection.page;

    // Track main-frame navigation requests using Puppeteer's canonical request model.
    page.on('request', (request) => {
        try {
            if (!request.isNavigationRequest() || request.frame() !== page.mainFrame()) {
                return;
            }
            const requestUrl = request.url();
            if (!requestUrl.startsWith('http')) {
                return;
            }

            const requestId = request._requestId || null;
            const redirectChain = request.redirectChain();

            if (!initialRecorded) {
                appendRedirectEntry(outputPath, {
                    timestamp: new Date().toISOString(),
                    from_url: null,
                    to_url: requestUrl,
                    status: null,
                    type: 'initial',
                    request_id: requestId,
                });
                initialRecorded = true;
            }

            const previousRequest = redirectChain.at(-1);
            const previousResponse = previousRequest?.response?.();
            if (previousRequest && previousResponse) {
                appendRedirectEntry(outputPath, {
                    timestamp: new Date().toISOString(),
                    from_url: previousRequest.url(),
                    to_url: requestUrl,
                    status: previousResponse.status(),
                    type: 'http',
                    request_id: requestId,
                });
            }

            finalUrl = requestUrl;
            lastObservedUrl = requestUrl;
        } catch (e) {
            return;
        }
    });

    page.on('framenavigated', (frame) => {
        try {
            if (frame !== page.mainFrame()) return;
            const newUrl = frame.url();
            if (!newUrl || !newUrl.startsWith('http')) return;
            if (!lastObservedUrl) {
                lastObservedUrl = newUrl;
                return;
            }
            if (newUrl === lastObservedUrl) return;
            appendRedirectEntry(outputPath, {
                timestamp: new Date().toISOString(),
                from_url: lastObservedUrl,
                to_url: newUrl,
                status: null,
                type: 'javascript',
            });
        } catch (e) {
            // Ignore frame navigation errors
        }
    });

    // After page loads, check for meta refresh and JS redirects
    page.on('load', async () => {
        try {
            // Small delay to let page settle
            await new Promise(resolve => setTimeout(resolve, 500));

            // Check for meta refresh
            const metaRefresh = await page.evaluate(() => {
                const meta = document.querySelector('meta[http-equiv="refresh"]');
                if (meta) {
                    const content = meta.getAttribute('content') || '';
                    const match = content.match(/url=['"]?([^'";\s]+)['"]?/i);
                    return { content, url: match ? match[1] : null };
                }
                return null;
            });

            if (metaRefresh && metaRefresh.url) {
                appendRedirectEntry(outputPath, {
                    timestamp: new Date().toISOString(),
                    from_url: page.url(),
                    to_url: metaRefresh.url,
                    status: null,
                    type: 'meta_refresh',
                    content: metaRefresh.content,
                });
            }

            // Check for JS redirects
            const jsRedirect = await page.evaluate(() => {
                const html = document.documentElement.outerHTML;
                const patterns = [
                    /window\.location\s*=\s*['"]([^'"]+)['"]/i,
                    /window\.location\.href\s*=\s*['"]([^'"]+)['"]/i,
                    /window\.location\.replace\s*\(\s*['"]([^'"]+)['"]\s*\)/i,
                ];
                for (const pattern of patterns) {
                    const match = html.match(pattern);
                    if (match) return { url: match[1], pattern: pattern.toString() };
                }
                return null;
            });

            if (jsRedirect && jsRedirect.url) {
                appendRedirectEntry(outputPath, {
                    timestamp: new Date().toISOString(),
                    from_url: page.url(),
                    to_url: jsRedirect.url,
                    status: null,
                    type: 'javascript',
                });
            }
        } catch (e) {
            // Ignore errors during meta/js redirect detection
        }
    });

    return { browser: connection.browser, page };
}

async function settleForLateRedirects(page, durationMs, intervalMs = 500) {
    const deadline = Date.now() + durationMs;
    while (Date.now() < deadline) {
        try {
            const currentUrl = page.url();
            if (currentUrl && currentUrl.startsWith('http') && lastObservedUrl && currentUrl !== lastObservedUrl) {
                appendRedirectEntry(path.join(OUTPUT_DIR, OUTPUT_FILE), {
                    timestamp: new Date().toISOString(),
                    from_url: lastObservedUrl,
                    to_url: currentUrl,
                    status: null,
                    type: 'javascript',
                });
            }
        } catch (e) {
            // Ignore transient page access errors while settling.
        }
        await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
}

function emitResult(result) {
    return new Promise((resolve) => {
        const line = JSON.stringify(result) + '\n';
        if (!process.stdout.write(line)) {
            process.stdout.once('drain', resolve);
        } else {
            setImmediate(resolve);
        }
    });
}

async function handleShutdown(signal) {
    console.error(`\nReceived ${signal}, emitting final results...`);

    // Emit final JSONL result to stdout
    const result = {
        type: 'ArchiveResult',
        status: 'succeeded',
        output_str: OUTPUT_FILE,
        plugin: PLUGIN_NAME,
        original_url: originalUrl,
        final_url: finalUrl || originalUrl,
        redirect_count: redirectChain.length,
        is_redirect: redirectChain.length > 0 || (finalUrl && finalUrl !== originalUrl),
    };

    await emitResult(result);
    process.exit(0);
}

async function main() {
    const args = parseArgs();
    const url = args.url;
    const snapshotId = args.snapshot_id;

    if (!url || !snapshotId) {
        console.error('Usage: on_Snapshot__25_redirects.bg.js --url=<url> --snapshot-id=<uuid>');
        process.exit(1);
    }

    originalUrl = url;

    if (!getEnvBool('REDIRECTS_ENABLED', true)) {
        console.error('Skipping (REDIRECTS_ENABLED=False)');
        emitArchiveResult('skipped', 'REDIRECTS_ENABLED=False');
        process.exit(0);
    }

    const timeout = getEnvInt('REDIRECTS_TIMEOUT', 30) * 1000;

    try {
        // Set up redirect listener BEFORE navigation
        await setupRedirectListener();

        // Wait for navigation to settle, then leave extra time for late JS redirects.
        try {
            await waitForPageLoaded(CHROME_SESSION_DIR, timeout * 4, 1000);
        } catch (e) {
            console.error(`WARN: ${e.message}`);
        }
        await settleForLateRedirects(page, JS_REDIRECT_SETTLE_MS);
        await handleShutdown('DONE');

    } catch (e) {
        const error = `${e.name}: ${e.message}`;
        console.error(`ERROR: ${error}`);

        await emitResult({
            type: 'ArchiveResult',
            status: 'failed',
            output_str: error,
        });
        process.exit(1);
    }
}

main().catch(async (e) => {
    console.error(`Fatal error: ${e.message}`);
    await emitResult({
        type: 'ArchiveResult',
        status: 'failed',
        output_str: `${e.name}: ${e.message}`,
    });
    process.exit(1);
});
