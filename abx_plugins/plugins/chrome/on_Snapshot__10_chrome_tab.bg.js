#!/usr/bin/env node
/**
 * Create a Chrome tab for this snapshot in the shared crawl Chrome session.
 *
 * Connects to the crawl-level Chrome session (from on_Crawl__90_chrome_launch.bg.js)
 * and creates a new tab. This hook does NOT launch its own Chrome instance.
 *
 * Usage: on_Snapshot__10_chrome_tab.bg.js --url=<url> --snapshot-id=<uuid> --crawl-id=<uuid>
 * Output: Creates chrome/ directory under snapshot output dir with:
 *   - cdp_url.txt: WebSocket URL for CDP connection
 *   - chrome.pid: Chrome process ID (from crawl)
 *   - target_id.txt: Target ID of this snapshot's tab
 *   - url.txt: The URL to be navigated to
 *
 * Environment variables:
 *     CRAWL_DIR: Crawl output directory (to find crawl's Chrome session)
 *     CHROME_BINARY: Path to Chromium binary (optional, for version info)
 *
 * This is a background hook that stays alive until SIGTERM so the tab
 * can be closed cleanly at the end of the snapshot run.
 */

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');
const { parseArgs } = require('../base/utils.js');
// Add NODE_MODULES_DIR to module resolution paths if set
if (process.env.NODE_MODULES_DIR) module.paths.unshift(process.env.NODE_MODULES_DIR);

const puppeteer = require('puppeteer');
const {
    getEnv,
    getEnvInt,
    openTabInChromeSession,
    readCdpUrl,
    readTargetId,
    acquireSessionLock,
    inspectChromeSessionArtifacts,
    connectToPage,
    waitForExtensionsMetadata,
    waitForCrawlChromeSession,
    closeTabInChromeSession,
} = require('./chrome_utils.js');

