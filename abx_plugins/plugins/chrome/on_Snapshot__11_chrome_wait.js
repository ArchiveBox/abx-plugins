#!/usr/bin/env node
/**
 * Wait for Chrome session files to exist (cdp_url.txt + target_id.txt).
 *
 * This is a foreground hook that blocks until the Chrome tab is ready,
 * so downstream hooks can safely connect to CDP.
 *
 * This hook exists primarily as a foreground barrier. The snapshot launch/tab
 * hooks are daemons/background hooks, so they do not block later foreground
 * hooks by themselves. Keeping this as a thin foreground wait stage means
 * downstream snapshot hooks do not all need to reimplement their own manual
 * ordering and blocking checks before connecting to the published page target.
 *
 * Usage: on_Snapshot__11_chrome_wait.js --url=<url>
 */

const fs = require('fs');
const path = require('path');
const { ensureNodeModuleResolution, parseArgs, getEnvInt, loadConfig, emitArchiveResultRecord } = require('../base/utils.js');
ensureNodeModuleResolution(module);

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

const {
    resolvePuppeteerModule,
    waitForChromeSessionState,
} = require('./chrome_utils.js');
const puppeteer = resolvePuppeteerModule();

const CHROME_SESSION_DIR = path.join(SNAP_DIR, 'chrome');
const CHROME_SESSION_REQUIRED_ERROR = 'No Chrome session found (chrome plugin must run first)';

async function main() {
    const args = parseArgs();
    const url = args.url;

    if (!url) {
        console.error('Usage: on_Snapshot__11_chrome_wait.js --url=<url>');
        process.exit(1);
    }

    const timeoutSeconds = getEnvInt('CHROME_TAB_TIMEOUT', getEnvInt('CHROME_TIMEOUT', getEnvInt('TIMEOUT', 60)));
    const timeoutMs = timeoutSeconds * 1000;

    console.log('waiting for chrome to launch...');
    console.error(`[chrome_wait] Waiting for Chrome session (timeout=${timeoutSeconds}s)...`);

    const readySession = await waitForChromeSessionState(CHROME_SESSION_DIR, {
        timeoutMs,
        intervalMs: 100,
        requireTargetId: true,
        requireConnectable: true,
        probeTimeoutMs: 1000,
        puppeteer,
    });
    console.log('verifying chrome is ready...');
    if (!readySession?.cdpUrl || !readySession?.targetId) {
        const error = CHROME_SESSION_REQUIRED_ERROR;
        console.error(`[chrome_wait] ERROR: ${error}`);
        emitArchiveResultRecord('failed', error);
        process.exit(1);
    }

    const cdpUrl = readySession.cdpUrl;
    const targetId = readySession.targetId;
    if (!cdpUrl || !targetId) {
        const error = CHROME_SESSION_REQUIRED_ERROR;
        console.error(`[chrome_wait] ERROR: ${error}`);
        emitArchiveResultRecord('failed', error);
        process.exit(1);
    }

    console.error(`[chrome_wait] Chrome session ready (verified CDP connection, cdp_url=${cdpUrl.slice(0, 32)}..., target_id=${targetId}).`);
    const port = (cdpUrl.match(/:(\d+)\/devtools\//) || [])[1] || '?';
    emitArchiveResultRecord('succeeded', `tab ready target=${targetId} port=${port}`);
    process.exit(0);
}

main().catch(e => {
    console.error(`Fatal error: ${e.message}`);
    process.exit(1);
});
