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
const { ensureNodeModuleResolution, loadConfig, emitArchiveResultRecord } = require('../base/utils.js');
ensureNodeModuleResolution(module);
const {
    getEnv,
    getEnvBool,
    getEnvInt,
    acquireSessionLock,
    waitForChromeSessionState,
    ensureChromeSession,
    closeBrowserInChromeSession,
    resolvePuppeteerModule,
} = require('./chrome_utils.js');
const puppeteer = resolvePuppeteerModule();

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || '.').trim());
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
    let releaseLock = null;

    try {
        releaseLock = await acquireSessionLock(path.join(OUTPUT_DIR, '.launch.lock'));
        const isolation = getEnv('CHROME_ISOLATION', 'crawl').toLowerCase() === 'snapshot' ? 'snapshot' : 'crawl';
        const keepAlive = getEnvBool('CHROME_KEEPALIVE', false);
        const cdpUrlOverride = getEnv('CHROME_CDP_URL', '');
        chromeProcessIsLocal = cdpUrlOverride ? false : getEnvBool('CHROME_IS_LOCAL', true);

        if (isolation === 'crawl') {
            const crawlChromeDir = path.join(path.resolve(getEnv('CRAWL_DIR', '.')), 'chrome');
            const crawlSession = await waitForChromeSessionState(crawlChromeDir, {
                timeoutMs: getEnvInt('CHROME_TIMEOUT', 60) * 1000,
            });
            if (!crawlSession?.cdpUrl) {
                throw new Error('No Chrome session found (chrome plugin must run first)');
            }
            releaseLock();
            releaseLock = null;
            emitArchiveResultRecord('skipped', 'CHROME_ISOLATION=crawl');
            process.exit(0);
        }

        // console.log('launching local chrome browser...');
        console.log('chrome is launching...');
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

        emitArchiveResultRecord('succeeded', `pid=${chromePid || 'external'} port=${session.port || '?'}`);
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
