#!/usr/bin/env node
/**
 * Save a browser-generated MHTML snapshot using Chrome/Puppeteer.
 *
 * Requires a Chrome session (from chrome plugin) and connects to it via CDP.
 *
 * Usage: on_Snapshot__54_chrome_mhtml.js --url=<url>
 * Output: Writes chrome_mhtml/snapshot.mhtml
 *
 * Environment variables:
 *     CHROME_MHTML_ENABLED: Enable MHTML extraction (default: true)
 */

const fs = require('fs');
const path = require('path');
const {
    ensureNodeModuleResolution,
    getEnvBool,
    getEnvInt,
    loadConfig,
    parseArgs,
    emitArchiveResultRecord,
    writeFileAtomic,
} = require('../base/utils.js');
ensureNodeModuleResolution(module);
const { connectToPage, resolvePuppeteerModule } = require('../chrome/chrome_utils.js');
const hookConfig = loadConfig();

if (!getEnvBool('CHROME_MHTML_ENABLED', true)) {
    console.error('Skipping Chrome MHTML (CHROME_MHTML_ENABLED=False)');
    emitArchiveResultRecord('skipped', 'CHROME_MHTML_ENABLED=False');
    process.exit(0);
}

const puppeteer = resolvePuppeteerModule();

const PLUGIN_NAME = 'chrome_mhtml';
const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const OUTPUT_FILE = 'snapshot.mhtml';
const CHROME_SESSION_DIR = '../chrome';

async function waitForFrameTreeSettled(page, timeoutMs) {
    const settleTimeoutMs = Math.min(Math.max(timeoutMs, 1000), 5000);
    if (typeof page.waitForNetworkIdle === 'function') {
        await page.waitForNetworkIdle({ idleTime: 500, timeout: settleTimeoutMs }).catch(() => null);
    }

    await Promise.all(page.frames().map(frame => (
        frame.waitForFunction(
            () => document.readyState === 'complete',
            { timeout: Math.min(settleTimeoutMs, 1500) }
        ).catch(() => null)
    )));
}

async function captureMhtml(timeoutMs) {
    const outputPath = path.join(OUTPUT_DIR, OUTPUT_FILE);
    let browser = null;

    try {
        const connection = await connectToPage({
            chromeSessionDir: CHROME_SESSION_DIR,
            timeoutMs,
            waitForNavigationComplete: true,
            postLoadDelayMs: 200,
            puppeteer,
        });
        browser = connection.browser;
        const page = connection.page;
        const cdpSession = connection.cdpSession;

        await waitForFrameTreeSettled(page, timeoutMs);
        await cdpSession.send('Page.enable').catch(() => null);

        const snapshot = await cdpSession.send('Page.captureSnapshot', { format: 'mhtml' });
        const mhtmlContent = snapshot && snapshot.data;

        if (mhtmlContent && mhtmlContent.length > 100) {
            writeFileAtomic(outputPath, mhtmlContent);
            return { success: true, output: OUTPUT_FILE };
        }
        return { success: true, noresults: true, output: 'MHTML content too short or empty' };

    } catch (e) {
        return { success: false, error: `${e.name}: ${e.message}` };
    } finally {
        if (browser) {
            browser.disconnect();
        }
    }
}

async function main() {
    const args = parseArgs();
    const url = args.url;

    if (!url) {
        console.error('Usage: on_Snapshot__54_chrome_mhtml.js --url=<url>');
        emitArchiveResultRecord('failed', 'missing required args');
        process.exit(1);
    }

    try {
        const timeoutMs = getEnvInt('CHROME_MHTML_TIMEOUT', getEnvInt('TIMEOUT', 30)) * 1000;
        const result = await captureMhtml(timeoutMs);

        if (result.success) {
            if (result.noresults) {
                emitArchiveResultRecord('noresults', result.output);
                process.exit(0);
            }
            const size = fs.statSync(path.join(OUTPUT_DIR, result.output)).size;
            console.error(`Chrome MHTML saved (${size} bytes)`);
            emitArchiveResultRecord('succeeded', `${PLUGIN_DIR}/${result.output}`);
            process.exit(0);
        }

        console.error(`ERROR: ${result.error}`);
        emitArchiveResultRecord('failed', result.error);
        process.exit(1);
    } catch (e) {
        console.error(`ERROR: ${e.name}: ${e.message}`);
        emitArchiveResultRecord('failed', `${e.name}: ${e.message}`);
        process.exit(1);
    }
}

main().catch(e => {
    console.error(`Fatal error: ${e.message}`);
    emitArchiveResultRecord('failed', `${e.name}: ${e.message}`);
    process.exit(1);
});
