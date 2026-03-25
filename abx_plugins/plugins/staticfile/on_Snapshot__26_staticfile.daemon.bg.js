#!/usr/bin/env node
/**
 * Detect static-file main responses using CDP during initial request.
 *
 * This hook sets up CDP listeners BEFORE chrome_navigate to capture the
 * Content-Type from the initial response. If it's a static file (PDF, image, etc.),
 * it prefers the artifact saved by the responses hook and only falls back to
 * saving its own copy when that artifact is unavailable.
 *
 * Usage: on_Snapshot__26_staticfile.daemon.bg.js --url=<url>
 * Output: Emits the saved main-response path
 */

const fs = require('fs');
const path = require('path');
const {
    getExtensionFromMimeType,
    getExtensionFromUrl,
} = require('../responses/filename_utils.js');

// Import generic helpers from base/utils.js
const {
    ensureNodeModuleResolution,
    getEnvBool,
    getEnvInt,
    loadConfig,
    parseArgs,
    emitArchiveResultRecord,
} = require('../base/utils.js');
ensureNodeModuleResolution(module);

// Import chrome-specific utilities from chrome_utils.js
const { connectToPage, resolvePuppeteerModule } = require('../chrome/chrome_utils.js');
const puppeteer = resolvePuppeteerModule();

const PLUGIN_NAME = 'staticfile';
const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
const RESPONSES_INDEX_PATH = path.join(SNAP_DIR, 'responses', 'index.jsonl');
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const CHROME_SESSION_DIR = '../chrome';

// Content-Types that indicate static files
const STATIC_CONTENT_TYPES = new Set([
    // Documents
    'application/pdf',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-powerpoint',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'application/rtf',
    'application/epub+zip',
    // Images
    'image/png',
    'image/jpeg',
    'image/gif',
    'image/webp',
    'image/svg+xml',
    'image/x-icon',
    'image/bmp',
    'image/tiff',
    'image/avif',
    'image/heic',
    'image/heif',
    // Audio
    'audio/mpeg',
    'audio/mp3',
    'audio/wav',
    'audio/flac',
    'audio/aac',
    'audio/ogg',
    'audio/webm',
    'audio/m4a',
    'audio/opus',
    // Video
    'video/mp4',
    'video/webm',
    'video/x-matroska',
    'video/avi',
    'video/quicktime',
    'video/x-ms-wmv',
    'video/x-flv',
    // Archives
    'application/zip',
    'application/x-tar',
    'application/gzip',
    'application/x-bzip2',
    'application/x-xz',
    'application/x-7z-compressed',
    'application/x-rar-compressed',
    'application/vnd.rar',
    // Data
    'application/json',
    'application/xml',
    'text/csv',
    'text/xml',
    'application/x-yaml',
    // Executables/Binaries
    'application/octet-stream',
    'application/x-executable',
    'application/x-msdos-program',
    'application/x-apple-diskimage',
    'application/vnd.debian.binary-package',
    'application/x-rpm',
    // Other
    'application/x-bittorrent',
    'application/wasm',
]);

const STATIC_CONTENT_TYPE_PREFIXES = [
    'image/',
    'audio/',
    'video/',
    'application/zip',
    'application/x-',
];

// Global state
let originalUrl = '';
let detectedContentType = null;
let isStaticFile = false;
let savedOutputPath = null;
let downloadError = null;
let page = null;
let browser = null;
let finalized = false;

function isStaticContentType(contentType) {
    if (!contentType) return false;

    const ct = contentType.split(';')[0].trim().toLowerCase();

    // Check exact match
    if (STATIC_CONTENT_TYPES.has(ct)) return true;

    // Check prefixes
    for (const prefix of STATIC_CONTENT_TYPE_PREFIXES) {
        if (ct.startsWith(prefix)) return true;
    }

    return false;
}

function sanitizeFilename(str, maxLen = 200) {
    return str
        .replace(/[^a-zA-Z0-9._-]/g, '_')
        .slice(0, maxLen);
}

function getFilenameFromUrl(url) {
    try {
        const pathname = new URL(url).pathname;
        const filename = path.basename(pathname) || 'downloaded_file';
        return sanitizeFilename(filename);
    } catch (e) {
        return 'downloaded_file';
    }
}

function normalizeUrl(url) {
    try {
        const parsed = new URL(url);
        let path = parsed.pathname || '';
        if (path === '/') path = '';
        return `${parsed.origin}${path}`;
    } catch (e) {
        return url;
    }
}

function isTopLevelNavigationRequest(request) {
    try {
        if (!request || request.isNavigationRequest?.() !== true) return false;
        const url = request.url?.() || '';
        if (!url.startsWith('http')) return false;
        const frame = request.frame?.() || null;
        return !frame || frame.parentFrame?.() === null || frame === page?.mainFrame?.();
    } catch (error) {
        return false;
    }
}

