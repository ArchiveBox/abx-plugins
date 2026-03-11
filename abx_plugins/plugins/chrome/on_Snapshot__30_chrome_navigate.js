#!/usr/bin/env node
/**
 * Navigate the Chrome browser to the target URL.
 *
 * This is a simple hook that ONLY navigates - nothing else.
 * Pre-load hooks (21-29) should set up their own CDP listeners.
 * Post-load hooks (31+) can then read from the loaded page.
 *
 * Usage: on_Snapshot__30_chrome_navigate.js --url=<url> --snapshot-id=<uuid>
 * Output: Writes page_loaded.txt marker when navigation completes
 *
 * Environment variables:
 *     CHROME_PAGELOAD_TIMEOUT: Timeout in seconds (default: 60)
 *     CHROME_DELAY_AFTER_LOAD: Extra delay after load in seconds (default: 0)
 *     CHROME_WAIT_FOR: Wait condition (default: networkidle2)
 */

const fs = require('fs');
const path = require('path');
// Add NODE_MODULES_DIR to module resolution paths if set
if (process.env.NODE_MODULES_DIR) module.paths.unshift(process.env.NODE_MODULES_DIR);
const puppeteer = require('puppeteer');
const {
    connectToPage,
} = require('./chrome_utils.js');

const PLUGIN_NAME = 'chrome_navigate';
const CHROME_SESSION_DIR = '.';
const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((process.env.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

function parseArgs() {
    const args = {};
    process.argv.slice(2).forEach(arg => {
        if (arg.startsWith('--')) {
            const [key, ...valueParts] = arg.slice(2).split('=');
            args[key.replace(/-/g, '_')] = valueParts.join('=') || true;
        }
    });
    return args;
}

function getEnv(name, defaultValue = '') {
    return (process.env[name] || defaultValue).trim();
}

function getEnvInt(name, defaultValue = 0) {
    const val = parseInt(getEnv(name, String(defaultValue)), 10);
    return isNaN(val) ? defaultValue : val;
}

function getEnvFloat(name, defaultValue = 0) {
    const val = parseFloat(getEnv(name, String(defaultValue)));
    return isNaN(val) ? defaultValue : val;
}

function getWaitCondition() {
    const waitFor = getEnv('CHROME_WAIT_FOR', 'networkidle2').toLowerCase();
    const valid = ['domcontentloaded', 'load', 'networkidle0', 'networkidle2'];
    return valid.includes(waitFor) ? waitFor : 'networkidle2';
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function navigate(url) {
    const timeout = (getEnvInt('CHROME_PAGELOAD_TIMEOUT') || getEnvInt('CHROME_TIMEOUT') || getEnvInt('TIMEOUT', 60)) * 1000;
    const delayAfterLoad = getEnvFloat('CHROME_DELAY_AFTER_LOAD', 0) * 1000;
    const waitUntil = getWaitCondition();

    let browser = null;
    const navStartTime = Date.now();

    try {
        const conn = await connectToPage({
            chromeSessionDir: CHROME_SESSION_DIR,
            timeoutMs: timeout,
            requireTargetId: true,
            puppeteer,
        });
        browser = conn.browser;
        const page = conn.page;

        // Navigate
        console.log(`Navigating to ${url} (wait: ${waitUntil}, timeout: ${timeout}ms)`);
        const response = await page.goto(url, { waitUntil, timeout });

        // Optional delay
        if (delayAfterLoad > 0) {
            console.log(`Waiting ${delayAfterLoad}ms after load...`);
            await sleep(delayAfterLoad);
        }

        const finalUrl = page.url();
        const status = response ? response.status() : null;
        const elapsed = Date.now() - navStartTime;

        // Write navigation state as JSON
        const navigationState = {
            waitUntil,
            elapsed,
            url,
            finalUrl,
            status,
            timestamp: new Date().toISOString()
        };
        fs.writeFileSync(path.join(OUTPUT_DIR, 'navigation.json'), JSON.stringify(navigationState, null, 2));

        // Write marker files for backwards compatibility
        fs.writeFileSync(path.join(OUTPUT_DIR, 'page_loaded.txt'), new Date().toISOString());
        fs.writeFileSync(path.join(OUTPUT_DIR, 'final_url.txt'), finalUrl);

        browser.disconnect();

        return { success: true, finalUrl, status, waitUntil, elapsed };

    } catch (e) {
        if (browser) browser.disconnect();
        const elapsed = Date.now() - navStartTime;
        return { success: false, error: `${e.name}: ${e.message}`, waitUntil, elapsed };
    }
}

async function main() {
    const args = parseArgs();
    const url = args.url;
    const snapshotId = args.snapshot_id;

    if (!url || !snapshotId) {
        console.error('Usage: on_Snapshot__30_chrome_navigate.js --url=<url> --snapshot-id=<uuid>');
        process.exit(1);
    }

    const startTs = new Date();
    let status = 'failed';
    let output = null;
    let error = '';

    const result = await navigate(url);

    if (result.success) {
        status = 'succeeded';
        output = result.status ? `page loaded http=${result.status}` : 'page loaded';
        console.log(`Page loaded: ${result.finalUrl} (HTTP ${result.status}) in ${result.elapsed}ms (waitUntil: ${result.waitUntil})`);
    } else {
        error = result.error;
        // Save navigation state even on failure
        const navigationState = {
            waitUntil: result.waitUntil,
            elapsed: result.elapsed,
            url,
            error: result.error,
            timestamp: new Date().toISOString()
        };
        fs.writeFileSync(path.join(OUTPUT_DIR, 'navigation.json'), JSON.stringify(navigationState, null, 2));
    }

    const endTs = new Date();

    if (error) console.error(`ERROR: ${error}`);

    // Output clean JSONL (no RESULT_JSON= prefix)
    console.log(JSON.stringify({
        type: 'ArchiveResult',
        status,
        output_str: output || error || '',
    }));

    process.exit(status === 'succeeded' ? 0 : 1);
}

main().catch(e => {
    console.error(`Fatal error: ${e.message}`);
    process.exit(1);
});
