#!/usr/bin/env node
/**
 * Create a Chrome tab for this snapshot in the shared crawl Chrome session.
 *
 * Connects to the crawl-level Chrome session (from on_Crawl__90_chrome_launch.bg.js)
 * and creates a new tab. This hook does NOT launch its own Chrome instance.
 *
 * Usage: on_Snapshot__10_chrome_tab.bg.js --url=<url> --snapshot-id=<uuid> --crawl-id=<uuid>
 * Output: Creates chrome/ directory under snapshot output dir with:
 *   - cdp_url.txt: WebSocket URL for CDP connection
 *   - chrome.pid: Chrome process ID (from crawl)
 *   - target_id.txt: Target ID of this snapshot's tab
 *   - url.txt: The URL to be navigated to
 *
 * Environment variables:
 *     CRAWL_DIR: Crawl output directory (to find crawl's Chrome session)
 *     CHROME_BINARY: Path to Chromium binary (optional, for version info)
 *
 * This is a background hook that stays alive until SIGTERM so the tab
 * can be closed cleanly at the end of the snapshot run.
 */

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');
// Add NODE_MODULES_DIR to module resolution paths if set
if (process.env.NODE_MODULES_DIR) module.paths.unshift(process.env.NODE_MODULES_DIR);

const puppeteer = require('puppeteer');
const {
    getEnv,
    getEnvInt,
    readCdpUrl,
    readTargetId,
    waitForCrawlChromeSession,
    openTabInChromeSession,
    closeTabInChromeSession,
} = require('./chrome_utils.js');

// Extractor metadata
const PLUGIN_NAME = 'chrome_tab';
const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((process.env.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const CHROME_SESSION_DIR = '.';

let finalStatus = 'failed';
let finalOutput = '';
let finalError = '';
let cmdVersion = '';
let finalized = false;

// Parse command line arguments
function parseArgs() {
    const args = {};
    process.argv.slice(2).forEach(arg => {
        if (arg.startsWith('--')) {
            const [key, ...valueParts] = arg.slice(2).split('=');
            args[key.replace(/-/g, '_')] = valueParts.join('=') || true;
        }
    });
    return args;
}

function emitResult(statusOverride) {
    if (finalized) return;
    finalized = true;

    const status = statusOverride || finalStatus;
    const outputStr = status === 'succeeded'
        ? finalOutput
        : (finalError || finalOutput || '');

    const result = {
        type: 'ArchiveResult',
        status,
        output_str: outputStr,
    };
    if (cmdVersion) {
        result.cmd_version = cmdVersion;
    }
    console.log(JSON.stringify(result));
}

// Cleanup handler for SIGTERM - close this snapshot's tab
async function cleanup(signal) {
    if (signal) {
        console.error(`\nReceived ${signal}, closing chrome tab...`);
    }
    try {
        const cdpUrl = readCdpUrl(OUTPUT_DIR);
        const targetId = readTargetId(OUTPUT_DIR);
        await closeTabInChromeSession({ cdpUrl, targetId, puppeteer });
    } catch (e) {
        // Best effort
    }
    emitResult();
    process.exit(finalStatus === 'succeeded' ? 0 : 1);
}

// Register signal handlers
process.on('SIGTERM', () => cleanup('SIGTERM'));
process.on('SIGINT', () => cleanup('SIGINT'));

async function main() {
    const args = parseArgs();
    const url = args.url;
    const snapshotId = args.snapshot_id;
    const crawlId = args.crawl_id || getEnv('CRAWL_ID', '');

    if (!url || !snapshotId) {
        console.error('Usage: on_Snapshot__10_chrome_tab.bg.js --url=<url> --snapshot-id=<uuid> [--crawl-id=<uuid>]');
        process.exit(1);
    }

    let status = 'failed';
    let output = '';
    let error = '';
    let version = '';

    try {
        // Get Chrome version
        try {
            const binary = getEnv('CHROME_BINARY', '').trim();
            if (binary) {
                version = execSync(`"${binary}" --version`, { encoding: 'utf8', timeout: 5000 }).trim().slice(0, 64);
            }
        } catch (e) {
            version = '';
        }

        // Try to use existing crawl Chrome session (wait for readiness)
        const timeoutSeconds = getEnvInt('CHROME_TAB_TIMEOUT', getEnvInt('CHROME_TIMEOUT', getEnvInt('TIMEOUT', 60)));
        const crawlSession = await waitForCrawlChromeSession(timeoutSeconds * 1000, {
            crawlBaseDir: getEnv('CRAWL_DIR', '.'),
        });
        console.log(`[*] Found existing Chrome session from crawl ${crawlId}`);

        const { targetId } = await openTabInChromeSession({
            cdpUrl: crawlSession.cdpUrl,
            puppeteer,
        });

        fs.writeFileSync(path.join(OUTPUT_DIR, 'cdp_url.txt'), crawlSession.cdpUrl);
        fs.writeFileSync(path.join(OUTPUT_DIR, 'chrome.pid'), String(crawlSession.pid));
        fs.writeFileSync(path.join(OUTPUT_DIR, 'target_id.txt'), targetId);
        fs.writeFileSync(path.join(OUTPUT_DIR, 'url.txt'), url);

        status = 'succeeded';
        output = OUTPUT_DIR;
        console.log(`[+] Chrome tab ready`);
        console.log(`[+] CDP URL: ${crawlSession.cdpUrl}`);
        console.log(`[+] Page target ID: ${targetId}`);
    } catch (e) {
        error = `${e.name}: ${e.message}`;
        status = 'failed';
    }

    if (error) {
        console.error(`ERROR: ${error}`);
    }

    finalStatus = status;
    finalOutput = output || '';
    finalError = error || '';
    cmdVersion = version || '';

    if (status !== 'succeeded') {
        emitResult(status);
        process.exit(1);
    }

    console.log('[*] Chrome tab created, waiting for cleanup signal...');
    await new Promise(() => {}); // Keep alive until SIGTERM
}

main().catch(e => {
    console.error(`Fatal error: ${e.message}`);
    process.exit(1);
});
