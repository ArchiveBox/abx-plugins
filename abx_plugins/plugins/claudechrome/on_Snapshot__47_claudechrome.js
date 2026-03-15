#!/usr/bin/env node
/**
 * Claude for Chrome - Snapshot Hook
 *
 * Runs Claude for Chrome on the current page with a user-configurable prompt.
 * Uses the extension's side panel to execute actions on the page (e.g. clicking
 * "expand" buttons, downloading PDFs, filling in search fields, etc.).
 *
 * Priority: 47 - After twocaptcha (crawl-level) and infiniscroll (45), before
 *               singlefile (50), screenshot (51), and other extractors.
 *
 * Usage: on_Snapshot__47_claudechrome.js --url=<url> --snapshot-id=<uuid>
 * Output: Creates claudechrome/ directory with conversation log and any downloads
 *
 * Environment variables:
 *     CLAUDECHROME_ENABLED: Enable/disable (default: false)
 *     CLAUDECHROME_PROMPT: Prompt for Claude to execute on the page
 *     CLAUDECHROME_TIMEOUT: Timeout in seconds (default: 120)
 *     CLAUDECHROME_MODEL: Claude model to use (default: sonnet)
 *     ANTHROPIC_API_KEY: API key for Anthropic
 */

const fs = require('fs');
const path = require('path');
const os = require('os');
// Add NODE_MODULES_DIR to module resolution paths if set
if (process.env.NODE_MODULES_DIR) module.paths.unshift(process.env.NODE_MODULES_DIR);

const {
    getEnv,
    getEnvBool,
    getEnvInt,
    parseArgs,
    readCdpUrl,
    connectToPage,
    waitForPageLoaded,
    waitForExtensionsMetadata,
    findExtensionMetadataByName,
} = require('../chrome/chrome_utils.js');

// Check if enabled BEFORE requiring puppeteer
if (!getEnvBool('CLAUDECHROME_ENABLED', false)) {
    console.error('Skipping Claude for Chrome (CLAUDECHROME_ENABLED=False)');
    console.log(JSON.stringify({
        type: 'ArchiveResult',
        status: 'skipped',
        output_str: 'CLAUDECHROME_ENABLED=False',
    }));
    process.exit(0);
}

// Check for API key BEFORE requiring puppeteer (so tests can run without node deps)
if (!getEnv('ANTHROPIC_API_KEY')) {
    console.error('ERROR: ANTHROPIC_API_KEY not set');
    console.log(JSON.stringify({
        type: 'ArchiveResult',
        status: 'failed',
        output_str: 'ANTHROPIC_API_KEY not set',
    }));
    process.exit(1);
}

const puppeteer = require('puppeteer-core');

