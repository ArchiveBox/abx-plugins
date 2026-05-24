#!/usr/bin/env node
/**
 * Wait for the crawl-level Chrome browser session to become CDP-connectable.
 *
 * This is a foreground crawl hook that blocks later crawl hooks until the
 * shared browser launched by on_CrawlSetup__90_chrome_launch.daemon.bg.js is actually
 * reachable over CDP.
 *
 * This hook exists primarily as a foreground barrier. The launch hook is a
 * daemon/background hook, so it does not block later foreground hooks by
 * itself. Keeping this as a thin foreground wait stage means downstream crawl
 * hooks do not all need to reimplement their own "wait until Chrome is ready"
 * ordering logic before touching the shared browser session.
 *
 * Usage: on_CrawlSetup__91_chrome_wait.js --url=<url>
 */

const fs = require('fs');
const path = require('path');
const { ensureNodeModuleResolution, parseArgs, getEnv, getEnvInt, loadConfig } = require('../base/utils.js');
ensureNodeModuleResolution(module);

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const CRAWL_DIR = path.resolve((hookConfig.CRAWL_DIR || '.').trim());
const OUTPUT_DIR = path.join(CRAWL_DIR, PLUGIN_DIR);
const CHROME_BINARY = (process.env.CHROME_BINARY || 'chromium').split('/').at(-1);
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

    if (!url) {
        console.error('Usage: on_CrawlSetup__91_chrome_wait.js --url=<url>');
        process.exit(1);
    }

    const timeoutSeconds = getEnvInt('CHROME_TAB_TIMEOUT', getEnvInt('CHROME_TIMEOUT', getEnvInt('TIMEOUT', 60)));
    const timeoutMs = timeoutSeconds * 1000;
    const isolation = getEnv('CHROME_ISOLATION', 'crawl').toLowerCase() === 'snapshot' ? 'snapshot' : 'crawl';

    if (isolation === 'snapshot') {
        console.error('CHROME_ISOLATION=snapshot, skipping crawl-scoped wait');
        process.exit(10);
    }

    console.log(`waiting for ${CHROME_BINARY} to launch...`);
    // console.error(`Waiting Chrome session...`);

    const readySession = await waitForChromeSessionState(CHROME_SESSION_DIR, {
        timeoutMs,
        intervalMs: 100,
        requireConnectable: true,
        probeTimeoutMs: 1000,
    });
    console.log(`verifying ${CHROME_BINARY} is ready...`);
    if (!readySession?.cdpUrl) {
        const error = CHROME_SESSION_REQUIRED_ERROR;
        console.error(`ERROR: ${error}`);
        process.exit(1);
    }

    const pid = readySession.pid || 'external';
    let port = '?';
    try {
        const endpoint = new URL(readySession.cdpUrl);
        port = endpoint.port || (endpoint.protocol === 'https:' || endpoint.protocol === 'wss:' ? '443' : '80');
    } catch (error) {}

    console.log(`${CHROME_BINARY} ready pid=${pid} cdp=${readySession.cdpUrl.split('/devtools/')[0]}`);
    process.exit(0);
}

main().catch(error => {
    console.error(`Fatal error: ${error.message}`);
    process.exit(1);
});
