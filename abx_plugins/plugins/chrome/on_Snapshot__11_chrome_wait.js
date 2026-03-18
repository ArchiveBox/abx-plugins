#!/usr/bin/env node
/**
 * Wait for Chrome session files to exist (cdp_url.txt + target_id.txt).
 *
 * This is a foreground hook that blocks until the Chrome tab is ready,
 * so downstream hooks can safely connect to CDP.
 *
 * Usage: on_Snapshot__11_chrome_wait.js --url=<url> --snapshot-id=<uuid>
 */

const fs = require('fs');
const path = require('path');
const { ensureNodeModuleResolution, parseArgs, getEnvInt } = require('../base/utils.js');
ensureNodeModuleResolution(module);
const puppeteer = require('puppeteer');

const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((process.env.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

const {
    connectToPage,
} = require('./chrome_utils.js');

const CHROME_SESSION_DIR = path.join(SNAP_DIR, 'chrome');
const CHROME_SESSION_REQUIRED_ERROR = 'No Chrome session found (chrome plugin must run first)';

async function main() {
    const args = parseArgs();
    const url = args.url;
    const snapshotId = args.snapshot_id;

    if (!url || !snapshotId) {
        console.error('Usage: on_Snapshot__11_chrome_wait.js --url=<url> --snapshot-id=<uuid>');
        process.exit(1);
    }

    const timeoutSeconds = getEnvInt('CHROME_TAB_TIMEOUT', getEnvInt('CHROME_TIMEOUT', getEnvInt('TIMEOUT', 60)));
    const timeoutMs = timeoutSeconds * 1000;

    console.error(`[chrome_wait] Waiting for Chrome session (timeout=${timeoutSeconds}s)...`);

    let readySession = null;
    try {
        readySession = await connectToPage({
            chromeSessionDir: CHROME_SESSION_DIR,
            timeoutMs,
            requireTargetId: true,
            puppeteer,
        });
    } catch (error) {
        const message = error?.message || CHROME_SESSION_REQUIRED_ERROR;
        console.error(`[chrome_wait] ERROR: ${message}`);
        console.log(JSON.stringify({ type: 'ArchiveResult', status: 'failed', output_str: message }));
        process.exit(1);
    }

    const cdpUrl = readySession.cdpUrl;
    const targetId = readySession.targetId;
    if (!cdpUrl || !targetId) {
        const error = CHROME_SESSION_REQUIRED_ERROR;
        console.error(`[chrome_wait] ERROR: ${error}`);
        console.log(JSON.stringify({ type: 'ArchiveResult', status: 'failed', output_str: error }));
        process.exit(1);
    }

    try {
        readySession.browser.disconnect();
    } catch (disconnectError) {}

    console.error(`[chrome_wait] Chrome session ready (verified CDP connection, cdp_url=${cdpUrl.slice(0, 32)}..., target_id=${targetId}).`);
    const port = (cdpUrl.match(/:(\d+)\/devtools\//) || [])[1] || '?';
    console.log(JSON.stringify({ type: 'ArchiveResult', status: 'succeeded', output_str: `tab ready target=${targetId} port=${port}` }));
    process.exit(0);
}

main().catch(e => {
    console.error(`Fatal error: ${e.message}`);
    process.exit(1);
});
