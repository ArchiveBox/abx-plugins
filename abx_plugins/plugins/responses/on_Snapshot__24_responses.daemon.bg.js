#!/usr/bin/env node
/**
 * Archive all network responses during page load.
 *
 * This hook sets up CDP listeners BEFORE chrome_navigate loads the page,
 * then waits for navigation to complete. The listeners capture all network
 * responses during the navigation.
 *
 * Usage: on_Snapshot__24_responses.daemon.bg.js --url=<url>
 * Output: Creates responses/ directory with index.jsonl
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const {
    buildUniqueFilename,
    getExtensionFromMimeType,
    getExtensionFromUrl,
} = require('./filename_utils.js');

// Import generic helpers from base/utils.js
const {
    ensureNodeModuleResolution,
    getEnv,
    getEnvBool,
    getEnvInt,
    loadConfig,
    parseArgs,
    emitArchiveResultRecord,
    writeFileAtomic,
} = require('../base/utils.js');
ensureNodeModuleResolution(module);

// Import chrome-specific utilities from chrome_utils.js
const {
    connectToPage,
    resolvePuppeteerModule,
    waitForNavigationComplete,
} = require('../chrome/chrome_utils.js');
const puppeteer = resolvePuppeteerModule();

const PLUGIN_NAME = 'responses';
const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const CHROME_SESSION_DIR = '../chrome';

let browser = null;
let page = null;
let responseCount = 0;
let shuttingDown = false;
let mainOutputPath = '';
let preferredOutputPath = '';
let preferredOutputSize = -1;
let lastProgressLine = '';
let lastProgressAt = 0;
let pendingProgressTimer = null;
const PROGRESS_DEBOUNCE_MS = 3000;

// Resource types to capture (by default, capture everything)
const DEFAULT_TYPES = ['document', 'script', 'stylesheet', 'font', 'image', 'media', 'xhr', 'websocket'];

function emitProgress(line) {
    if (!line) {
        return;
    }
    if (line === lastProgressLine) {
        return;
    }
    const now = Date.now();
    const emitLine = () => {
        pendingProgressTimer = null;
        lastProgressAt = Date.now();
        lastProgressLine = line;
        console.log(line);
    };
    if (lastProgressAt === 0 || now - lastProgressAt >= PROGRESS_DEBOUNCE_MS) {
        if (pendingProgressTimer) {
            clearTimeout(pendingProgressTimer);
            pendingProgressTimer = null;
        }
        emitLine();
        return;
    }
    if (!pendingProgressTimer) {
        pendingProgressTimer = setTimeout(emitLine, PROGRESS_DEBOUNCE_MS - (now - lastProgressAt));
    }
}

function emitResponseProgress(force = false) {
    const line = `${responseCount} response${responseCount === 1 ? '' : 's'} captured`;
    if (force) {
        if (line === lastProgressLine && !pendingProgressTimer) {
            return;
        }
        if (pendingProgressTimer) {
            clearTimeout(pendingProgressTimer);
            pendingProgressTimer = null;
        }
        lastProgressAt = Date.now();
        lastProgressLine = line;
        console.log(line);
        return;
    }
    emitProgress(line);
}

async function createSymlink(target, linkPath) {
    try {
        const dir = path.dirname(linkPath);
        if (!fs.existsSync(dir)) {
            fs.mkdirSync(dir, { recursive: true });
        }

        if (fs.existsSync(linkPath)) {
            fs.unlinkSync(linkPath);
        }

        const relativePath = path.relative(dir, target);
        fs.symlinkSync(relativePath, linkPath);
    } catch (e) {
        // Ignore symlink errors
    }
}

async function setupListener() {
    const timeout = getEnvInt('RESPONSES_TIMEOUT', 30) * 1000;
    const typesStr = getEnv('RESPONSES_TYPES', DEFAULT_TYPES.join(','));
    const typesToSave = typesStr.split(',').map(t => t.trim().toLowerCase());

    // Create subdirectories
    const allDir = path.join(OUTPUT_DIR, 'all');
    if (!fs.existsSync(allDir)) {
        fs.mkdirSync(allDir, { recursive: true });
    }

    // Connect to Chrome page using shared utility
    const { browser, page } = await connectToPage({
        chromeSessionDir: CHROME_SESSION_DIR,
        timeoutMs: timeout,
        puppeteer,
    });

    // Set up response listener
    page.on('response', async (response) => {
        try {
            const request = response.request();
            const url = response.url();
            const resourceType = request.resourceType().toLowerCase();
            const method = request.method();
            const status = response.status();

            // Skip redirects and errors
            if (status >= 300 && status < 400) return;
            if (status >= 400 && status < 600) return;

            // Check if we should save this resource type
            if (typesToSave.length && !typesToSave.includes(resourceType)) {
                return;
            }

            // Get response body
            let bodyBuffer = null;
            try {
                bodyBuffer = await response.buffer();
            } catch (e) {
                return;
            }

            if (!bodyBuffer || bodyBuffer.length === 0) {
                return;
            }

            const isMainNavigationResponse = (
                request.isNavigationRequest?.() === true
                && request.frame?.() === page.mainFrame()
            );

            // Determine file extension
            const mimeType = response.headers()['content-type'] || '';
            const mimeBase = mimeType.split(';')[0].trim().toLowerCase();
            let extension = getExtensionFromMimeType(mimeType) || getExtensionFromUrl(url);

            // Create timestamp-based unique filename
            const timestamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\..+/, '');
            const uniqueFilename = buildUniqueFilename({ timestamp, method, url, extension });
            const uniquePath = path.join(allDir, uniqueFilename);

            // Save to unique file
            fs.writeFileSync(uniquePath, bodyBuffer);

            const relativeOutputPath = path.posix.join(
                PLUGIN_DIR,
                path.relative(OUTPUT_DIR, uniquePath).split(path.sep).join('/'),
            );
            const isHtmlResponse = mimeBase === 'text/html' || ['html', 'htm'].includes((extension || '').toLowerCase());
            let candidateMainOutputPath = relativeOutputPath;
            if (isHtmlResponse) {
                const bodySize = bodyBuffer.length;
                if (
                    bodySize > preferredOutputSize
                    || (bodySize === preferredOutputSize && (!preferredOutputPath || relativeOutputPath < preferredOutputPath))
                ) {
                    preferredOutputPath = relativeOutputPath;
                    preferredOutputSize = bodySize;
                }
            }

            // Create URL-organized symlink
            try {
                const urlObj = new URL(url);
                const hostname = urlObj.hostname;
                const pathname = urlObj.pathname || '/';
                const filename = path.basename(pathname) || 'index' + (extension ? '.' + extension : '');
                const dirPathRaw = path.dirname(pathname);
                const dirPath = dirPathRaw === '.' ? '' : dirPathRaw.replace(/^\/+/, '');

                const symlinkDir = path.join(OUTPUT_DIR, resourceType, hostname, dirPath);
                const symlinkPath = path.join(symlinkDir, filename);
                await createSymlink(uniquePath, symlinkPath);

                // Also create a site-style symlink without resource type for easy browsing
                const siteDir = path.join(OUTPUT_DIR, hostname, dirPath);
                const sitePath = path.join(siteDir, filename);
                await createSymlink(uniquePath, sitePath);
                candidateMainOutputPath = path.posix.join(PLUGIN_DIR, hostname, dirPath.split(path.sep).join('/'), filename);
            } catch (e) {
                // URL parsing or symlink creation failed, skip
            }

            if (isMainNavigationResponse) {
                mainOutputPath = candidateMainOutputPath;
            }

            // Calculate SHA256
            const sha256 = crypto.createHash('sha256').update(bodyBuffer).digest('hex');
            const urlSha256 = crypto.createHash('sha256').update(url).digest('hex');

            // Write to index
            const indexEntry = {
                ts: timestamp,
                method,
                url: method === 'DATA' ? url.slice(0, 128) : url,
                urlSha256,
                status,
                resourceType,
                mimeType: mimeBase,
                responseSha256: sha256,
                path: './' + path.relative(OUTPUT_DIR, uniquePath),
                extension,
            };

            fs.appendFileSync(indexPath, JSON.stringify(indexEntry) + '\n');
            responseCount += 1;
            emitResponseProgress();

        } catch (e) {
            // Ignore errors
        }
    });

    const indexPath = path.join(OUTPUT_DIR, 'index.jsonl');
    writeFileAtomic(indexPath, '');

    return { browser, page };
}

function emitResult(status = 'succeeded', outputStr = mainOutputPath || preferredOutputPath || `${responseCount} responses`) {
    if (shuttingDown) return Promise.resolve();
    shuttingDown = true;
    emitResponseProgress(true);
    emitArchiveResultRecord(status, outputStr);
    return Promise.resolve();
}

async function handleShutdown(signal) {
    console.error(`\nReceived ${signal}, emitting final results...`);
    await emitResult('succeeded');
    if (browser) {
        try {
            browser.disconnect();
        } catch (e) {}
    }
    process.exit(0);
}

async function main() {
    const args = parseArgs();
    const url = args.url;

    if (!url) {
        console.error('Usage: on_Snapshot__24_responses.daemon.bg.js --url=<url>');
        process.exit(1);
    }

    if (!getEnvBool('RESPONSES_ENABLED', true)) {
        console.error('Skipping (RESPONSES_ENABLED=False)');
        emitArchiveResultRecord('skipped', 'RESPONSES_ENABLED=False');
        process.exit(0);
    }

    try {
        // Set up listener BEFORE navigation
        const connection = await setupListener();
        browser = connection.browser;
        page = connection.page;
        emitResponseProgress(true);

        // Register signal handlers for graceful shutdown
        process.on('SIGTERM', () => handleShutdown('SIGTERM'));
        process.on('SIGINT', () => handleShutdown('SIGINT'));

        // Wait for chrome_navigate to complete (non-fatal)
        try {
            const timeout = getEnvInt('RESPONSES_TIMEOUT', 30) * 1000;
            await waitForNavigationComplete(CHROME_SESSION_DIR, timeout * 4, 1000);
            emitResponseProgress(true);
        } catch (e) {
            console.error(`WARN: ${e.message}`);
        }

        // console.error('Responses listener active, waiting for cleanup signal...');
        await new Promise(() => {}); // Keep alive until SIGTERM
        return;

    } catch (e) {
        const error = `${e.name}: ${e.message}`;
        console.error(`ERROR: ${error}`);

        await emitResult('failed', error);
        process.exit(1);
    }
}

main().catch(async (e) => {
    console.error(`Fatal error: ${e.message}`);
    await emitResult('failed', `${e.name}: ${e.message}`);
    process.exit(1);
});