function getOutputPathRelativeToSnapshot(filePath) {
    if (!filePath) return null;
    return path.posix.join(PLUGIN_DIR, String(filePath).split(path.sep).join('/'));
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function getResponsesOutputInfo(url, mimeType) {
    try {
        const urlObj = new URL(url);
        const hostname = urlObj.hostname;
        const pathname = urlObj.pathname || '/';
        const extension = getExtensionFromMimeType(mimeType) || getExtensionFromUrl(url);
        const filename = path.basename(pathname) || `index${extension ? `.${extension}` : ''}`;
        const dirPathRaw = path.dirname(pathname);
        const dirPath = dirPathRaw === '.' ? '' : dirPathRaw.replace(/^\/+/, '');
        const relativePath = path.posix.join(
            'responses',
            hostname,
            dirPath.split(path.sep).join('/'),
            filename,
        );
        return {
            relativePath,
            absolutePath: path.join(SNAP_DIR, ...relativePath.split('/')),
        };
    } catch (error) {
        return null;
    }
}

function isSavedResponseReady(filePath) {
    if (!filePath || !fs.existsSync(filePath)) return false;

    try {
        const stats = fs.statSync(filePath);
        return stats.isFile() && stats.size > 0;
    } catch (error) {
        return false;
    }
}

async function waitForResponsesOutput(outputInfo, timeoutMs) {
    if (!outputInfo) return null;

    const startupDeadline = Date.now() + Math.min(timeoutMs, 2000);
    while (Date.now() < startupDeadline) {
        if (fs.existsSync(RESPONSES_INDEX_PATH)) break;
        await sleep(100);
    }

    if (!fs.existsSync(RESPONSES_INDEX_PATH)) {
        return null;
    }

    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        if (isSavedResponseReady(outputInfo.absolutePath)) {
            return outputInfo.relativePath;
        }
        await sleep(100);
    }

    if (isSavedResponseReady(outputInfo.absolutePath)) {
        return outputInfo.relativePath;
    }

    return null;
}

function buildArchiveResult() {
    const outputMimeType = detectedContentType || 'unknown';

    if (!detectedContentType) {
        return {
            type: 'ArchiveResult',
            status: 'failed',
            output_str: 'No main response captured',
            plugin: PLUGIN_NAME,
        };
    }

    if (!isStaticFile) {
        return {
            type: 'ArchiveResult',
            status: 'noresults',
            output_str: detectedContentType.startsWith('text/html')
                ? 'Page is HTML (not staticfile)'
                : outputMimeType,
            plugin: PLUGIN_NAME,
            content_type: detectedContentType,
        };
    }

    if (downloadError) {
        return {
            type: 'ArchiveResult',
            status: 'failed',
            output_str: outputMimeType,
            plugin: PLUGIN_NAME,
            content_type: detectedContentType,
        };
    }

    if (savedOutputPath) {
        return {
            type: 'ArchiveResult',
            status: 'succeeded',
            output_str: savedOutputPath,
            plugin: PLUGIN_NAME,
            content_type: detectedContentType,
        };
    }

    return {
        type: 'ArchiveResult',
        status: 'failed',
        output_str: outputMimeType,
        plugin: PLUGIN_NAME,
        content_type: detectedContentType,
    };
}