// Extractor metadata
const PLUGIN_NAME = 'chrome_tab';
const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((process.env.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const CHROME_SESSION_DIR = '.';

let finalStatus = 'failed';
let finalOutput = '';
let finalError = '';
let cmdVersion = '';
let finalized = false;
let targetId = null;
let keepAliveTimer = null;
const SNAPSHOT_MARKER_FILES = [
    'target_id.txt',
    'url.txt',
    'page_loaded.txt',
    'final_url.txt',
    'navigation.json',
];

function getPortFromCdpUrl(cdpUrl) {
    const match = (cdpUrl || '').match(/:(\d+)\/devtools\//);
    return match ? match[1] : '?';
}

function emitResult(statusOverride) {
    if (finalized) return;
    finalized = true;

    const status = statusOverride || finalStatus;
    const outputStr = status === 'succeeded'
        ? finalOutput
        : (finalError || finalOutput || '');

    const result = {
        type: 'ArchiveResult',
        status,
        output_str: outputStr,
    };
    if (cmdVersion) {
        result.cmd_version = cmdVersion;
    }
    console.log(JSON.stringify(result));
}

function cleanupSnapshotMarkers(reason) {
    let removed = 0;
    for (const fileName of SNAPSHOT_MARKER_FILES) {
        const filePath = path.join(OUTPUT_DIR, fileName);
        if (!fs.existsSync(filePath)) continue;
        try {
            fs.unlinkSync(filePath);
            removed += 1;
        } catch (error) {}
    }
    if (removed > 0 && reason) {
        console.log(`[*] Removed stale Chrome snapshot markers (${reason})`);
    }
}

// Cleanup handler for SIGTERM - close this snapshot's tab
async function cleanup(signal) {
    if (signal) {
        console.error(`\nReceived ${signal}, closing chrome tab...`);
    }
    const targetIdFile = path.join(OUTPUT_DIR, 'target_id.txt');
    try {
        if (keepAliveTimer) {
            clearInterval(keepAliveTimer);
            keepAliveTimer = null;
        }
        const cdpUrl = readCdpUrl(OUTPUT_DIR);
        const currentTargetId = targetId || readTargetId(OUTPUT_DIR);
        await closeTabInChromeSession({ cdpUrl, targetId: currentTargetId, puppeteer });
    } catch (e) {
        // Best effort
    }
    try {
        fs.unlinkSync(targetIdFile);
    } catch (e) {}
    targetId = null;
    emitResult(finalStatus);
    process.exit(finalStatus === 'succeeded' ? 0 : 1);
}

// Register signal handlers
process.on('SIGTERM', () => cleanup('SIGTERM'));
process.on('SIGINT', () => cleanup('SIGINT'));

async function main() {
    const args = parseArgs();
    const url = args.url;
    const snapshotId = args.snapshot_id;
    const crawlId = args.crawl_id || getEnv('CRAWL_ID', '');
    let releaseLock = null;

    if (!url || !snapshotId) {
        console.error('Usage: on_Snapshot__10_chrome_tab.bg.js --url=<url> --snapshot-id=<uuid> [--crawl-id=<uuid>]');
        process.exit(1);
    }

    let status = 'failed';
    let output = '';
    let error = '';
    let version = '';

    try {
        releaseLock = await acquireSessionLock(path.join(OUTPUT_DIR, '.target.lock'));
        // Get Chrome version
        try {
            const binary = getEnv('CHROME_BINARY', '').trim();
            if (binary) {
                version = execSync(`"${binary}" --version`, { encoding: 'utf8', timeout: 5000 }).trim().slice(0, 64);
            }
        } catch (e) {
            version = '';
        }

        const existingTargetId = readTargetId(OUTPUT_DIR);
        if (!existingTargetId) {
            cleanupSnapshotMarkers('missing target_id.txt');
        }

        // Try to use existing crawl Chrome session (wait for readiness)
        const timeoutSeconds = getEnvInt('CHROME_TAB_TIMEOUT', getEnvInt('CHROME_TIMEOUT', getEnvInt('TIMEOUT', 60)));
        const crawlSession = await waitForCrawlChromeSession(timeoutSeconds * 1000, {
            crawlBaseDir: getEnv('CRAWL_DIR', '.'),
        });
        console.log(`[*] Found existing Chrome session from crawl ${crawlId}`);

        const existingSnapshotSession = await inspectChromeSessionArtifacts(OUTPUT_DIR, { requireTargetId: true });
        if (existingSnapshotSession.hasArtifacts && !existingSnapshotSession.stale && existingSnapshotSession.state?.targetId) {
            let reusableTarget = false;
            let existingBrowser = null;
            try {
                const existingConnection = await connectToPage({
                    chromeSessionDir: OUTPUT_DIR,
                    timeoutMs: timeoutSeconds * 1000,
                    requireTargetId: true,
                    puppeteer,
                });
                existingBrowser = existingConnection.browser;
                reusableTarget = true;
            } catch (error) {
                reusableTarget = false;
            } finally {
                if (existingBrowser) {
                    try {
                        existingBrowser.disconnect();
                    } catch (error) {}
                }
            }
            if (reusableTarget) {
                const existingUrlFile = path.join(OUTPUT_DIR, 'url.txt');
                const existingUrl = fs.existsSync(existingUrlFile) ? fs.readFileSync(existingUrlFile, 'utf-8').trim() : '';
                if (existingUrl && existingUrl !== url) {
                    throw new Error(`Live snapshot target already exists for different URL (${existingUrl})`);
                }
                targetId = existingSnapshotSession.state.targetId;
                fs.writeFileSync(path.join(OUTPUT_DIR, 'cdp_url.txt'), crawlSession.cdpUrl);
                fs.writeFileSync(path.join(OUTPUT_DIR, 'chrome.pid'), String(crawlSession.pid));
                fs.writeFileSync(existingUrlFile, url);
                status = 'succeeded';
                output = `target=${targetId} port=${getPortFromCdpUrl(crawlSession.cdpUrl)}`;
                finalStatus = status;
                finalOutput = output;
                finalError = '';
                cmdVersion = version || '';
                releaseLock();
                releaseLock = null;
                console.log('[*] Reusing existing live snapshot tab');
                console.log(JSON.stringify({
                    type: 'ArchiveResult',
                    status,
                    output_str: output,
                }));
                console.log('[*] Chrome tab created, waiting for cleanup signal...');
                keepAliveTimer = setInterval(() => {}, 1000);
                await new Promise(() => {});
            }
            cleanupSnapshotMarkers(`discarded dead target ${existingSnapshotSession.state.targetId}`);
        }

        if (existingTargetId) {
            try {
                await closeTabInChromeSession({
                    cdpUrl: crawlSession.cdpUrl,
                    targetId: existingTargetId,
                    puppeteer,
                });
                cleanupSnapshotMarkers(`replaced stale target ${existingTargetId}`);
            } catch (error) {
                cleanupSnapshotMarkers(`failed to reuse target ${existingTargetId}`);
            }
        }

        const opened = await openTabInChromeSession({
            cdpUrl: crawlSession.cdpUrl,
            puppeteer,
        });
        targetId = opened.targetId;
        if (!targetId) {
            throw new Error('Failed to resolve target ID for new tab');
        }

        fs.writeFileSync(path.join(OUTPUT_DIR, 'cdp_url.txt'), crawlSession.cdpUrl);
        fs.writeFileSync(path.join(OUTPUT_DIR, 'chrome.pid'), String(crawlSession.pid));
        fs.writeFileSync(path.join(OUTPUT_DIR, 'target_id.txt'), targetId);
        fs.writeFileSync(path.join(OUTPUT_DIR, 'url.txt'), url);

        // Mark success immediately after tab creation so SIGTERM cleanup exits 0.
        status = 'succeeded';
        output = `target=${targetId} port=${getPortFromCdpUrl(crawlSession.cdpUrl)}`;
        finalStatus = status;
        finalOutput = output;
        finalError = '';
        cmdVersion = version || '';

        try {
            const extensionsMetadata = await waitForExtensionsMetadata(crawlSession.crawlChromeDir, 10000);
            fs.writeFileSync(
                path.join(OUTPUT_DIR, 'extensions.json'),
                JSON.stringify(extensionsMetadata, null, 2)
            );
        } catch (err) {
            // Extension metadata is optional for non-extension snapshots.
        }
        console.log(`[+] Chrome tab ready`);
        console.log(`[+] CDP URL: ${crawlSession.cdpUrl}`);
        console.log(`[+] Page target ID: ${targetId}`);
        console.log(JSON.stringify({
            type: 'ArchiveResult',
            status,
            output_str: output,
        }));
        releaseLock();
        releaseLock = null;
    } catch (e) {
        error = `${e.name}: ${e.message}`;
        status = 'failed';
    }

    if (releaseLock) {
        releaseLock();
    }

    if (error) {
        console.error(`ERROR: ${error}`);
    }

    finalStatus = status;
    finalOutput = output || '';
    finalError = error || '';
    cmdVersion = version || '';

    if (status !== 'succeeded') {
        emitResult(status);
        process.exit(1);
    }

    console.log('[*] Chrome tab created, waiting for cleanup signal...');
    keepAliveTimer = setInterval(() => {}, 1000);
    await new Promise(() => {}); // Keep alive until SIGTERM
}

main().catch(e => {
    console.error(`Fatal error: ${e.message}`);
    process.exit(1);
});
