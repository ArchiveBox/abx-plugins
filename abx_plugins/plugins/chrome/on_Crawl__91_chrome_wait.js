#!/usr/bin/env node
/**
 * Wait for the crawl-level Chrome browser session to become CDP-connectable.
 *
 * This is a foreground crawl hook that blocks later crawl hooks until the
 * shared browser launched by on_Crawl__90_chrome_launch.daemon.bg.js is actually
 * reachable over CDP.
 *
 * Usage: on_Crawl__91_chrome_wait.js --url=<url> --snapshot-id=<uuid>
 */

const fs = require('fs');
const path = require('path');
const { ensureNodeModuleResolution, parseArgs, getEnv, getEnvBool, getEnvInt } = require('../base/utils.js');
ensureNodeModuleResolution(module);
const puppeteer = require('puppeteer');

const PLUGIN_DIR = path.basename(__dirname);
const CRAWL_DIR = path.resolve((process.env.CRAWL_DIR || '.').trim());
const OUTPUT_DIR = path.join(CRAWL_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

const {
    waitForChromeSessionState,
    connectToBrowserEndpoint,
} = require('./chrome_utils.js');

const CHROME_SESSION_DIR = path.join(CRAWL_DIR, 'chrome');
const CHROME_SESSION_REQUIRED_ERROR = 'No Chrome session found (chrome plugin must run first)';

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function waitForConnectableCrawlChromeSession(chromeSessionDir, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    const processIsLocal = getEnv('CHROME_CDP_URL', '') ? false : getEnvBool('CHROME_IS_LOCAL', true);
    let lastError = CHROME_SESSION_REQUIRED_ERROR;

    while (Date.now() < deadline) {
        const remainingMs = Math.max(deadline - Date.now(), 0);
        const state = await waitForChromeSessionState(chromeSessionDir, {
            timeoutMs: Math.min(remainingMs, 500),
            intervalMs: 100,
            requirePid: processIsLocal,
            requireAlivePid: processIsLocal,
            processIsLocal,
        });

        if (!state?.cdpUrl) {
            lastError = CHROME_SESSION_REQUIRED_ERROR;
            await sleep(Math.min(200, remainingMs));
            continue;
        }

        let browser = null;
        try {
            browser = await connectToBrowserEndpoint(puppeteer, state.cdpUrl, { defaultViewport: null });
            return {
                cdpUrl: state.cdpUrl,
                pid: state.pid,
            };
        } catch (error) {
            lastError = error?.message || String(error);
            await sleep(Math.min(200, remainingMs));
        } finally {
            if (browser) {
                try {
                    browser.disconnect();
                } catch (disconnectError) {}
            }
        }
    }

    return {
        cdpUrl: null,
        pid: null,
        error: lastError,
    };
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
    if (!readySession?.cdpUrl) {
        const error = readySession?.error || CHROME_SESSION_REQUIRED_ERROR;
        console.error(`[chrome_wait:crawl] ERROR: ${error}`);
        console.log(JSON.stringify({ type: 'ArchiveResult', status: 'failed', output_str: error }));
        process.exit(1);
    }

    const pid = readySession.pid || 'external';
    let port = '?';
    try {
        const endpoint = new URL(readySession.cdpUrl);
        port = endpoint.port || (endpoint.protocol === 'https:' || endpoint.protocol === 'wss:' ? '443' : '80');
    } catch (error) {}

    console.error(`[chrome_wait:crawl] Chrome session ready (verified CDP connection, pid=${pid}, cdp_url=${readySession.cdpUrl.slice(0, 32)}...).`);
    console.log(JSON.stringify({ type: 'ArchiveResult', status: 'succeeded', output_str: `browser ready pid=${pid} port=${port}` }));
    process.exit(0);
}

main().catch(error => {
    console.error(`Fatal error: ${error.message}`);
    process.exit(1);
});
