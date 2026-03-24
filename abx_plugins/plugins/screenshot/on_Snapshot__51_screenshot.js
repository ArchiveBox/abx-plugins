#!/usr/bin/env node
/**
 * Take a screenshot of a URL using an existing Chrome session.
 *
 * Requires chrome plugin to have already created a Chrome session.
 * Connects to the existing session via CDP and takes a screenshot.
 *
 * Usage: on_Snapshot__51_screenshot.js --url=<url>
 * Output: Writes screenshot/screenshot.png
 *
 * Environment variables:
 *     SCREENSHOT_ENABLED: Enable screenshot capture (default: true)
 */

const fs = require('fs');
const path = require('path');
const {
    ensureNodeModuleResolution,
    getEnv,
    getEnvBool,
    loadConfig,
    parseArgs,
    emitArchiveResultRecord,
    hasStaticFileOutput,
} = require('../base/utils.js');
ensureNodeModuleResolution(module);
const { connectToPage, resolvePuppeteerModule } = require('../chrome/chrome_utils.js');
const hookConfig = loadConfig();

// Flush V8 coverage before exiting (for NODE_V8_COVERAGE support)
function flushCoverageAndExit(exitCode) {
    if (hookConfig.NODE_V8_COVERAGE) {
        try {
            const v8 = require('v8');
            v8.takeCoverage();
        } catch (e) {
            // Ignore errors during coverage flush
        }
    }
    process.exit(exitCode);
}

function tempPathFor(filePath) {
    const dir = path.dirname(filePath);
    const base = path.basename(filePath);
    return path.join(dir, `.${base}.${process.pid}.tmp`);
}

// Check if screenshot is enabled BEFORE requiring puppeteer
if (!getEnvBool('SCREENSHOT_ENABLED', true)) {
    console.error('Skipping screenshot (SCREENSHOT_ENABLED=False)');
    emitArchiveResultRecord('skipped', 'SCREENSHOT_ENABLED=False');
    flushCoverageAndExit(0);
}

// Now safe to require puppeteer
const puppeteer = resolvePuppeteerModule();

// Extractor metadata
const PLUGIN_NAME = 'screenshot';
const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const OUTPUT_FILE = 'screenshot.png';
const CHROME_SESSION_DIR = '../chrome';

async function takeScreenshot(url) {
    // Output directory is current directory (hook already runs in output dir)
    const outputPath = path.join(OUTPUT_DIR, OUTPUT_FILE);
    const tempOutputPath = tempPathFor(outputPath);

    // Wait for chrome_navigate to complete (writes navigation.json)
    // Keep runtime default aligned with config.json (default: 60s).
    const timeoutSeconds = parseInt(getEnv('SCREENSHOT_TIMEOUT', '60'), 10);
    const timeoutMs = timeoutSeconds * 1000;
    const { browser, page } = await connectToPage({
        chromeSessionDir: CHROME_SESSION_DIR,
        timeoutMs,
        waitForNavigationComplete: true,
        puppeteer,
    });

    try {
        const captureTimeoutMs = Math.max(timeoutMs, 10000);
        const timeoutPromise = new Promise((_, reject) => {
            setTimeout(() => reject(new Error('Screenshot capture timed out')), captureTimeoutMs);
        });

        await page.bringToFront();
        try {
            await Promise.race([
                page.screenshot({ path: tempOutputPath, fullPage: true }),
                timeoutPromise,
            ]);
        } catch (err) {
            if (!(err instanceof Error) || !err.message.includes('timed out')) {
                throw err;
            }
            // Some Chromium builds hang on full-page capture against local fixture pages.
            // Fall back to viewport capture before failing the hook.
            await page.screenshot({ path: tempOutputPath, fullPage: false });
        }

        fs.renameSync(tempOutputPath, outputPath);

    return OUTPUT_FILE;

    } finally {
        // Disconnect from browser (don't close it - we're connected to a shared session)
        // The chrome_launch hook manages the browser lifecycle
        await browser.disconnect();
    }
}

async function main() {
    const args = parseArgs();
    const url = args.url;

    if (!url) {
        console.error('Usage: on_Snapshot__51_screenshot.js --url=<url>');
        emitArchiveResultRecord('failed', 'missing required args');
        flushCoverageAndExit(1);
    }

    // Check if staticfile extractor already handled this (permanent skip)
    if (hasStaticFileOutput()) {
        console.error(`Skipping screenshot - staticfile extractor already downloaded this`);
        emitArchiveResultRecord('noresults', 'staticfile already handled');
        flushCoverageAndExit(0);
    }

    // Take screenshot (throws on error)
    const outputPath = await takeScreenshot(url);

    // Success - emit ArchiveResult
    const size = fs.statSync(path.join(OUTPUT_DIR, outputPath)).size;
    console.error(`Screenshot saved (${size} bytes)`);
    emitArchiveResultRecord('succeeded', `${PLUGIN_DIR}/${outputPath}`);
    flushCoverageAndExit(0);
}

main().catch(e => {
    console.error(`ERROR: ${e.message}`);
    emitArchiveResultRecord('failed', e.message || 'unknown error');
    flushCoverageAndExit(1);
});
