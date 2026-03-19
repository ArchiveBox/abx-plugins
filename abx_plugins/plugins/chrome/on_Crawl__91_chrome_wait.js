#!/usr/bin/env node
/**
 * Wait for the crawl-level Chrome browser session to become CDP-connectable.
 *
 * This is a foreground crawl hook that blocks later crawl hooks until the
 * shared browser launched by on_Crawl__90_chrome_launch.daemon.bg.js is actually
 * reachable over CDP.
 *
 * This hook exists primarily as a foreground barrier. The launch hook is a
 * daemon/background hook, so it does not block later foreground hooks by
 * itself. Keeping this as a thin foreground wait stage means downstream crawl
 * hooks do not all need to reimplement their own "wait until Chrome is ready"
 * ordering logic before touching the shared browser session.
 *
 * Usage: on_Crawl__91_chrome_wait.js --url=<url> --snapshot-id=<uuid>
 */

const fs = require('fs');
const path = require('path');
const { ensureNodeModuleResolution, parseArgs, getEnv, getEnvInt } = require('../base/utils.js');
ensureNodeModuleResolution(module);

const PLUGIN_DIR = path.basename(__dirname);
const CRAWL_DIR = path.resolve((process.env.CRAWL_DIR || '.').trim());
const OUTPUT_DIR = path.join(CRAWL_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

const {
    waitForChromeSessionState,
} = require('./chrome_utils.js');

const CHROME_SESSION_DIR = path.join(CRAWL_DIR, 'chrome');
const CHROME_SESSION_REQUIRED_ERROR = 'No Chrome session found (chrome plugin must run first)';

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
    const isolation = getEnv('CHROME_ISOLATION', 'crawl').toLowerCase() === 'snapshot' ? 'snapshot' : 'crawl';

    if (isolation === 'snapshot') {
        console.error('[chrome_wait:crawl] CHROME_ISOLATION=snapshot, skipping crawl-scoped wait');
        console.log(JSON.stringify({ type: 'ArchiveResult', status: 'succeeded', output_str: 'snapshot isolation active' }));
        process.exit(0);
    }

    console.error(`[chrome_wait:crawl] Waiting for crawl Chrome session (timeout=${timeoutSeconds}s)...`);

    const readySession = await waitForChromeSessionState(CHROME_SESSION_DIR, {
        timeoutMs,
        intervalMs: 100,
        requireConnectable: true,
        probeTimeoutMs: 1000,
    });
    if (!readySession?.cdpUrl) {
        const error = CHROME_SESSION_REQUIRED_ERROR;
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
