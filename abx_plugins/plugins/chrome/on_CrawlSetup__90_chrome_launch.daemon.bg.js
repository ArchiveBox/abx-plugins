#!/usr/bin/env node
/**
 * Launch a shared Chromium browser session for the entire crawl.
 *
 * This runs once per crawl and keeps Chromium alive for all snapshots to share.
 * Each snapshot creates its own tab via on_Snapshot__10_chrome_tab.daemon.bg.js.
 *
 * NOTE: We use Chromium instead of Chrome because Chrome 137+ removed support for
 * --load-extension and --disable-extensions-except flags.
 *
 * Usage: on_CrawlSetup__90_chrome_launch.daemon.bg.js
 * Output: Writes to current directory (executor creates chrome/ dir):
 *   - cdp_url.txt: WebSocket/HTTP URL for CDP connection
 *   - chrome.pid: Chromium process ID (for cleanup)
 *   - extensions.json: Loaded extensions metadata
 *
 * Environment variables:
 *     NODE_MODULES_DIR: Path to node_modules directory for module resolution
 *     process.env.: Path to Chromium binary (falls back to auto-detection)
 *     CHROME_RESOLUTION: Page resolution (default: 1440,2000)
 *     CHROME_HEADLESS: Run in headless mode (default: true)
 *     CHROME_CHECK_SSL_VALIDITY: Whether to check SSL certificates (default: true)
 *     CHROME_EXTENSIONS_DIR: Directory containing Chrome extensions
 */

const fs = require('fs');
const path = require('path');
const { ensureNodeModuleResolution, getEnv, getEnvBool, getEnvInt, loadConfig } = require('../base/utils.js');
ensureNodeModuleResolution(module);
const {
    acquireSessionLock,
    ensureChromeSession,
    closeBrowserInChromeSession,
    killZombieChrome,
    waitForChromeLaunchPrerequisites,
} = require('./chrome_utils.js');

// Extractor metadata
const PLUGIN_NAME = 'chrome_launch';
const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const CRAWL_DIR = path.resolve((hookConfig.CRAWL_DIR || '.').trim());
const OUTPUT_DIR = path.join(CRAWL_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const CHROME_BINARY = (process.env.CHROME_BINARY || 'chromium').split('/').at(-1);
const PERSONA_DIR = process.env.PERSONA_DIR || ((process.env.PERSONAS_DIR || '~/.config/abx/personas') + '/' + (process.env.ACTIVE_PERSONA || 'Default'))

// Global state for cleanup
let chromePid = null;
let chromeCdpUrl = null;
let chromeProcessIsLocal = getEnv('CHROME_CDP_URL', '') ? false : getEnvBool('CHROME_IS_LOCAL', true);
let shouldCloseOnCleanup = false;
let puppeteer = null;

function getPortFromCdpUrl(cdpUrl) {
    if (!cdpUrl) return null;
    const match = cdpUrl.match(/:(\d+)\/devtools\//);
    return match ? match[1] : null;
}

// Cleanup handler for SIGTERM
async function cleanup() {
    if (shouldCloseOnCleanup) {
        console.log(`shutting down ${CHROME_BINARY} cleanly...`);
        const closed = await closeBrowserInChromeSession({
            cdpUrl: chromeCdpUrl,
            pid: chromePid,
            outputDir: OUTPUT_DIR,
            puppeteer,
            processIsLocal: chromeProcessIsLocal,
        });
        if (!closed) {
            console.error(`${CHROME_BINARY} cleanup did not fully stop the browser process tree`);
            process.exit(1);
        }
        await killZombieChrome(CRAWL_DIR, {
            quiet: true,
            excludeCurrentRuntimeDirs: false,
        });
        console.log(`${CHROME_BINARY} exited successfully`);
        console.log(JSON.stringify({ succeeded: true, skipped: false }));  // we launched and we killed it (nothing was skipped)
    } else {
        console.log(`leaving ${CHROME_BINARY} running (CHROME_KEEPALIVE=True)`);
        console.log(JSON.stringify({ succeeded: true, skipped: chromeCdpUrl ? true : false }));  // we didn't launch it (we connected over CDP), and we didn't kill it, so we skipped basically the whole hook
    }
    process.exit(0);
}

// Register signal handlers
process.on('SIGTERM', cleanup);
process.on('SIGINT', cleanup);

async function main() {
    let releaseLock = null;

    try {
        console.log('waiting for other chrome instances to finish launching...')
        releaseLock = await acquireSessionLock(path.join(OUTPUT_DIR, '.launch.lock'));
        const isolation = getEnv('CHROME_ISOLATION', 'crawl').toLowerCase() === 'snapshot' ? 'snapshot' : 'crawl';
        const keepAlive = getEnvBool('CHROME_KEEPALIVE', false);
        const cdpUrlOverride = getEnv('CHROME_CDP_URL', '');
        chromeProcessIsLocal = cdpUrlOverride ? false : getEnvBool('CHROME_IS_LOCAL', true);
        const prerequisiteTimeoutMs = Math.max(
            getEnvInt('CHROME_TIMEOUT', 60) * 1000,
            getEnvInt('CHROME_INSTALL_TIMEOUT', 300) * 1000
        );

        if (isolation === 'snapshot') {
            console.log('skipping crawl-scoped browser launch (CHROME_ISOLATION=snapshot)');
            releaseLock();
            releaseLock = null;
            process.exit(0);
        }

        console.log(`waiting for ${CHROME_BINARY} to be installed...`)
        const prerequisites = await waitForChromeLaunchPrerequisites({
            requireLocalBinary: !cdpUrlOverride && chromeProcessIsLocal,
            timeoutMs: prerequisiteTimeoutMs,
        });
        puppeteer = prerequisites.puppeteer;

        console.log(cdpUrlOverride
            ? `connecting ${CHROME_BINARY} ${cdpUrlOverride}...`
            : `launching ${CHROME_BINARY} ${PERSONA_DIR}...`)
        const session = await ensureChromeSession({
            outputDir: OUTPUT_DIR,
            puppeteer,
            processIsLocal: chromeProcessIsLocal,
            cdpUrl: cdpUrlOverride,
            timeoutMs: getEnvInt('CHROME_TIMEOUT', 60) * 1000,
            binary: prerequisites.binary || null,
        });

        chromePid = session.pid;
        chromeCdpUrl = session.cdpUrl;
        shouldCloseOnCleanup = !keepAlive;

        for (const extension of session.installedExtensions) {
            console.log(`loading extension: ${extension.name || extension.id || extension.unpacked_path}...`);
        }
        if (session.reusedExisting) {
            console.log(`reusing live ${CHROME_BINARY} session in ${OUTPUT_DIR}`);
        }

        console.error(`[+] ${CHROME_BINARY} session started`);
        console.error(`[+] CDP URL: ${chromeCdpUrl}`);
        releaseLock();
        releaseLock = null;

        if (!shouldCloseOnCleanup) {
            process.exit(0);
        }

        console.log(`${CHROME_BINARY} running pid=${chromePid || 'remote'}, waiting for cleanup...`);
        setInterval(() => {}, 1000000);

    } catch (e) {
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
        console.error(`ERROR: ${e.name}: ${e.message}`);
        process.exit(1);
    }
}

main().catch((e) => {
    console.error(`Fatal error: ${e.message}`);
    process.exit(1);
});