const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((process.env.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

const CHROME_SESSION_DIR = path.join(SNAP_DIR, '..', 'chrome');
const DOWNLOADS_DIR = process.env.CHROME_DOWNLOADS_DIR ||
    path.join(process.env.PERSONAS_DIR || path.join(os.homedir(), '.config', 'abx', 'personas'),
        process.env.ACTIVE_PERSONA || 'Default',
        'chrome_downloads');

const DEFAULT_PROMPT = 'Look at the current page. If there are any "expand", "show more", ' +
    '"load more", or similar buttons/links, click them all to reveal hidden content. ' +
    'Report what you did.';

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * Record existing files in a directory for later diffing.
 */
function snapshotDirFiles(dir) {
    const files = new Set();
    if (fs.existsSync(dir)) {
        for (const entry of fs.readdirSync(dir)) {
            files.add(entry);
        }
    }
    return files;
}

/**
 * Set Chrome download directory via CDP.
 */
async function setDownloadDir(page, downloadDir) {
    try {
        await fs.promises.mkdir(downloadDir, { recursive: true });
        const client = await page.target().createCDPSession();
        try {
            await client.send('Page.setDownloadBehavior', {
                behavior: 'allow',
                downloadPath: downloadDir,
            });
        } catch (err) {
            await client.send('Browser.setDownloadBehavior', {
                behavior: 'allow',
                downloadPath: downloadDir,
            });
        }
    } catch (err) {
        console.error(`[!] Failed to set download directory: ${err.message || err}`);
    }
}

/**
 * Move new files from downloads directory to output directory.
 */
async function moveNewDownloads(downloadsDir, outputDir, previousFiles) {
    const moved = [];
    if (!fs.existsSync(downloadsDir)) return moved;

    for (const entry of fs.readdirSync(downloadsDir)) {
        if (previousFiles.has(entry)) continue;
        // Skip incomplete downloads
        if (entry.endsWith('.crdownload') || entry.endsWith('.tmp')) continue;

        const src = path.join(downloadsDir, entry);
        const dst = path.join(outputDir, entry);

        try {
            // Use rename for same-filesystem moves, fallback to copy+delete
            try {
                await fs.promises.rename(src, dst);
            } catch (e) {
                await fs.promises.copyFile(src, dst);
                await fs.promises.unlink(src);
            }
            moved.push(entry);
            console.error(`[+] Moved download: ${entry}`);
        } catch (e) {
            console.error(`[!] Failed to move ${entry}: ${e.message}`);
        }
    }
    return moved;
}

/**
 * Open the Claude for Chrome side panel and run a prompt.
 *
 * This interacts with the extension's side panel UI:
 * 1. Opens the side panel via chrome.sidePanel API
 * 2. Finds the prompt input area
 * 3. Types the prompt and submits
 * 4. Waits for the response to complete
 * 5. Captures the conversation text
 */
async function runClaudeOnPage(browser, page, extensionId, prompt, timeout) {
    const startTime = Date.now();
    const conversation = [];

    console.error(`[*] Running Claude for Chrome with prompt: ${prompt.slice(0, 100)}...`);
    console.error(`[*] Extension ID: ${extensionId}`);

    // Try to open the side panel via CDP
    // The extension registers a side panel, we trigger it via the action button
    try {
        // Find extension targets (service worker or popup)
        const targets = await browser.targets();
        const extTarget = targets.find(t => {
            const url = t.url();
            return url.startsWith(`chrome-extension://${extensionId}`);
        });

        if (!extTarget) {
            console.error('[!] Extension target not found - extension may not be running');
            return { success: false, conversation, error: 'Extension target not found' };
        }

        // Try to open side panel by clicking the extension's action button
        // This uses CDP to simulate the toolbar button click
        const tabId = page.target()._targetId;

        // Try using chrome.sidePanel.open via the service worker
        let workerTarget = targets.find(t =>
            t.type() === 'service_worker' &&
            t.url().startsWith(`chrome-extension://${extensionId}`)
        );

        if (workerTarget) {
            try {
                const worker = await workerTarget.worker();
                if (worker) {
                    await worker.evaluate(`
                        chrome.sidePanel.open({ tabId: ${JSON.stringify(tabId)} })
                            .catch(e => console.error('sidePanel.open failed:', e));
                    `);
                    await sleep(3000);
                    console.error('[+] Side panel opened via service worker');
                }
            } catch (e) {
                console.error(`[*] Service worker sidePanel.open: ${e.message}`);
            }
        }

        // Find the side panel page
        await sleep(2000);
        const allPages = await browser.pages();
        let sidePanelPage = allPages.find(p => {
            const url = p.url();
            return url.startsWith(`chrome-extension://${extensionId}`) &&
                (url.includes('sidepanel') || url.includes('side_panel') || url.includes('panel'));
        });

        if (!sidePanelPage) {
            // Try to find any extension page that might be the UI
            sidePanelPage = allPages.find(p =>
                p.url().startsWith(`chrome-extension://${extensionId}`) &&
                !p.url().includes('background')
            );
        }

        if (!sidePanelPage) {
            console.error('[!] Could not find extension side panel page');
            console.error('[*] Available pages:');
            for (const p of allPages) {
                console.error(`    ${p.url()}`);
            }
            return { success: false, conversation, error: 'Side panel not found' };
        }

        console.error(`[+] Found extension UI at: ${sidePanelPage.url()}`);

        // Wait for the UI to be ready
        await sleep(2000);

        // Find the text input area and type the prompt
        // Claude for Chrome uses a textarea or contenteditable div for input
        const inputResult = await sidePanelPage.evaluate(async (promptText) => {
            // Try common input selectors
            const selectors = [
                'textarea',
                '[contenteditable="true"]',
                'input[type="text"]',
                '[role="textbox"]',
                '.ProseMirror',
                '[data-placeholder]',
            ];

            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el) {
                    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                        el.value = promptText;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    } else {
                        el.textContent = promptText;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                    return { found: true, selector: sel };
                }
            }
            return { found: false };
        }, prompt);

        if (!inputResult.found) {
            console.error('[!] Could not find input field in extension UI');
            return { success: false, conversation, error: 'Input field not found' };
        }

        console.error(`[+] Typed prompt into ${inputResult.selector}`);

        // Submit the prompt (press Enter or click submit button)
        await sidePanelPage.evaluate(() => {
            // Try clicking a submit button first
            const submitSelectors = [
                'button[type="submit"]',
                'button[aria-label*="send" i]',
                'button[aria-label*="submit" i]',
                'button:has(svg)',  // Icon-only button (common for chat UIs)
            ];

            for (const sel of submitSelectors) {
                const btn = document.querySelector(sel);
                if (btn && !btn.disabled) {
                    btn.click();
                    return true;
                }
            }

            // Fallback: press Enter on the input
            const input = document.querySelector('textarea, [contenteditable="true"], input[type="text"]');
            if (input) {
                input.dispatchEvent(new KeyboardEvent('keydown', {
                    key: 'Enter',
                    code: 'Enter',
                    bubbles: true,
                }));
                return true;
            }
            return false;
        });

        console.error('[*] Prompt submitted, waiting for response...');

        // Wait for the response to complete
        // Poll for new content and detect when the agent stops
        let lastContent = '';
        let stableCount = 0;
        const pollInterval = 3000;
        const maxStable = 5; // 5 * 3s = 15s of no change = done

        while (Date.now() - startTime < timeout * 1000) {
            await sleep(pollInterval);

            const content = await sidePanelPage.evaluate(() => {
                // Capture all visible text in the chat area
                const chatSelectors = [
                    '[role="log"]',
                    '.messages',
                    '.chat-messages',
                    '.conversation',
                    'main',
                    '#root',
                ];

                for (const sel of chatSelectors) {
                    const el = document.querySelector(sel);
                    if (el && el.textContent.trim().length > 0) {
                        return el.textContent.trim();
                    }
                }
                return document.body?.textContent?.trim() || '';
            }).catch(() => '');

            if (content === lastContent) {
                stableCount++;
                if (stableCount >= maxStable) {
                    console.error('[+] Response appears complete (content stable)');
                    break;
                }
            } else {
                stableCount = 0;
                lastContent = content;
                console.error(`[*] Response growing... (${content.length} chars)`);
            }

            // Check for loading/thinking indicators
            const isLoading = await sidePanelPage.evaluate(() => {
                const loadingSelectors = [
                    '.loading', '.spinner', '[aria-busy="true"]',
                    '.thinking', '.generating',
                ];
                return loadingSelectors.some(sel => document.querySelector(sel) !== null);
            }).catch(() => false);

            if (!isLoading && stableCount >= 2) {
                console.error('[+] Response complete (no loading indicator)');
                break;
            }
        }

        // Capture the final conversation
        const finalContent = await sidePanelPage.evaluate(() => {
            // Get structured conversation if possible
            const messages = document.querySelectorAll('[data-role], [class*="message"]');
            if (messages.length > 0) {
                return Array.from(messages).map(m => ({
                    role: m.getAttribute('data-role') ||
                        (m.className.includes('user') ? 'user' : 'assistant'),
                    text: m.textContent.trim(),
                }));
            }
            // Fallback to raw text
            return document.body?.textContent?.trim() || '';
        }).catch(() => lastContent);

        if (typeof finalContent === 'string') {
            conversation.push({ role: 'full_text', text: finalContent });
        } else {
            conversation.push(...finalContent);
        }

        try { await sidePanelPage.close(); } catch (e) {}

        return { success: true, conversation };

    } catch (e) {
        console.error(`[!] Error running Claude on page: ${e.message}`);
        return { success: false, conversation, error: e.message };
    }
}

