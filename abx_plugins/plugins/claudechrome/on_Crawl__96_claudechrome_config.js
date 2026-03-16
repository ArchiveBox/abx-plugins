#!/usr/bin/env node
/**
 * Claude for Chrome Extension - Configuration
 *
 * Configures the Claude for Chrome extension with the ANTHROPIC_API_KEY after
 * Chrome session starts. Injects the API key into chrome.storage.local so the
 * extension can authenticate without manual OAuth login.
 *
 * Priority: 96 (after chrome_launch at 90)
 * Hook: on_Crawl (runs once per crawl, not per snapshot)
 *
 * Note: Claude for Chrome normally uses OAuth PKCE (claude.com login).
 * This hook attempts to inject the API key directly into extension storage.
 * If this doesn't work with your extension version, you may need to log in
 * manually via the Chrome session before archiving.
 */

const path = require('path');
const fs = require('fs');
const { ensureNodeModuleResolution } = require('../base/utils.js');
ensureNodeModuleResolution(module);

const { getEnv, getEnvBool } = require('../chrome/chrome_utils.js');

// Check if enabled
if (!getEnvBool('CLAUDECHROME_ENABLED', false)) {
    process.exit(0);
}

const puppeteer = require('puppeteer-core');

const PLUGIN_DIR = path.basename(__dirname);
const CRAWL_DIR = path.resolve((process.env.CRAWL_DIR || '.').trim());
const OUTPUT_DIR = path.join(CRAWL_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

function getCrawlChromeSessionDir() {
    return path.join(path.resolve(process.env.CRAWL_DIR || '.'), 'chrome');
}

const CHROME_SESSION_DIR = getCrawlChromeSessionDir();
const CONFIG_MARKER = path.join(CHROME_SESSION_DIR, '.claudechrome_configured');

async function configureClaudeChrome() {
    // Check if already configured in this session
    if (fs.existsSync(CONFIG_MARKER)) {
        console.error('[*] Claude for Chrome already configured in this session');
        return { success: true, skipped: true };
    }

    const apiKey = getEnv('ANTHROPIC_API_KEY');
    if (!apiKey) {
        console.warn('[!] ANTHROPIC_API_KEY not set, skipping Claude for Chrome config');
        return { success: false, error: 'ANTHROPIC_API_KEY not set' };
    }

    console.error('[*] Configuring Claude for Chrome extension...');
    console.error(`[*]   API Key: ${apiKey.slice(0, 8)}...${apiKey.slice(-4)}`);

    try {
        const cdpFile = path.join(CHROME_SESSION_DIR, 'cdp_url.txt');
        if (!fs.existsSync(cdpFile)) {
            return { success: false, error: 'No Chrome session found (chrome plugin must run first)' };
        }

        const cdpUrl = fs.readFileSync(cdpFile, 'utf-8').trim();
        const browser = await puppeteer.connect({ browserWSEndpoint: cdpUrl });

        try {
            // Wake up the extension by visiting a page
            console.error('[*] Waking up extension...');
            const triggerPage = await browser.newPage();
            try {
                await triggerPage.goto('https://www.google.com', { waitUntil: 'domcontentloaded', timeout: 10000 });
                await new Promise(r => setTimeout(r, 3000));
            } catch (e) {
                console.warn(`[!] Trigger page: ${e.message}`);
            }
            try { await triggerPage.close(); } catch (e) {}

            // Read extension metadata
            const extensionsFile = path.join(CHROME_SESSION_DIR, 'extensions.json');
            if (!fs.existsSync(extensionsFile)) {
                return { success: false, error: 'extensions.json not found' };
            }

            const extensions = JSON.parse(fs.readFileSync(extensionsFile, 'utf-8'));
            const claudeExt = extensions.find(ext => ext.name === 'claudechrome');
            if (!claudeExt || !claudeExt.id) {
                console.error('[*] Claude for Chrome extension not found in extensions.json');
                return { success: true, skipped: true };
            }

            const extensionId = claudeExt.id;
            console.error(`[*] Claude for Chrome Extension ID: ${extensionId}`);

            // Try to find an extension page to inject config
            // The extension may have a popup, options page, or side panel
            const pages = await browser.pages();
            let extPage = pages.find(p => p.url().startsWith(`chrome-extension://${extensionId}`));

            if (!extPage) {
                // Try opening the extension's popup or background page
                extPage = await browser.newPage();
                try {
                    // Try common extension pages
                    for (const pagePath of ['popup.html', 'index.html', 'options.html', 'sidepanel.html']) {
                        try {
                            await extPage.goto(`chrome-extension://${extensionId}/${pagePath}`, {
                                waitUntil: 'domcontentloaded',
                                timeout: 5000,
                            });
                            if (extPage.url().startsWith(`chrome-extension://${extensionId}`)) {
                                break;
                            }
                        } catch (e) {
                            continue;
                        }
                    }
                } catch (e) {
                    console.error(`[*] Could not open extension page: ${e.message}`);
                }
            }

            if (!extPage || !extPage.url().startsWith(`chrome-extension://${extensionId}`)) {
                console.warn('[!] Could not access extension context for config injection');
                // Still mark as configured so we don't retry
                fs.writeFileSync(CONFIG_MARKER, JSON.stringify({
                    timestamp: new Date().toISOString(),
                    extensionId,
                    configured: false,
                    note: 'Could not access extension context - user may need to log in manually',
                }, null, 2));
                return { success: true, skipped: true };
            }

            // Inject API key into chrome.storage.local
            // Claude for Chrome internally uses the Anthropic SDK, so we try to
            // set the API key in the storage format it expects
            const result = await extPage.evaluate((key) => {
                return new Promise((resolve) => {
                    if (typeof chrome === 'undefined' || !chrome.storage) {
                        resolve({ success: false, error: 'chrome.storage not available' });
                        return;
                    }

                    // Try setting the API key in various storage formats the extension might use
                    const configData = {
                        anthropicApiKey: key,
                        apiKey: key,
                        api_key: key,
                    };

                    chrome.storage.local.set(configData, () => {
                        if (chrome.runtime.lastError) {
                            resolve({ success: false, error: chrome.runtime.lastError.message });
                        } else {
                            resolve({ success: true });
                        }
                    });
                });
            }, apiKey);

            try { await extPage.close(); } catch (e) {}

            if (result.success) {
                console.error('[+] Claude for Chrome API key injected into extension storage');
            } else {
                console.warn(`[!] Storage injection failed: ${result.error}`);
                console.warn('[!] User may need to log in to claude.com manually in the Chrome session');
            }

            // Write marker file
            fs.writeFileSync(CONFIG_MARKER, JSON.stringify({
                timestamp: new Date().toISOString(),
                extensionId,
                configured: result.success,
            }, null, 2));

            return { success: true };
        } finally {
            browser.disconnect();
        }
    } catch (e) {
        return { success: false, error: `${e.name}: ${e.message}` };
    }
}

async function main() {
    const result = await configureClaudeChrome();

    if (result.skipped) {
        process.exit(0);
    } else if (result.success) {
        console.error('[+] Claude for Chrome configuration complete');
        process.exit(0);
    } else {
        console.error(`ERROR: ${result.error}`);
        // Non-fatal - extension may still work with manual login
        process.exit(0);
    }
}

main().catch(e => {
    console.error(`Fatal error: ${e.message}`);
    process.exit(1);
});
