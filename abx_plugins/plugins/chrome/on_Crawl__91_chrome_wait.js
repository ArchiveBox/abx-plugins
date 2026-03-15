#!/usr/bin/env node
/**
 * Wait for the crawl-level Chrome browser session to become CDP-connectable.
 *
 * This is a foreground crawl hook that blocks later crawl hooks until the
 * shared browser launched by on_Crawl__90_chrome_launch.bg.js is actually
 * reachable over CDP.
 *
 * Usage: on_Crawl__91_chrome_wait.js --url=<url> --snapshot-id=<uuid>
 */

const fs = require('fs');
const path = require('path');
const { parseArgs } = require('../base/utils.js');
if (process.env.NODE_MODULES_DIR) module.paths.unshift(process.env.NODE_MODULES_DIR);
const puppeteer = require('puppeteer');

const PLUGIN_DIR = path.basename(__dirname);
const CRAWL_DIR = path.resolve((process.env.CRAWL_DIR || '.').trim());
const OUTPUT_DIR = path.join(CRAWL_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

const {
    getEnvInt,
    waitForChromeSessionState,
} = require('./chrome_utils.js');

const CHROME_SESSION_DIR = path.join(CRAWL_DIR, 'chrome');
const CHROME_SESSION_REQUIRED_ERROR = 'No Chrome session found (chrome plugin must run first)';

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function waitForConnectableCrawlChromeSession(chromeSessionDir, timeoutMs) {
    const deadline = Date.now() + timeoutMs;

    while (Date.now() < deadline) {
        const remainingMs = Math.max(deadline - Date.now(), 0);
        const state = await waitForChromeSessionState(chromeSessionDir, {
            timeoutMs: Math.min(remainingMs, 500),
            intervalMs: 100,
            requirePid: true,
            requireAlivePid: true,
        });

        if (!state?.cdpUrl) {
            await sleep(Math.min(200, remainingMs));
            continue;
        }

        let browser = null;
        try {
            browser = await puppeteer.connect({
                browserWSEndpoint: state.cdpUrl,
                defaultViewport: null,
            });
            return {
                cdpUrl: state.cdpUrl,
                pid: state.pid,
            };
        } catch (error) {
            await sleep(Math.min(200, remainingMs));
        } finally {
            if (browser) {
                try {
                    browser.disconnect();
                } catch (disconnectError) {}
            }
        }
    }

    return null;
}

async function main() {
    const args = parseArgs();
    const url = args.url;
    const snapshotId = args.snapshot_id;

    if (!url || !snapshotId) {
        console.error('Usage: on_Crawl__91_chrome_wait.js --url=<url> --snapshot-id=<uuid>');
        process.exit(1);
    }

    const timeoutSeconds = getEnvInt('CHROME_TAB_TIMEOUT', getEnvInt('CHROME_TIMEOUT', getEnvInt('TIMEOUT', 60)));
    const timeoutMs = timeoutSeconds * 1000;

    console.error(`[chrome_wait:crawl] Waiting for crawl Chrome session (timeout=${timeoutSeconds}s)...`);

    const readySession = await waitForConnectableCrawlChromeSession(CHROME_SESSION_DIR, timeoutMs);
    if (!readySession) {
        console.error(`[chrome_wait:crawl] ERROR: ${CHROME_SESSION_REQUIRED_ERROR}`);
        console.log(JSON.stringify({ type: 'ArchiveResult', status: 'failed', output_str: CHROME_SESSION_REQUIRED_ERROR }));
        process.exit(1);
    }

    console.error(`[chrome_wait:crawl] Chrome session ready (verified CDP connection, pid=${readySession.pid}, cdp_url=${readySession.cdpUrl.slice(0, 32)}...).`);
    const port = (readySession.cdpUrl.match(/:(\d+)\/devtools\//) || [])[1] || '?';
    console.log(JSON.stringify({ type: 'ArchiveResult', status: 'succeeded', output_str: `browser ready pid=${readySession.pid} port=${port}` }));
    process.exit(0);
}

main().catch(error => {
    console.error(`Fatal error: ${error.message}`);
    process.exit(1);
});
