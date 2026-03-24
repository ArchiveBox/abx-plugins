#!/usr/bin/env node
/**
 * Claude for Chrome - Snapshot Hook
 *
 * Uses the Anthropic API with CDP to let Claude interact with the current page.
 * Takes screenshots, sends them to Claude with the user's prompt, and executes
 * the actions Claude requests (click, type, scroll, etc.) via CDP.
 *
 * This replicates what the Claude for Chrome extension does internally, but
 * works reliably in headless/automated mode without requiring OAuth login.
 *
 * Priority: 47 - After twocaptcha (crawl-level) and infiniscroll (45), before
 *               singlefile (50), screenshot (51), and other extractors.
 *
 * Usage: on_Snapshot__47_claudechrome.js --url=<url>
 * Output: Creates claudechrome/ directory with conversation log and any downloads
 *
 * Environment variables:
 *     CLAUDECHROME_ENABLED: Enable/disable (default: false)
 *     CLAUDECHROME_PROMPT: Prompt for Claude to execute on the page
 *     CLAUDECHROME_TIMEOUT: Timeout in seconds (default: 120)
 *     CLAUDECHROME_MODEL: Claude model to use (default: claude-sonnet-4-5-20250514)
 *     CLAUDECHROME_MAX_ACTIONS: Max agentic loop iterations (default: 15)
 *     ANTHROPIC_API_KEY: API key for Anthropic
 */

const fs = require('fs');
const path = require('path');
const {
    ensureNodeModuleResolution,
    emitArchiveResultRecord,
    getEnv,
    getEnvBool,
    getEnvInt,
    loadConfig,
    parseArgs,
} = require('../base/utils.js');
ensureNodeModuleResolution(module);
const {
    connectToPage,
    resolvePuppeteerModule,
    setBrowserDownloadBehavior,
} = require('../chrome/chrome_utils.js');

// Check if enabled BEFORE requiring puppeteer
if (!getEnvBool('CLAUDECHROME_ENABLED', false)) {
    console.error('Skipping Claude for Chrome (CLAUDECHROME_ENABLED=False)');
    emitArchiveResultRecord('skipped', 'CLAUDECHROME_ENABLED=False');
    process.exit(0);
}

// Check for API key BEFORE requiring puppeteer (so tests can run without node deps)
if (!getEnv('ANTHROPIC_API_KEY')) {
    console.error('ERROR: ANTHROPIC_API_KEY not set');
    emitArchiveResultRecord('failed', 'ANTHROPIC_API_KEY not set');
    process.exit(1);
}

const puppeteer = resolvePuppeteerModule();
const { execFileSync } = require('child_process');

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

const CHROME_SESSION_DIR = '../chrome';
const DOWNLOADS_DIR = hookConfig.CHROME_DOWNLOADS_DIR ||
    path.join(hookConfig.PERSONAS_DIR,
        hookConfig.ACTIVE_PERSONA,
        'chrome_downloads');

const DEFAULT_PROMPT = 'Look at the current page. If there are any "expand", "show more", ' +
    '"load more", or similar buttons/links, click them all to reveal hidden content. ' +
    'Report what you did.';

// Model name mapping (short names -> full model IDs)
const MODEL_MAP = {
    'sonnet': 'claude-sonnet-4-5-20250514',
    'haiku': 'claude-haiku-4-5-20251001',
    'opus': 'claude-opus-4-20250514',
};

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function snapshotDirFiles(dir) {
    const files = new Set();
    if (fs.existsSync(dir)) {
        for (const entry of fs.readdirSync(dir)) {
            files.add(entry);
        }
    }
    return files;
}

