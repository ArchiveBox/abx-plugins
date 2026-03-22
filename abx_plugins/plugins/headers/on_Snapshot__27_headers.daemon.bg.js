#!/usr/bin/env node
/**
 * Capture original request + response headers for the main navigation.
 *
 * This hook sets up CDP listeners BEFORE chrome_navigate loads the page,
 * then waits for navigation to complete. It records the first top-level
 * request headers and the corresponding response headers (with :status).
 *
 * Usage: on_Snapshot__27_headers.daemon.bg.js --url=<url>
 * Output: Writes headers.json
 */

const fs = require('fs');
const path = require('path');

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
const puppeteer = require('puppeteer-core');

// Import chrome-specific utilities from chrome_utils.js
const {
    connectToPage,
    waitForNavigationComplete,
} = require('../chrome/chrome_utils.js');

const PLUGIN_NAME = 'headers';
const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const OUTPUT_FILE = 'headers.json';
const OUTPUT_PATH_STR = `${PLUGIN_DIR}/${OUTPUT_FILE}`;
const CHROME_SESSION_DIR = '../chrome';
const CHROME_SESSION_REQUIRED_ERROR = 'No Chrome session found (chrome plugin must run first)';
const POST_CAPTURE_NAVIGATION_GRACE_MS = 2000;

let browser = null;
let page = null;
let shuttingDown = false;
let headersWritten = false;

let requestUrl = null;
let requestHeaders = null;
let responseHeaders = null;
let responseStatus = null;
let responseStatusText = null;
let responseUrl = null;
let originalUrl = null;
let latestNavigationState = null;
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

function getFinalUrl(navigationState = null) {
    return navigationState?.finalUrl || page?.url() || null;
}

function isMainNavigationRequest(request) {
    try {
        if (!request) return false;
        const url = request.url();
        if (!url || !url.startsWith('http')) return false;

        const resourceType = (request.resourceType?.() || '').toLowerCase();
        if (resourceType && resourceType !== 'document') return false;

        if (request.isNavigationRequest?.() === false) return false;

        const requestFrame = request.frame?.() || null;
        if (requestFrame && page?.mainFrame && requestFrame !== page.mainFrame()) {
            return false;
        }

        return true;
    } catch (error) {
        return false;
    }
}

function writeHeadersFile(navigationState = null, forceRewrite = false) {
    if (!responseHeaders) return;
    if (headersWritten && !forceRewrite) return;

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
        final_url: getFinalUrl(navigationState),
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
    const wasWritten = headersWritten;
    headersWritten = true;
    if (!wasWritten && headersReadyResolve) {
        headersReadyResolve();
    }
}

async function setupListener(url) {
    const outputPath = path.join(OUTPUT_DIR, OUTPUT_FILE);
    const timeout = getEnvInt('HEADERS_TIMEOUT', getEnvInt('TIMEOUT', 30)) * 1000;
    try { fs.unlinkSync(outputPath); } catch (error) {}
    const { browser, page } = await connectToPage({
        chromeSessionDir: CHROME_SESSION_DIR,
        timeoutMs: timeout,
        puppeteer,
    });

    page.on('request', (request) => {
        try {
            if (!isMainNavigationRequest(request)) return;
            requestUrl = requestUrl || request.url();
            requestHeaders = request.headers() || {};
        } catch (e) {
            // Ignore errors
        }
    });

    page.on('response', (response) => {
        try {
            const request = response.request();
            if (!isMainNavigationRequest(request)) return;

            requestUrl = requestUrl || request.url();
            requestHeaders = request.headers() || {};
            responseHeaders = response.headers() || {};
            responseStatus = response.status() || null;
            responseStatusText = response.statusText ? response.statusText() : null;
            responseUrl = response.url() || null;
            writeHeadersFile(null, true);
        } catch (e) {
            // Ignore errors
        }
    });

    page.on('requestfailed', (request) => {
        try {
            if (!isMainNavigationRequest(request) || headersWritten) return;
            const errorText = request.failure()?.errorText || 'Main request failed';
            if (headersReadyReject) {
                headersReadyReject(new Error(errorText));
            }
        } catch (e) {
            // Ignore errors
        }
    });

    // Create the output file only after listeners are attached so callers can
    // use its existence as a readiness signal before triggering navigation.
    fs.closeSync(fs.openSync(outputPath, 'a'));

    return { browser, page };
}

function emitResult(status = 'succeeded', outputStr = OUTPUT_PATH_STR) {
    if (shuttingDown) return Promise.resolve();
    shuttingDown = true;
    emitArchiveResultRecord(status, outputStr);
    return Promise.resolve();
}

async function handleShutdown(signal) {
    console.error(`\nReceived ${signal}, emitting final results...`);
    if (!headersWritten || latestNavigationState) {
        writeHeadersFile(latestNavigationState, true);
    }
    if (headersWritten) {
        await emitResult('succeeded', OUTPUT_PATH_STR);
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

    if (!url) {
        console.error('Usage: on_Snapshot__27_headers.daemon.bg.js --url=<url>');
        process.exit(1);
    }

    originalUrl = url;

    if (!getEnvBool('HEADERS_ENABLED', true)) {
        console.error('Skipping (HEADERS_ENABLED=False)');
        emitArchiveResultRecord('skipped', 'HEADERS_ENABLED=False');
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

        // Best-effort short grace period so navigation.json can land before we
        // serialize output, without blocking success on it.
        let navigationState = null;
        try {
            navigationState = await waitForNavigationComplete(CHROME_SESSION_DIR, POST_CAPTURE_NAVIGATION_GRACE_MS, 200);
            latestNavigationState = navigationState;
        } catch (e) {
            // Ignore navigation marker timeouts once headers have been captured.
        }

        writeHeadersFile(navigationState, true);
        if (!headersWritten) {
            throw new Error('No headers captured');
        }

        await emitResult('succeeded', OUTPUT_PATH_STR);
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
