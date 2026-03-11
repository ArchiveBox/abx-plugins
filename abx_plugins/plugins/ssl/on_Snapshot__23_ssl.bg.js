#!/usr/bin/env node
/**
 * Extract SSL/TLS certificate details from a URL.
 *
 * This hook sets up CDP listeners BEFORE chrome_navigate loads the page,
 * then waits for navigation to complete. The listener captures SSL details
 * during the navigation request.
 *
 * Usage: on_Snapshot__23_ssl.js --url=<url> --snapshot-id=<uuid>
 * Output: Writes ssl.jsonl
 */

const fs = require('fs');
const path = require('path');

// Add NODE_MODULES_DIR to module resolution paths if set
if (process.env.NODE_MODULES_DIR) module.paths.unshift(process.env.NODE_MODULES_DIR);

const puppeteer = require('puppeteer-core');

// Import shared utilities from chrome_utils.js
const {
    getEnvBool,
    getEnvInt,
    parseArgs,
    connectToPage,
    waitForPageLoaded,
} = require('../chrome/chrome_utils.js');

const PLUGIN_NAME = 'ssl';
const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((process.env.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const OUTPUT_FILE = 'ssl.jsonl';
const CHROME_SESSION_DIR = '../chrome';

let browser = null;
let page = null;
let client = null;
let sslCaptured = false;
let shuttingDown = false;
let sslIssuer = null;

function truncateIssuerName(value, maxLen = 40) {
    const text = String(value || '').trim();
    if (!text) return 'unknown issuer';
    if (text.length <= maxLen) return text;
    return `${text.slice(0, maxLen - 3)}...`;
}

async function setupListener(url) {
    const outputPath = path.join(OUTPUT_DIR, OUTPUT_FILE);
    const timeout = getEnvInt('SSL_TIMEOUT', 30) * 1000;
    let targetHost = null;

    // Only extract SSL for HTTPS URLs
    if (!url.startsWith('https://')) {
        throw new Error('URL is not HTTPS');
    }

    try {
        targetHost = new URL(url).host;
    } catch (e) {
        targetHost = null;
    }

    // Connect to Chrome page using shared utility
    const { browser, page } = await connectToPage({
        chromeSessionDir: CHROME_SESSION_DIR,
        timeoutMs: timeout,
        puppeteer,
    });

    client = await page.target().createCDPSession();
    await client.send('Network.enable');

    client.on('Network.responseReceived', (params) => {
        try {
            if (sslCaptured) return;
            if (params.type && params.type !== 'Document') return;
            const response = params.response || {};
            const responseUrl = response.url || '';
            if (!responseUrl.startsWith('http')) return;

            if (targetHost) {
                try {
                    const responseHost = new URL(responseUrl).host;
                    if (responseHost !== targetHost) return;
                } catch (e) {
                    // Ignore URL parse errors, fall through
                }
            }

            const securityDetails = response.securityDetails || null;
            let sslInfo = { url: responseUrl };

            if (securityDetails) {
                sslInfo.protocol = securityDetails.protocol;
                sslInfo.subjectName = securityDetails.subjectName;
                sslInfo.issuer = securityDetails.issuer;
                sslIssuer = securityDetails.issuer || securityDetails.subjectName || null;
                sslInfo.validFrom = securityDetails.validFrom;
                sslInfo.validTo = securityDetails.validTo;
                sslInfo.certificateId = securityDetails.subjectName;
                sslInfo.securityState = response.securityState || 'secure';
                sslInfo.schemeIsCryptographic = true;

                const sanList = securityDetails.sanList;
                if (sanList && sanList.length > 0) {
                    sslInfo.subjectAlternativeNames = sanList;
                }
            } else if (responseUrl.startsWith('https://')) {
                sslInfo.securityState = response.securityState || 'unknown';
                sslInfo.schemeIsCryptographic = true;
                sslInfo.error = 'No security details available';
            } else {
                sslInfo.securityState = 'insecure';
                sslInfo.schemeIsCryptographic = false;
            }

            fs.writeFileSync(outputPath, JSON.stringify(sslInfo, null, 2));
            sslCaptured = true;
        } catch (e) {
            // Ignore errors
        }
    });

    return { browser, page };
}

function emitResult(status = 'succeeded', outputStr = truncateIssuerName(sslIssuer)) {
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
    const snapshotId = args.snapshot_id;

    if (!url || !snapshotId) {
        console.error('Usage: on_Snapshot__23_ssl.js --url=<url> --snapshot-id=<uuid>');
        process.exit(1);
    }

    if (!getEnvBool('SSL_ENABLED', true)) {
        console.error('Skipping (SSL_ENABLED=False)');
        console.log(JSON.stringify({type: 'ArchiveResult', status: 'skipped', output_str: 'SSL_ENABLED=False'}));
        process.exit(0);
    }

    try {
        // Set up listener BEFORE navigation
        const connection = await setupListener(url);
        browser = connection.browser;
        page = connection.page;

        // Register signal handlers for graceful shutdown
        process.on('SIGTERM', () => handleShutdown('SIGTERM'));
        process.on('SIGINT', () => handleShutdown('SIGINT'));

        // Wait for chrome_navigate to complete (non-fatal)
        try {
            const timeout = getEnvInt('SSL_TIMEOUT', 30) * 1000;
            await waitForPageLoaded(CHROME_SESSION_DIR, timeout * 4);
        } catch (e) {
            console.error(`WARN: ${e.message}`);
        }

        // console.error('SSL listener active, waiting for cleanup signal...');
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
