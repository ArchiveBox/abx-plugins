#!/usr/bin/env node
/**
 * Report uBlock Origin blocking stats for the current snapshot.
 *
 * Runs as a background script before navigation, watches blocked ad requests,
 * and reports final counts when the snapshot phase shuts down.
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
} = require('../base/utils.js');
ensureNodeModuleResolution(module);

const {
    connectToPage,
    waitForNavigationComplete,
} = require('../chrome/chrome_utils.js');

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

const CHROME_SESSION_DIR = path.join(SNAP_DIR, 'chrome');
const AD_DOMAINS = [
    'doubleclick',
    'googlesyndication',
    'googleadservices',
    'facebook.com/tr',
    'analytics',
    'adservice',
    'advertising',
    'taboola',
    'outbrain',
    'criteo',
    'amazon-adsystem',
    'ads.yahoo',
    'gemini.yahoo',
    'yimg.com/cv/',
    'beap.gemini',
];
const AD_SELECTORS = [
    '[class*="ad-"]',
    '[class*="ad_"]',
    '[class*="-ad"]',
    '[class*="_ad"]',
    '[id*="ad-"]',
    '[id*="ad_"]',
    '[id*="-ad"]',
    '[id*="_ad"]',
    '[class*="advertisement"]',
    '[id*="advertisement"]',
    '[class*="sponsored"]',
    '[id*="sponsored"]',
    'ins.adsbygoogle',
    '[data-ad-client]',
    '[data-ad-slot]',
    '[class*="gemini"]',
    '[data-beacon]',
    '[class*="native-ad"]',
    '[class*="stream-ad"]',
    '[class*="LDRB"]',
    '[class*="ntv-ad"]',
    'iframe[src*="ad"]',
    'iframe[src*="doubleclick"]',
    'iframe[src*="googlesyndication"]',
    '[style*="300px"][style*="250px"]',
    '[style*="728px"][style*="90px"]',
    '[style*="160px"][style*="600px"]',
    '[style*="320px"][style*="50px"]',
];

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function formatProgress(blockedRequests, hiddenElements) {
    return `${blockedRequests} ad request${blockedRequests === 1 ? '' : 's'} blocked | ${hiddenElements} element${hiddenElements === 1 ? '' : 's'} hidden`;
}

let lastProgressLine = '';

function emitProgress(blockedRequests, hiddenElements) {
    const line = formatProgress(blockedRequests, hiddenElements);
    if (line !== lastProgressLine) {
        lastProgressLine = line;
        console.log(line);
    }
}

async function countHiddenAdElements(page) {
    return page.evaluate((selectors) => {
        let found = 0;
        let visible = 0;

        for (const selector of selectors) {
            try {
                const elements = document.querySelectorAll(selector);
                for (const el of elements) {
                    found += 1;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const isVisible = style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        style.opacity !== '0' &&
                        rect.width > 0 &&
                        rect.height > 0;
                    if (isVisible) {
                        visible += 1;
                    }
                }
            } catch (error) {}
        }

        return Math.max(found - visible, 0);
    }, AD_SELECTORS);
}

async function main() {
    const args = parseArgs();
    if (!args.url) {
        console.error('Usage: on_Snapshot__12_ublock.daemon.bg.js --url=<url>');
        process.exit(1);
    }

    if (!getEnvBool('UBLOCK_ENABLED', true)) {
        emitArchiveResultRecord('skipped', 'UBLOCK_ENABLED=False');
        process.exit(0);
    }

    const timeoutMs = getEnvInt('CHROME_TIMEOUT', getEnvInt('TIMEOUT', 60)) * 1000;
    const pollIntervalMs = 1000;

    let browser = null;
    let running = true;
    let blockedRequests = 0;
    let hiddenElements = 0;

    const emitAndExit = () => {
        const status = blockedRequests > 0 || hiddenElements > 0 ? 'succeeded' : 'noresults';
        emitArchiveResultRecord(
            status,
            `${blockedRequests} ad requests blocked | ${hiddenElements} elements hidden`,
        );
        if (browser) {
            try {
                browser.disconnect();
            } catch (error) {}
        }
        process.exit(0);
    };

    process.on('SIGTERM', () => {
        running = false;
        emitAndExit();
    });

    try {
        const connection = await connectToPage({
            chromeSessionDir: CHROME_SESSION_DIR,
            timeoutMs,
            requireTargetId: true,
        });
        browser = connection.browser;
        const page = connection.page;
        emitProgress(blockedRequests, hiddenElements);

        page.on('requestfailed', request => {
            const url = request.url().toLowerCase();
            const failure = request.failure();
            const errorText = (failure && failure.errorText) || '';
            if (
                AD_DOMAINS.some(domain => url.includes(domain)) &&
                errorText.toUpperCase().includes('ERR_BLOCKED_BY_CLIENT')
            ) {
                blockedRequests += 1;
                emitProgress(blockedRequests, hiddenElements);
            }
        });

        await waitForNavigationComplete(CHROME_SESSION_DIR, timeoutMs, 0);

        while (running) {
            try {
                hiddenElements = Math.max(hiddenElements, await countHiddenAdElements(page));
                emitProgress(blockedRequests, hiddenElements);
            } catch (error) {
                if (!running) break;
            }
            await sleep(pollIntervalMs);
        }
    } catch (error) {
        if (browser) {
            try {
                browser.disconnect();
            } catch (disconnectError) {}
        }
        console.error(`ERROR: ${error.name}: ${error.message}`);
        process.exit(1);
    }
}

main().catch(error => {
    console.error(`Fatal error: ${error.message}`);
    process.exit(1);
});
