#!/usr/bin/env node
/**
 * Take a screenshot of a URL using an existing Chrome session.
 *
 * Requires chrome plugin to have already created a Chrome session.
 * Connects to the existing session via CDP and takes a screenshot.
 *
 * Usage: on_Snapshot__51_screenshot.js --url=<url> --snapshot-id=<uuid>
 * Output: Writes screenshot/screenshot.png
 *
 * Environment variables:
 *     SCREENSHOT_ENABLED: Enable screenshot capture (default: true)
 */

const fs = require('fs');
const path = require('path');
// Add NODE_MODULES_DIR to module resolution paths if set
if (process.env.NODE_MODULES_DIR) module.paths.unshift(process.env.NODE_MODULES_DIR);

// Flush V8 coverage before exiting (for NODE_V8_COVERAGE support)
function flushCoverageAndExit(exitCode) {
    if (process.env.NODE_V8_COVERAGE) {
        try {
            const v8 = require('v8');
            v8.takeCoverage();
        } catch (e) {
            // Ignore errors during coverage flush
        }
    }
    process.exit(exitCode);
}

function emitArchiveResult(status, outputStr) {
    console.log(JSON.stringify({
        type: 'ArchiveResult',
        status,
        output_str: outputStr,
    }));
}

function tempPathFor(filePath) {
    const dir = path.dirname(filePath);
    const base = path.basename(filePath);
    return path.join(dir, `.${base}.${process.pid}.tmp`);
}

const {
    getEnv,
    getEnvBool,
    parseArgs,
    connectToPage,
    getTargetIdFromPage,
    waitForPageLoaded,
    readTargetId,
} = require('../chrome/chrome_utils.js');

// Check if screenshot is enabled BEFORE requiring puppeteer
if (!getEnvBool('SCREENSHOT_ENABLED', true)) {
    console.error('Skipping screenshot (SCREENSHOT_ENABLED=False)');
    emitArchiveResult('skipped', 'SCREENSHOT_ENABLED=False');
    flushCoverageAndExit(0);
}

// Now safe to require puppeteer
const puppeteer = require('puppeteer-core');

// Extractor metadata
const PLUGIN_NAME = 'screenshot';
const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((process.env.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const OUTPUT_FILE = 'screenshot.png';
const CHROME_SESSION_DIR = '../chrome';

// Check if staticfile extractor already downloaded this URL
const STATICFILE_DIR = '../staticfile';
function hasStaticFileOutput() {
    if (!fs.existsSync(STATICFILE_DIR)) return false;
    const stdoutPath = path.join(STATICFILE_DIR, 'stdout.log');
    if (!fs.existsSync(stdoutPath)) return false;
    const stdout = fs.readFileSync(stdoutPath, 'utf8');
    for (const line of stdout.split('\n')) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('{')) continue;
        try {
            const record = JSON.parse(trimmed);
            if (record.type === 'ArchiveResult' && record.status === 'succeeded') {
                return true;
            }
        } catch (e) {}
    }
    return false;
}

async function takeScreenshot(url) {
    // Output directory is current directory (hook already runs in output dir)
    const outputPath = path.join(OUTPUT_DIR, OUTPUT_FILE);
    const tempOutputPath = tempPathFor(outputPath);

    const cdpFile = path.join(CHROME_SESSION_DIR, 'cdp_url.txt');
    const targetFile = path.join(CHROME_SESSION_DIR, 'target_id.txt');
    if (!fs.existsSync(cdpFile)) {
        throw new Error('No Chrome session found (chrome plugin must run first)');
    }
    if (!fs.existsSync(targetFile)) {
        throw new Error('No target_id.txt found (chrome_tab must run first)');
    }

    // Wait for chrome_navigate to complete (writes navigation.json)
    // Keep runtime default aligned with config.json (default: 60s).
    const timeoutSeconds = parseInt(getEnv('SCREENSHOT_TIMEOUT', '60'), 10);
    const timeoutMs = timeoutSeconds * 1000;
    const navigationFile = path.join(CHROME_SESSION_DIR, 'navigation.json');
    if (!fs.existsSync(navigationFile)) {
        await waitForPageLoaded(CHROME_SESSION_DIR, timeoutMs);
    }
    const cdpUrl = fs.readFileSync(cdpFile, 'utf8').trim();
    if (!cdpUrl.startsWith('ws://') && !cdpUrl.startsWith('wss://')) {
        throw new Error('Invalid CDP URL in cdp_url.txt');
    }

    const { browser, page } = await connectToPage({
        chromeSessionDir: CHROME_SESSION_DIR,
        timeoutMs,
        puppeteer,
    });

    try {
        const expectedTargetId = readTargetId(CHROME_SESSION_DIR);
        if (!expectedTargetId) {
            throw new Error('No target_id.txt found (chrome_tab must run first)');
        }
        const actualTargetId = getTargetIdFromPage(page);
        if (actualTargetId !== expectedTargetId) {
            throw new Error(`Target ${expectedTargetId} not found in Chrome session`);
        }

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
    const snapshotId = args.snapshot_id;

    if (!url || !snapshotId) {
        console.error('Usage: on_Snapshot__51_screenshot.js --url=<url> --snapshot-id=<uuid>');
        emitArchiveResult('failed', 'missing required args');
        flushCoverageAndExit(1);
    }

    // Check if staticfile extractor already handled this (permanent skip)
    if (hasStaticFileOutput()) {
        console.error(`Skipping screenshot - staticfile extractor already downloaded this`);
        emitArchiveResult('noresults', 'staticfile already handled');
        flushCoverageAndExit(0);
    }

    // Take screenshot (throws on error)
    const outputPath = await takeScreenshot(url);

    // Success - emit ArchiveResult
    const size = fs.statSync(path.join(OUTPUT_DIR, outputPath)).size;
    console.error(`Screenshot saved (${size} bytes)`);
    emitArchiveResult('succeeded', outputPath);
    flushCoverageAndExit(0);
}

main().catch(e => {
    console.error(`ERROR: ${e.message}`);
    emitArchiveResult('failed', e.message || 'unknown error');
    flushCoverageAndExit(1);
});
