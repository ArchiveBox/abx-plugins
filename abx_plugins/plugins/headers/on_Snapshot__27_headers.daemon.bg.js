#!/usr/bin/env node
/**
 * Capture original request + response headers for the main navigation.
 *
 * This hook sets up CDP listeners BEFORE chrome_navigate loads the page,
 * then waits for navigation to complete. It records the first top-level
 * request headers and the corresponding response headers (with :status).
 *
 * Usage: on_Snapshot__27_headers.daemon.bg.js --url=<url> --snapshot-id=<uuid>
 * Output: Writes headers.json
 */

const fs = require('fs');
const path = require('path');

// Import generic helpers from base/utils.js
const {
    ensureNodeModuleResolution,
    getEnvBool,
    getEnvInt,
    parseArgs,
    emitArchiveResult,
} = require('../base/utils.js');
ensureNodeModuleResolution(module);
const puppeteer = require('puppeteer-core');

// Import chrome-specific utilities from chrome_utils.js
const {
    connectToPage,
    waitForPageLoaded,
} = require('../chrome/chrome_utils.js');

const PLUGIN_NAME = 'headers';
const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((process.env.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const OUTPUT_FILE = 'headers.json';
const CHROME_SESSION_DIR = '../chrome';
const CHROME_SESSION_REQUIRED_ERROR = 'No Chrome session found (chrome plugin must run first)';
const POST_CAPTURE_NAVIGATION_GRACE_MS = 2000;

let browser = null;
let page = null;
let client = null;
let shuttingDown = false;
let headersWritten = false;

let requestId = null;
let requestUrl = null;
let requestHeaders = null;
let responseHeaders = null;
let responseStatus = null;
let responseStatusText = null;
let responseUrl = null;
let originalUrl = null;
let headersReadyResolve = null;
let headersReadyReject = null;
const headersReady = new Promise((resolve) => {
    headersReadyResolve = resolve;
}).catch((error) => {
    throw error;
});
const headersReadyFailure = new Promise((_, reject) => {
    headersReadyReject = reject;
});

function getFinalUrl() {
    const finalUrlFile = path.join(CHROME_SESSION_DIR, 'final_url.txt');
    if (fs.existsSync(finalUrlFile)) {
        return fs.readFileSync(finalUrlFile, 'utf8').trim();
    }
    return page ? page.url() : null;
}

function writeHeadersFile() {
    if (headersWritten) return;
    if (!responseHeaders) return;

    const outputPath = path.join(OUTPUT_DIR, OUTPUT_FILE);
    const responseHeadersWithStatus = {
        ...(responseHeaders || {}),
    };

    if (responseStatus !== null && responseStatus !== undefined &&
        responseHeadersWithStatus[':status'] === undefined) {
        responseHeadersWithStatus[':status'] = String(responseStatus);
    }

    const record = {
        url: requestUrl || originalUrl,
        final_url: getFinalUrl(),
        status: responseStatus !== undefined ? responseStatus : null,
        request_headers: requestHeaders || {},
        response_headers: responseHeadersWithStatus,
        headers: responseHeadersWithStatus, // backwards compatibility
    };

    if (responseStatusText) {
        record.statusText = responseStatusText;
    }
    if (responseUrl) {
        record.response_url = responseUrl;
    }

    fs.writeFileSync(outputPath, JSON.stringify(record, null, 2));
    headersWritten = true;
    if (headersReadyResolve) {
        headersReadyResolve();
    }
}

async function setupListener(url) {
    const timeout = getEnvInt('HEADERS_TIMEOUT', getEnvInt('TIMEOUT', 30)) * 1000;
    const { browser, page, cdpSession } = await connectToPage({
        chromeSessionDir: CHROME_SESSION_DIR,
        timeoutMs: timeout,
        puppeteer,
    });

    client = cdpSession;
    await client.send('Network.enable');

    client.on('Network.requestWillBeSent', (params) => {
        try {
            if (requestId && !responseHeaders && params.redirectResponse && params.requestId === requestId) {
                responseHeaders = params.redirectResponse.headers || {};
                responseStatus = params.redirectResponse.status || null;
                responseStatusText = params.redirectResponse.statusText || null;
                responseUrl = params.redirectResponse.url || null;
                writeHeadersFile();
            }

            if (requestId) return;
            if (params.type && params.type !== 'Document') return;
            if (!params.request || !params.request.url) return;
            if (!params.request.url.startsWith('http')) return;

            requestId = params.requestId;
            requestUrl = params.request.url;
            requestHeaders = params.request.headers || {};
        } catch (e) {
            // Ignore errors
        }
    });

    client.on('Network.responseReceived', (params) => {
        try {
            if (!requestId || params.requestId !== requestId || responseHeaders) return;
            const response = params.response || {};
            responseHeaders = response.headers || {};
            responseStatus = response.status || null;
            responseStatusText = response.statusText || null;
            responseUrl = response.url || null;
            writeHeadersFile();
        } catch (e) {
            // Ignore errors
        }
    });

    client.on('Network.loadingFailed', (params) => {
        try {
            if (!requestId || params.requestId !== requestId || headersWritten) return;
            const errorText = params.errorText || 'Main request failed';
            if (headersReadyReject) {
                headersReadyReject(new Error(errorText));
            }
        } catch (e) {
            // Ignore errors
        }
    });

    return { browser, page };
}

function emitResult(status = 'succeeded', outputStr = OUTPUT_FILE) {
    if (shuttingDown) return Promise.resolve();
    shuttingDown = true;

    const line = JSON.stringify({
        type: 'ArchiveResult',
        status,
        output_str: outputStr,
    }) + '\n';
    return new Promise((resolve) => {
        if (!process.stdout.write(line)) {
            process.stdout.once('drain', resolve);
        } else {
            setImmediate(resolve);
        }
    });
}

async function handleShutdown(signal) {
    console.error(`\nReceived ${signal}, emitting final results...`);
    if (!headersWritten) {
        writeHeadersFile();
    }
    if (headersWritten) {
        await emitResult('succeeded', OUTPUT_FILE);
    } else {
        await emitResult('failed', 'No headers captured');
    }

    if (browser) {
        try {
            browser.disconnect();
        } catch (e) {}
    }
    process.exit(headersWritten ? 0 : 1);
}

async function main() {
    const args = parseArgs();
    const url = args.url;
    const snapshotId = args.snapshot_id;

    if (!url || !snapshotId) {
        console.error('Usage: on_Snapshot__27_headers.daemon.bg.js --url=<url> --snapshot-id=<uuid>');
        process.exit(1);
    }

    originalUrl = url;

    if (!getEnvBool('HEADERS_ENABLED', true)) {
        console.error('Skipping (HEADERS_ENABLED=False)');
        emitArchiveResult('skipped', 'HEADERS_ENABLED=False');
        process.exit(0);
    }

    try {
        // Set up listeners BEFORE navigation
        const connection = await setupListener(url);
        browser = connection.browser;
        page = connection.page;

        // The hook only needs the top-level request/response pair. Waiting for
        // full navigation as a hard requirement keeps the daemon alive longer
        // than necessary and makes CI timing more fragile.
        const timeout = getEnvInt('HEADERS_TIMEOUT', getEnvInt('TIMEOUT', 30)) * 1000;
        await Promise.race([
            headersReady,
            headersReadyFailure,
            new Promise((_, reject) => setTimeout(() => reject(new Error('Timed out waiting for headers')), timeout * 4)),
        ]);

        // Best-effort short grace period so final_url.txt/page_loaded.txt can
        // land before we serialize output, without blocking success on them.
        try {
            await waitForPageLoaded(CHROME_SESSION_DIR, POST_CAPTURE_NAVIGATION_GRACE_MS, 200);
        } catch (e) {
            // Ignore navigation marker timeouts once headers have been captured.
        }

        if (!headersWritten) {
            throw new Error('No headers captured');
        }

        await emitResult('succeeded', OUTPUT_FILE);
        if (browser) {
            try {
                browser.disconnect();
            } catch (e) {}
        }
        process.exit(0);

    } catch (e) {
        const errorMessage = (e && e.message)
            ? `${e.name || 'Error'}: ${e.message}`
            : String(e || 'Unknown error');
        console.error(`ERROR: ${errorMessage}`);

        await emitResult('failed', errorMessage);
        process.exit(1);
    }
}

process.on('SIGINT', () => {
    handleShutdown('SIGINT');
});

process.on('SIGTERM', () => {
    handleShutdown('SIGTERM');
});

main().catch(async (e) => {
    console.error(`Fatal error: ${e.message}`);
    await emitResult('failed', `${e.name}: ${e.message}`);
    process.exit(1);
});
