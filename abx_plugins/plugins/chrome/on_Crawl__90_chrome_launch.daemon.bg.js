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
 * Usage: on_Crawl__90_chrome_launch.daemon.bg.js
 * Output: Writes to current directory (executor creates chrome/ dir):
 *   - cdp_url.txt: WebSocket/HTTP URL for CDP connection
 *   - chrome.pid: Chromium process ID (for cleanup)
 *   - extensions.json: Loaded extensions metadata
 *
 * Environment variables:
 *     NODE_MODULES_DIR: Path to node_modules directory for module resolution
 *     CHROME_BINARY: Path to Chromium binary (falls back to auto-detection)
 *     CHROME_RESOLUTION: Page resolution (default: 1440,2000)
 *     CHROME_HEADLESS: Run in headless mode (default: true)
 *     CHROME_CHECK_SSL_VALIDITY: Whether to check SSL certificates (default: true)
 *     CHROME_EXTENSIONS_DIR: Directory containing Chrome extensions
 */

const fs = require('fs');
const path = require('path');
const { ensureNodeModuleResolution, getEnv, getEnvBool, getEnvInt, emitArchiveResultRecord } = require('../base/utils.js');
ensureNodeModuleResolution(module);
const {
    acquireSessionLock,
    ensureChromeSession,
    closeBrowserInChromeSession,
    waitForChromeLaunchPrerequisites,
} = require('./chrome_utils.js');

// Extractor metadata
const PLUGIN_NAME = 'chrome_launch';
const PLUGIN_DIR = path.basename(__dirname);
const CRAWL_DIR = path.resolve((process.env.CRAWL_DIR || '.').trim());
const OUTPUT_DIR = path.join(CRAWL_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

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
        console.error('[*] Cleaning up Chrome session...');
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

// Register signal handlers
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
        const prerequisiteTimeoutMs = Math.max(
            getEnvInt('CHROME_TIMEOUT', 60) * 1000,
            getEnvInt('CHROME_INSTALL_TIMEOUT', 300) * 1000
        );

        if (isolation === 'snapshot') {
            console.error('[*] CHROME_ISOLATION=snapshot, skipping crawl-scoped browser launch');
            releaseLock();
            releaseLock = null;
            emitArchiveResultRecord('succeeded', 'snapshot isolation active');
            process.exit(0);
        }

        const prerequisites = await waitForChromeLaunchPrerequisites({
            requireLocalBinary: !cdpUrlOverride && chromeProcessIsLocal,
            timeoutMs: prerequisiteTimeoutMs,
        });
        puppeteer = prerequisites.puppeteer;

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

        if (session.binary) {
            let version = '';
            try {
                const { execSync } = require('child_process');
                version = execSync(`"${session.binary}" --version`, { encoding: 'utf8', timeout: 5000 })
                    .trim()
                    .slice(0, 64);
            } catch (e) {}
            console.error(`[*] Using browser: ${session.binary}`);
            if (version) console.error(`[*] Version: ${version}`);
        } else if (cdpUrlOverride) {
            console.error(`[*] Adopting browser from CHROME_CDP_URL`);
        }

        for (const extension of session.installedExtensions) {
            console.error(`[*] Loading extension: ${extension.name || extension.id || extension.unpacked_path}`);
        }
        if (session.installedExtensions.length > 0) {
            console.error(`[+] Found ${session.installedExtensions.length} extension(s) to load`);
        }
        if (session.reusedExisting) {
            console.error(`[*] Reusing live Chromium session in ${OUTPUT_DIR}`);
        }

        console.error('[+] Chromium session started');
        console.error(`[+] CDP URL: ${chromeCdpUrl}`);
        console.error(`[+] PID: ${chromePid || 'external'}`);
        emitArchiveResultRecord('succeeded', `pid=${chromePid || 'external'} port=${getPortFromCdpUrl(chromeCdpUrl) || '?'}`);
        releaseLock();
        releaseLock = null;

        if (!shouldCloseOnCleanup) {
            process.exit(0);
        }

        console.log('[*] Chromium launch hook staying alive to handle cleanup...');
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
