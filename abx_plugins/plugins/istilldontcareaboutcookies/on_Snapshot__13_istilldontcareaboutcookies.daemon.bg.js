#!/usr/bin/env node
/**
 * Report cookie-consent elements hidden by the extension for the current snapshot.
 *
 * Runs as a background script before navigation and reports final counts on
 * snapshot shutdown.
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
const COOKIE_SELECTORS = [
    '.cky-consent-container',
    '.cky-popup-center',
    '.cky-overlay',
    '.cky-modal',
    '#onetrust-consent-sdk',
    '#onetrust-banner-sdk',
    '.onetrust-pc-dark-filter',
    '#CybotCookiebotDialog',
    '#CybotCookiebotDialogBodyUnderlay',
    '[class*="cookie-consent"]',
    '[class*="cookie-banner"]',
    '[class*="cookie-notice"]',
    '[class*="cookie-popup"]',
    '[class*="cookie-modal"]',
    '[class*="cookie-dialog"]',
    '[id*="cookie-consent"]',
    '[id*="cookie-banner"]',
    '[id*="cookie-notice"]',
    '[id*="cookieconsent"]',
    '[id*="cookie-law"]',
    '[class*="gdpr"]',
    '[id*="gdpr"]',
    '[class*="privacy-banner"]',
    '[class*="privacy-notice"]',
    '.cc-window',
    '.cc-banner',
    '#cc-main',
    '.qc-cmp2-container',
    '.sp-message-container',
];

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function formatPopupCount(count) {
    return `${count} cookie consent popup${count === 1 ? '' : 's'} hidden`;
}

async function countHiddenCookiePopups(page) {
    return page.evaluate((selectors) => {
        let hidden = 0;

        for (const selector of selectors) {
            try {
                const elements = document.querySelectorAll(selector);
                for (const el of elements) {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const isHidden = style.display === 'none' ||
                        style.visibility === 'hidden' ||
                        style.opacity === '0' ||
                        rect.width === 0 ||
                        rect.height === 0;
                    if (isHidden) {
                        hidden += 1;
                    }
                }
            } catch (error) {}
        }

        return hidden;
    }, COOKIE_SELECTORS);
}

async function main() {
    const args = parseArgs();
    if (!args.url) {
        console.error('Usage: on_Snapshot__13_istilldontcareaboutcookies.daemon.bg.js --url=<url>');
        process.exit(1);
    }

    if (!getEnvBool('ISTILLDONTCAREABOUTCOOKIES_ENABLED', true)) {
        emitArchiveResultRecord('skipped', 'ISTILLDONTCAREABOUTCOOKIES_ENABLED=False');
        process.exit(0);
    }

    const timeoutMs = getEnvInt('CHROME_TIMEOUT', getEnvInt('TIMEOUT', 60)) * 1000;
    const pollIntervalMs = 1000;

let browser = null;
let running = true;
let hiddenPopups = 0;
let lastProgressLine = '';

function emitProgress(line) {
    if (line && line !== lastProgressLine) {
        lastProgressLine = line;
        console.log(line);
    }
}

    const emitAndExit = () => {
        const status = hiddenPopups > 0 ? 'succeeded' : 'noresults';
        emitArchiveResultRecord(status, formatPopupCount(hiddenPopups));
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

        await waitForNavigationComplete(CHROME_SESSION_DIR, timeoutMs, 0);
        emitProgress(formatPopupCount(0));

        while (running) {
            try {
                hiddenPopups = Math.max(hiddenPopups, await countHiddenCookiePopups(page));
                emitProgress(formatPopupCount(hiddenPopups));
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