async function setupStaticFileListener() {
    const timeout = getEnvInt('STATICFILE_TIMEOUT', 30) * 1000;

    // Connect to Chrome page using shared utility
    const connection = await connectToPage({
        chromeSessionDir: CHROME_SESSION_DIR,
        timeoutMs: timeout,
        puppeteer,
    });
    browser = connection.browser;
    page = connection.page;

    let resolveMainResponse;
    let rejectMainResponse;
    const mainResponseHandled = new Promise((resolve, reject) => {
        resolveMainResponse = resolve;
        rejectMainResponse = reject;
    });

    const failTimer = setTimeout(() => {
        rejectMainResponse(new Error(`Timed out waiting for main response after ${timeout * 4 / 1000} seconds`));
    }, timeout * 4);

    const finish = () => {
        clearTimeout(failTimer);
        resolveMainResponse(buildArchiveResult());
    };

    let firstResponseHandled = false;

    page.on('response', async (response) => {
        if (firstResponseHandled) return;

        try {
            const request = response.request();
            const url = response.url();
            const headers = response.headers();
            const contentType = headers['content-type'] || '';
            const status = response.status();

            // Only process the main document response
            if (status < 200 || status >= 300) return;
            if (!isTopLevelNavigationRequest(request)) return;

            firstResponseHandled = true;
            detectedContentType = contentType.split(';')[0].trim();

            console.error(`Detected Content-Type: ${detectedContentType}`);

            // Check if it's a static file
            if (!isStaticContentType(detectedContentType)) {
                console.error('Not a static file, skipping download');
                finish();
                return;
            }

            isStaticFile = true;
            console.error('Static file detected, waiting for saved output...');

            const responsesEnabled = getEnvBool('RESPONSES_ENABLED', true);
            if (responsesEnabled) {
                const responsesOutputInfo = getResponsesOutputInfo(url, detectedContentType);
                const waitedOutputPath = await waitForResponsesOutput(responsesOutputInfo, timeout);
                if (waitedOutputPath) {
                    savedOutputPath = waitedOutputPath;
                    console.error(`Using responses output: ${savedOutputPath}`);
                    finish();
                    return;
                }
                console.error('Responses output unavailable in time, falling back to staticfile save');
            } else {
                console.error('RESPONSES_ENABLED=False, falling back to staticfile save');
            }

            console.error('Saving static file fallback locally...');

            // Download the file
            const maxSize = getEnvInt('STATICFILE_MAX_SIZE', 1024 * 1024 * 1024); // 1GB default
            const buffer = await response.buffer();

            if (buffer.length > maxSize) {
                downloadError = `File too large: ${buffer.length} bytes > ${maxSize} max`;
                finish();
                return;
            }

            // Determine filename
            let filename = getFilenameFromUrl(url);

            // Check content-disposition header for better filename
            const contentDisp = headers['content-disposition'] || '';
            if (contentDisp.includes('filename=')) {
                const match = contentDisp.match(/filename[*]?=["']?([^"';\n]+)/);
                if (match) {
                    filename = sanitizeFilename(match[1].trim());
                }
            }

            const outputPath = path.join(OUTPUT_DIR, filename);
            fs.writeFileSync(outputPath, buffer);

            savedOutputPath = getOutputPathRelativeToSnapshot(filename);
            console.error(`Static file downloaded (${buffer.length} bytes): ${filename}`);
            finish();

        } catch (e) {
            downloadError = `${e.name}: ${e.message}`;
            console.error(`Error downloading static file: ${downloadError}`);
            firstResponseHandled = true;
            finish();
        }
    });

    page.on('requestfailed', (request) => {
        if (firstResponseHandled) return;
        try {
            if (!isTopLevelNavigationRequest(request)) return;
            firstResponseHandled = true;
            const failure = request.failure();
            downloadError = failure ? failure.errorText : 'Request failed';
            rejectMainResponse(new Error(downloadError));
        } catch (e) {
            rejectMainResponse(e);
        }
    });

    return { browser, page, mainResponseHandled };
}

function emitResult(result) {
    emitArchiveResultRecord(
        result.status,
        result.output_str,
        {
            plugin: result.plugin,
            content_type: result.content_type,
        },
    );
    return Promise.resolve();
}

async function handleShutdown(signal) {
    console.error(`\nReceived ${signal}, emitting final results...`);
    if (finalized) {
        process.exit(0);
    }
    finalized = true;
    await emitResult(buildArchiveResult());
    process.exit(0);
}

async function main() {
    const args = parseArgs();
    const url = args.url;

    if (!url) {
        console.error('Usage: on_Snapshot__26_staticfile.daemon.bg.js --url=<url>');
        process.exit(1);
    }

    originalUrl = url;
    console.log('waiting for initial response...');

    if (!getEnvBool('STATICFILE_ENABLED', true)) {
        console.error('Skipping (STATICFILE_ENABLED=False)');
        emitArchiveResultRecord('skipped', 'STATICFILE_ENABLED=False');
        process.exit(0);
    }

    const timeout = getEnvInt('STATICFILE_TIMEOUT', 30) * 1000;

    // Register signal handlers for graceful shutdown
    process.on('SIGTERM', () => handleShutdown('SIGTERM'));
    process.on('SIGINT', () => handleShutdown('SIGINT'));

    try {
        // Set up static file listener BEFORE navigation and finish on the
        // first successful main-document response.
        const connection = await setupStaticFileListener();
        const result = await connection.mainResponseHandled;
        finalized = true;
        await emitResult(result);
        if (browser) {
            try {
                browser.disconnect();
            } catch (e) {}
        }
        process.exit(result.status === 'failed' ? 1 : 0);

    } catch (e) {
        const error = `${e.name}: ${e.message}`;
        console.error(`ERROR: ${error}`);

        await emitResult({
            type: 'ArchiveResult',
            status: 'failed',
            output_str: error,
        });
        process.exit(1);
    }
}

main().catch(async (e) => {
    console.error(`Fatal error: ${e.message}`);
    await emitResult({
        type: 'ArchiveResult',
        status: 'failed',
        output_str: `${e.name}: ${e.message}`,
    });
    process.exit(1);
});