async function main() {
    const args = parseArgs();
    const url = args.url;
    const snapshotId = args.snapshot_id;

    if (!url || !snapshotId) {
        console.error('Usage: on_Snapshot__47_claudechrome.js --url=<url> --snapshot-id=<uuid>');
        process.exit(1);
    }

    const prompt = getEnv('CLAUDECHROME_PROMPT', DEFAULT_PROMPT);
    const timeout = getEnvInt('CLAUDECHROME_TIMEOUT', 120);

    let browser = null;

    try {
        if (!readCdpUrl(CHROME_SESSION_DIR)) {
            throw new Error('No Chrome session found (chrome plugin must run first)');
        }

        // Snapshot pre-existing downloads before we start
        const previousDownloads = snapshotDirFiles(DOWNLOADS_DIR);

        // Connect to the page
        const connectTimeoutMs = Math.min(timeout * 1000, getEnvInt('TIMEOUT', 30) * 1000);
        const connection = await connectToPage({
            chromeSessionDir: CHROME_SESSION_DIR,
            timeoutMs: connectTimeoutMs,
            puppeteer,
        });
        browser = connection.browser;
        const page = connection.page;
        await waitForPageLoaded(CHROME_SESSION_DIR, connectTimeoutMs * 4, 200);

        // Set download directory so any files Claude triggers go to a known location
        await setDownloadDir(page, DOWNLOADS_DIR);

        console.error(`[*] Running Claude for Chrome on ${url}`);

        // Get extension ID from session metadata
        const sessionExtensions = await waitForExtensionsMetadata(CHROME_SESSION_DIR, 15000);
        const extMeta = findExtensionMetadataByName(sessionExtensions, 'claudechrome');

        if (!extMeta || !extMeta.id) {
            throw new Error('Claude for Chrome extension not found in session metadata');
        }

        // Run Claude on the page
        const result = await runClaudeOnPage(browser, page, extMeta.id, prompt, timeout);

        browser.disconnect();
        browser = null;

        // Save conversation log
        const logPath = path.join(OUTPUT_DIR, 'conversation.json');
        fs.writeFileSync(logPath, JSON.stringify({
            url,
            snapshotId,
            prompt,
            timestamp: new Date().toISOString(),
            success: result.success,
            error: result.error || null,
            conversation: result.conversation,
        }, null, 2));
        console.error(`[+] Conversation saved to ${logPath}`);

        // Also save a human-readable version
        const readablePath = path.join(OUTPUT_DIR, 'conversation.txt');
        let readableText = `URL: ${url}\nPrompt: ${prompt}\nTimestamp: ${new Date().toISOString()}\n\n`;
        for (const msg of result.conversation) {
            readableText += `--- ${msg.role || 'unknown'} ---\n${msg.text}\n\n`;
        }
        fs.writeFileSync(readablePath, readableText);

        // Move any new downloads to the output directory
        // Wait briefly for any in-progress downloads to complete
        await sleep(2000);
        const movedFiles = await moveNewDownloads(DOWNLOADS_DIR, OUTPUT_DIR, previousDownloads);
        if (movedFiles.length > 0) {
            console.error(`[+] Moved ${movedFiles.length} download(s): ${movedFiles.join(', ')}`);
        }

        // Emit result
        const outputFiles = [];
        if (fs.existsSync(logPath)) outputFiles.push('conversation.json');
        outputFiles.push(...movedFiles);

        const outputStr = outputFiles.length > 0
            ? outputFiles.join(', ')
            : (result.success ? 'completed' : result.error || 'failed');

        console.log(JSON.stringify({
            type: 'ArchiveResult',
            status: result.success ? 'succeeded' : 'failed',
            output_str: outputStr,
        }));

        process.exit(result.success ? 0 : 1);

    } catch (e) {
        if (browser) browser.disconnect();
        console.error(`ERROR: ${e.name}: ${e.message}`);
        console.log(JSON.stringify({
            type: 'ArchiveResult',
            status: 'failed',
            output_str: `${e.name}: ${e.message}`,
        }));
        process.exit(1);
    }
}

main().catch(e => {
    console.error(`Fatal error: ${e.message}`);
    process.exit(1);
});