async function moveNewDownloads(downloadsDir, outputDir, previousFiles) {
    const moved = [];
    if (!fs.existsSync(downloadsDir)) return moved;

    for (const entry of fs.readdirSync(downloadsDir)) {
        if (previousFiles.has(entry)) continue;
        if (entry.endsWith('.crdownload') || entry.endsWith('.tmp')) continue;

        const src = path.join(downloadsDir, entry);
        const dst = path.join(outputDir, entry);

        try {
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
 * Take a screenshot of the page via CDP and return as base64 PNG.
 */
async function takeScreenshot(cdpClient, viewport) {
    const result = await cdpClient.send('Page.captureScreenshot', {
        format: 'png',
        clip: {
            x: 0,
            y: 0,
            width: viewport.width,
            height: viewport.height,
            scale: 1,
        },
    });
    return result.data; // base64 PNG
}

/**
 * Execute a computer-use action on the page.
 *
 * The computer_20250124 tool returns actions in `input.action` format:
 *   left_click, right_click, middle_click, double_click, triple_click,
 *   type, key, scroll, screenshot, wait, mouse_move, left_click_drag
 *
 * Coordinates are in `input.coordinate` as [x, y].
 * Text input is in `input.text`. Key presses are in `input.key`.
 * Scroll direction is in `input.scroll_direction` and amount in `input.scroll_amount`.
 */
async function executeAction(page, cdpClient, action, viewport) {
    const actionType = action.action || action.type;

    switch (actionType) {
        case 'left_click':
        case 'click': {
            const { coordinate } = action;
            if (!coordinate || coordinate.length !== 2) {
                console.error(`[!] Invalid click coordinate: ${JSON.stringify(coordinate)}`);
                return;
            }
            const [x, y] = coordinate;
            console.error(`[*] Click at (${x}, ${y})`);
            await page.mouse.click(x, y);
            await sleep(500);
            break;
        }

        case 'right_click': {
            const { coordinate } = action;
            if (coordinate && coordinate.length === 2) {
                const [x, y] = coordinate;
                console.error(`[*] Right-click at (${x}, ${y})`);
                await page.mouse.click(x, y, { button: 'right' });
                await sleep(500);
            }
            break;
        }

        case 'middle_click': {
            const { coordinate } = action;
            if (coordinate && coordinate.length === 2) {
                const [x, y] = coordinate;
                console.error(`[*] Middle-click at (${x}, ${y})`);
                await page.mouse.click(x, y, { button: 'middle' });
                await sleep(500);
            }
            break;
        }

        case 'double_click': {
            const { coordinate } = action;
            if (coordinate && coordinate.length === 2) {
                const [x, y] = coordinate;
                console.error(`[*] Double-click at (${x}, ${y})`);
                await page.mouse.click(x, y, { clickCount: 2 });
                await sleep(500);
            }
            break;
        }

        case 'triple_click': {
            const { coordinate } = action;
            if (coordinate && coordinate.length === 2) {
                const [x, y] = coordinate;
                console.error(`[*] Triple-click at (${x}, ${y})`);
                await page.mouse.click(x, y, { clickCount: 3 });
                await sleep(500);
            }
            break;
        }

        case 'type': {
            const { text } = action;
            if (!text) { console.error('[!] No text to type'); return; }
            console.error(`[*] Type: "${text.slice(0, 50)}${text.length > 50 ? '...' : ''}"`);
            await page.keyboard.type(text, { delay: 30 });
            await sleep(300);
            break;
        }

        case 'key': {
            const keyVal = action.key || action.text;
            if (!keyVal) { console.error('[!] No key specified'); return; }
            console.error(`[*] Key: ${keyVal}`);
            // Map computer_use key names to puppeteer equivalents
            const keyMap = {
                'Return': 'Enter',
                'enter': 'Enter',
                'space': 'Space',
                'tab': 'Tab',
                'Tab': 'Tab',
                'escape': 'Escape',
                'Escape': 'Escape',
                'backspace': 'Backspace',
                'BackSpace': 'Backspace',
                'delete': 'Delete',
                'Delete': 'Delete',
                'up': 'ArrowUp',
                'Up': 'ArrowUp',
                'down': 'ArrowDown',
                'Down': 'ArrowDown',
                'left': 'ArrowLeft',
                'Left': 'ArrowLeft',
                'right': 'ArrowRight',
                'Right': 'ArrowRight',
                'page_up': 'PageUp',
                'Page_Up': 'PageUp',
                'page_down': 'PageDown',
                'Page_Down': 'PageDown',
                'home': 'Home',
                'Home': 'Home',
                'end': 'End',
                'End': 'End',
                'F1': 'F1', 'F2': 'F2', 'F3': 'F3', 'F4': 'F4',
                'F5': 'F5', 'F6': 'F6', 'F7': 'F7', 'F8': 'F8',
                'F9': 'F9', 'F10': 'F10', 'F11': 'F11', 'F12': 'F12',
            };
            const mappedKey = keyMap[keyVal] || keyVal;
            await page.keyboard.press(mappedKey);
            await sleep(300);
            break;
        }

        case 'scroll': {
            const { coordinate, scroll_direction, scroll_amount } = action;
            const direction = scroll_direction || action.direction || 'down';
            const amount = (scroll_amount || 3) * 120; // scroll_amount is in "clicks"
            const [x, y] = coordinate || [viewport.width / 2, viewport.height / 2];
            const isVertical = direction === 'up' || direction === 'down';
            const scrollPx = direction === 'up' || direction === 'left' ? -amount : amount;

            console.error(`[*] Scroll ${direction} ${amount}px at (${x}, ${y})`);
            await page.mouse.move(x, y);
            await page.evaluate(({ sx, sy, vert }) => {
                window.scrollBy(vert ? 0 : sx, vert ? sy : 0);
            }, { sx: scrollPx, sy: scrollPx, vert: isVertical });
            await sleep(500);
            break;
        }

        case 'mouse_move': {
            const { coordinate } = action;
            if (coordinate && coordinate.length === 2) {
                const [x, y] = coordinate;
                console.error(`[*] Mouse move to (${x}, ${y})`);
                await page.mouse.move(x, y);
                await sleep(200);
            }
            break;
        }

        case 'left_click_drag': {
            const { start_coordinate, coordinate } = action;
            if (start_coordinate && coordinate) {
                const [sx, sy] = start_coordinate;
                const [ex, ey] = coordinate;
                console.error(`[*] Drag from (${sx}, ${sy}) to (${ex}, ${ey})`);
                await page.mouse.move(sx, sy);
                await page.mouse.down();
                await page.mouse.move(ex, ey, { steps: 10 });
                await page.mouse.up();
                await sleep(500);
            }
            break;
        }

        case 'wait': {
            const waitMs = (action.duration || 2) * 1000;
            console.error(`[*] Wait ${waitMs}ms`);
            await sleep(waitMs);
            break;
        }

        case 'screenshot': {
            console.error('[*] Screenshot requested (will be taken on next loop)');
            break;
        }

        default:
            console.error(`[!] Unknown action type: ${actionType}`);
    }
}

/**
 * Call the Anthropic Messages API.
 *
 * Uses curl as a subprocess for reliable proxy support. The Anthropic Node SDK
 * doesn't handle all proxy configurations (e.g. corporate JWT-auth proxies),
 * while curl respects HTTP_PROXY/HTTPS_PROXY environment variables universally.
 */
function callAnthropicAPI(body) {
    const apiKey = getEnv('ANTHROPIC_API_KEY');
    const bodyJson = JSON.stringify(body);

    // Write body to temp file to avoid shell escaping issues with large payloads
    const tmpFile = path.join(OUTPUT_DIR, '.api_request.tmp.json');
    fs.writeFileSync(tmpFile, bodyJson);

    try {
        const result = execFileSync('curl', [
            '-s', '--connect-timeout', '30', '--max-time', '120',
            '-X', 'POST', 'https://api.anthropic.com/v1/messages',
            '-H', 'Content-Type: application/json',
            '-H', `x-api-key: ${apiKey}`,
            '-H', 'anthropic-version: 2023-06-01',
            '-H', 'anthropic-beta: computer-use-2025-01-24',
            '-d', `@${tmpFile}`,
        ], { encoding: 'utf-8', timeout: 130000, maxBuffer: 50 * 1024 * 1024 });

        return JSON.parse(result);
    } finally {
        try { fs.unlinkSync(tmpFile); } catch (e) {}
    }
}

/**
 * Run the agentic computer-use loop.
 *
 * 1. Take screenshot
 * 2. Send to Claude with prompt
 * 3. Execute any actions Claude returns
 * 4. Repeat until Claude returns text-only (no more actions) or max iterations
 */
async function runComputerUseLoop(page, cdpClient, prompt, options) {
    const {
        model,
        timeout,
        maxActions,
        viewport,
    } = options;

    const conversation = [];
    const startTime = Date.now();

    // Take initial screenshot
    console.error('[*] Taking initial screenshot...');
    const initialScreenshot = await takeScreenshot(cdpClient, viewport);

    // Save initial screenshot to output
    fs.writeFileSync(path.join(OUTPUT_DIR, 'screenshot_initial.png'),
        Buffer.from(initialScreenshot, 'base64'));

    // Build initial messages
    const messages = [
        {
            role: 'user',
            content: [
                {
                    type: 'image',
                    source: {
                        type: 'base64',
                        media_type: 'image/png',
                        data: initialScreenshot,
                    },
                },
                {
                    type: 'text',
                    text: prompt,
                },
            ],
        },
    ];

    conversation.push({ role: 'user', text: prompt });

    let actionCount = 0;

    for (let iteration = 0; iteration < maxActions; iteration++) {
        if (Date.now() - startTime > timeout * 1000) {
            console.error(`[!] Timeout reached after ${iteration} iterations`);
            conversation.push({ role: 'system', text: `Timeout after ${iteration} iterations` });
            break;
        }

        console.error(`[*] API call ${iteration + 1}/${maxActions}...`);

        let response;
        try {
            response = callAnthropicAPI({
                model,
                max_tokens: 4096,
                system: 'You are controlling a web browser via computer use. ' +
                    'You can see the current page screenshot and interact with it. ' +
                    'Execute the user\'s request by clicking, typing, scrolling, etc. ' +
                    'When you have completed the task (or determined it cannot be done), ' +
                    'respond with only a text message explaining what you did. ' +
                    'Do NOT use the computer tool once you are done.',
                tools: [
                    {
                        type: 'computer_20250124',
                        name: 'computer',
                        display_width_px: viewport.width,
                        display_height_px: viewport.height,
                        display_number: 1,
                    },
                ],
                messages,
            });
            if (response.type === 'error') {
                throw new Error(`${response.error.type}: ${response.error.message}`);
            }
        } catch (e) {
            console.error(`[!] API error: ${e.message}`);
            conversation.push({ role: 'error', text: `API error: ${e.message}` });
            break;
        }

        // Process response content
        const assistantContent = response.content;
        let hasToolUse = false;
        let textParts = [];

        for (const block of assistantContent) {
            if (block.type === 'text') {
                textParts.push(block.text);
            } else if (block.type === 'tool_use') {
                hasToolUse = true;
                actionCount++;
                const action = block.input;
                console.error(`[*] Action ${actionCount}: ${action.action || action.type || 'unknown'}`);

                // Execute the action
                try {
                    await executeAction(page, cdpClient, {
                        type: action.action || action.type,
                        ...action,
                    }, viewport);
                } catch (e) {
                    console.error(`[!] Action failed: ${e.message}`);
                }

                // Wait for page to settle after action
                await sleep(1000);

                // Take a new screenshot after the action
                const screenshot = await takeScreenshot(cdpClient, viewport);

                // Save intermediate screenshot
                fs.writeFileSync(
                    path.join(OUTPUT_DIR, `screenshot_${String(actionCount).padStart(3, '0')}.png`),
                    Buffer.from(screenshot, 'base64')
                );

                // Add assistant message and tool result to conversation
                messages.push({ role: 'assistant', content: assistantContent });
                messages.push({
                    role: 'user',
                    content: [
                        {
                            type: 'tool_result',
                            tool_use_id: block.id,
                            content: [
                                {
                                    type: 'image',
                                    source: {
                                        type: 'base64',
                                        media_type: 'image/png',
                                        data: screenshot,
                                    },
                                },
                            ],
                        },
                    ],
                });

                // Only process the first tool_use per response
                break;
            }
        }

        if (textParts.length > 0) {
            const text = textParts.join('\n');
            conversation.push({ role: 'assistant', text });
            console.error(`[*] Claude: ${text.slice(0, 200)}${text.length > 200 ? '...' : ''}`);
        }

        // If no tool use, Claude is done
        if (!hasToolUse) {
            console.error('[+] Claude finished (no more actions requested)');
            break;
        }

        // If stop_reason is end_turn with no tool use, we're done
        if (response.stop_reason === 'end_turn' && !hasToolUse) {
            break;
        }
    }

    // Take final screenshot
    const finalScreenshot = await takeScreenshot(cdpClient, viewport);
    fs.writeFileSync(path.join(OUTPUT_DIR, 'screenshot_final.png'),
        Buffer.from(finalScreenshot, 'base64'));

    return {
        success: true,
        conversation,
        actionCount,
        iterations: Math.min(actionCount + 1, maxActions),
    };
}

async function main() {
    const args = parseArgs();
    const url = args.url;

    if (!url) {
        console.error('Usage: on_Snapshot__47_claudechrome.js --url=<url>');
        process.exit(1);
    }

    const prompt = getEnv('CLAUDECHROME_PROMPT', DEFAULT_PROMPT);
    const timeout = getEnvInt('CLAUDECHROME_TIMEOUT', 120);
    const maxActions = getEnvInt('CLAUDECHROME_MAX_ACTIONS', 15);
    const modelInput = getEnv('CLAUDECHROME_MODEL', 'sonnet');
    const model = MODEL_MAP[modelInput] || modelInput;

    let browser = null;

    try {
        // Snapshot pre-existing downloads before we start
        const previousDownloads = snapshotDirFiles(DOWNLOADS_DIR);

        // Connect to the page
        const connectTimeoutMs = Math.min(timeout * 1000, getEnvInt('TIMEOUT', 30) * 1000);
        const connection = await connectToPage({
            chromeSessionDir: CHROME_SESSION_DIR,
            timeoutMs: connectTimeoutMs,
            waitForNavigationComplete: true,
            postLoadDelayMs: 200,
            puppeteer,
        });
        browser = connection.browser;
        const page = connection.page;
        const cdpClient = connection.cdpSession;

        // Set download directory
        await setBrowserDownloadBehavior({ page, downloadPath: DOWNLOADS_DIR });

        // Get viewport dimensions
        const viewport = await page.evaluate(() => ({
            width: window.innerWidth || 1280,
            height: window.innerHeight || 720,
        }));

        console.error(`[*] Viewport: ${viewport.width}x${viewport.height}`);
        console.error(`[*] Model: ${model}`);
        console.error(`[*] Running Claude on ${url}`);
        console.error(`[*] Prompt: ${prompt.slice(0, 150)}...`);

        // Run the computer-use agentic loop
        const result = await runComputerUseLoop(page, cdpClient, prompt, {
            model,
            timeout,
            maxActions,
            viewport,
        });

        browser.disconnect();
        browser = null;

        // Save conversation log
        const logPath = path.join(OUTPUT_DIR, 'conversation.json');
        fs.writeFileSync(logPath, JSON.stringify({
            url,
            prompt,
            model,
            timestamp: new Date().toISOString(),
            success: result.success,
            actionCount: result.actionCount,
            conversation: result.conversation,
        }, null, 2));
        console.error(`[+] Conversation saved to ${logPath}`);

        // Save human-readable version
        const readablePath = path.join(OUTPUT_DIR, 'conversation.txt');
        let readableText = `URL: ${url}\nModel: ${model}\nPrompt: ${prompt}\n`;
        readableText += `Timestamp: ${new Date().toISOString()}\n`;
        readableText += `Actions taken: ${result.actionCount}\n\n`;
        for (const msg of result.conversation) {
            readableText += `--- ${msg.role || 'unknown'} ---\n${msg.text}\n\n`;
        }
        fs.writeFileSync(readablePath, readableText);

        // Move any new downloads
        await sleep(2000);
        const movedFiles = await moveNewDownloads(DOWNLOADS_DIR, OUTPUT_DIR, previousDownloads);
        if (movedFiles.length > 0) {
            console.error(`[+] Moved ${movedFiles.length} download(s): ${movedFiles.join(', ')}`);
        }

        // Emit result
        const outputFiles = ['conversation.json'];
        outputFiles.push(...movedFiles);

        const outputStr = `${result.actionCount} actions, ${outputFiles.join(', ')}`;

        emitArchiveResultRecord(result.success ? 'succeeded' : 'failed', outputStr);

        process.exit(result.success ? 0 : 1);

    } catch (e) {
        if (browser) browser.disconnect();
        console.error(`ERROR: ${e.name}: ${e.message}`);
        emitArchiveResultRecord('failed', `${e.name}: ${e.message}`);
        process.exit(1);
    }
}

main().catch(e => {
    console.error(`Fatal error: ${e.message}`);
    process.exit(1);
});
