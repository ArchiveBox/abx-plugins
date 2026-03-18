#!/usr/bin/env node
/**
 * Launch or adopt a snapshot-scoped Chrome session when CHROME_ISOLATION=snapshot.
 *
 * In crawl isolation this hook is a no-op readiness check. In snapshot isolation
 * it owns the browser lifecycle for this snapshot and publishes snapshot-scoped
 * session markers before the tab hook runs.
 */

const fs = require('fs');
const path = require('path');
const { ensureNodeModuleResolution, parseArgs } = require('../base/utils.js');
ensureNodeModuleResolution(module);
const puppeteer = require('puppeteer');
const {
    getEnv,
    getEnvBool,
    getEnvInt,
    acquireSessionLock,
    waitForCrawlChromeSession,
    ensureChromeSession,
    closeBrowserInChromeSession,
} = require('./chrome_utils.js');

const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((process.env.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, 'chrome');
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

let chromePid = null;
let chromeCdpUrl = null;
let chromeProcessIsLocal = getEnv('CHROME_CDP_URL', '') ? false : getEnvBool('CHROME_IS_LOCAL', true);
let shouldCloseOnCleanup = false;

async function cleanup() {
    if (shouldCloseOnCleanup) {
        await closeBrowserInChromeSession({
            cdpUrl: chromeCdpUrl,
            pid: chromePid,
            outputDir: OUTPUT_DIR,
            puppeteer,
            processIsLocal: chromeProcessIsLocal,
        });
    }
    process.exit(0);
}

process.on('SIGTERM', cleanup);
process.on('SIGINT', cleanup);

async function main() {
    const args = parseArgs();
    const snapshotId = args.snapshot_id;
    let releaseLock = null;

    if (!snapshotId) {
        console.error('Usage: on_Snapshot__09_chrome_launch.daemon.bg.js --snapshot-id=<uuid> [--url=<url>]');
        process.exit(1);
    }

    try {
        releaseLock = await acquireSessionLock(path.join(OUTPUT_DIR, '.launch.lock'));
        const isolation = getEnv('CHROME_ISOLATION', 'crawl').toLowerCase() === 'snapshot' ? 'snapshot' : 'crawl';
        const keepAlive = getEnvBool('CHROME_KEEPALIVE', false);
        const cdpUrlOverride = getEnv('CHROME_CDP_URL', '');
        chromeProcessIsLocal = cdpUrlOverride ? false : getEnvBool('CHROME_IS_LOCAL', true);

        if (isolation === 'crawl') {
            await waitForCrawlChromeSession(getEnvInt('CHROME_TIMEOUT', 60) * 1000, {
                crawlBaseDir: getEnv('CRAWL_DIR', '.'),
                processIsLocal: chromeProcessIsLocal,
            });
            releaseLock();
            releaseLock = null;
            console.log(JSON.stringify({
                type: 'ArchiveResult',
                status: 'succeeded',
                output_str: 'crawl isolation active',
            }));
            process.exit(0);
        }

        const session = await ensureChromeSession({
            outputDir: OUTPUT_DIR,
            puppeteer,
            processIsLocal: chromeProcessIsLocal,
            cdpUrl: cdpUrlOverride,
            timeoutMs: getEnvInt('CHROME_TIMEOUT', 60) * 1000,
        });

        chromePid = session.pid;
        chromeCdpUrl = session.cdpUrl;
        shouldCloseOnCleanup = !keepAlive;

        console.log(JSON.stringify({
            type: 'ArchiveResult',
            status: 'succeeded',
            output_str: `pid=${chromePid || 'external'} port=${session.port || '?'}`,
        }));
        releaseLock();
        releaseLock = null;

        if (!shouldCloseOnCleanup) {
            process.exit(0);
        }

        setInterval(() => {}, 1000000);
    } catch (error) {
        if (chromeCdpUrl || chromePid) {
            try {
                await closeBrowserInChromeSession({
                    cdpUrl: chromeCdpUrl,
                    pid: chromePid,
                    outputDir: OUTPUT_DIR,
                    puppeteer,
                    processIsLocal: chromeProcessIsLocal,
                });
            } catch (cleanupError) {}
        }
        if (releaseLock) {
            releaseLock();
        }
        console.error(`ERROR: ${error.name}: ${error.message}`);
        process.exit(1);
    }
}

main().catch((error) => {
    console.error(`Fatal error: ${error.message}`);
    process.exit(1);
});
