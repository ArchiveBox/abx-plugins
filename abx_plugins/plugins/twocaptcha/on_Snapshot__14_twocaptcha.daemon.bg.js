#!/usr/bin/env node
/**
 * Report CAPTCHA solve counts observed during the current snapshot.
 *
 * Runs as a background script before navigation and watches the live page for
 * solved CAPTCHA response tokens until snapshot shutdown.
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

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function formatCaptchaCount(count) {
    return `${count} captcha${count === 1 ? '' : 's'} solved`;
}

async function countSolvedCaptchas(page) {
    return page.evaluate(() => {
        const solved = new Set();
        const selectors = [
            'textarea[name="g-recaptcha-response"]',
            'textarea#g-recaptcha-response',
            'input[name="g-recaptcha-response"]',
            'textarea[name="h-captcha-response"]',
            'input[name="h-captcha-response"]',
            'textarea[name="cf-turnstile-response"]',
            'input[name="cf-turnstile-response"]',
        ];

        for (const selector of selectors) {
            try {
                const elements = document.querySelectorAll(selector);
                for (const el of elements) {
                    const value = typeof el.value === 'string' ? el.value.trim() : '';
                    if (value.length > 20) {
                        solved.add(`${selector}:${value.slice(0, 128)}`);
                    }
                }
            } catch (error) {}
        }

        try {
            if (typeof grecaptcha !== 'undefined' && typeof grecaptcha.getResponse === 'function') {
                const response = grecaptcha.getResponse();
                if (typeof response === 'string' && response.trim().length > 20) {
                    solved.add(`grecaptcha:${response.trim().slice(0, 128)}`);
                }
            }
        } catch (error) {}

        return solved.size;
    });
}

async function main() {
    const args = parseArgs();
    if (!args.url) {
        console.error('Usage: on_Snapshot__14_twocaptcha.daemon.bg.js --url=<url>');
        process.exit(1);
    }

    if (!getEnvBool('TWOCAPTCHA_ENABLED', true)) {
        emitArchiveResultRecord('skipped', 'TWOCAPTCHA_ENABLED=False');
        process.exit(0);
    }

    const apiKey = (hookConfig.TWOCAPTCHA_API_KEY || '').trim();
    if (!apiKey || apiKey === 'YOUR_API_KEY_HERE') {
        emitArchiveResultRecord('skipped', 'TWOCAPTCHA_API_KEY=None');
        process.exit(0);
    }

    const timeoutMs = getEnvInt('TWOCAPTCHA_TIMEOUT', getEnvInt('CHROME_TIMEOUT', getEnvInt('TIMEOUT', 60))) * 1000;
    const pollIntervalMs = 2000;

    let browser = null;
    let running = true;
    let solvedCaptchas = 0;

    const emitAndExit = () => {
        const status = solvedCaptchas > 0 ? 'succeeded' : 'noresults';
        emitArchiveResultRecord(status, formatCaptchaCount(solvedCaptchas));
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

        while (running) {
            try {
                solvedCaptchas = Math.max(solvedCaptchas, await countSolvedCaptchas(page));
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
