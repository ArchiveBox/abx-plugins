#!/usr/bin/env node
/**
 * Create a Chrome tab for this snapshot in the shared crawl Chrome session.
 *
 * Connects to the crawl-level Chrome session (from on_CrawlSetup__90_chrome_launch.daemon.bg.js)
 * and creates a new tab. This hook does NOT launch its own Chrome instance.
 *
 * Usage: on_Snapshot__10_chrome_tab.daemon.bg.js --url=<url>
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
const { ensureNodeModuleResolution, parseArgs, getEnv, getEnvBool, getEnvInt, loadConfig, emitArchiveResultRecord } = require('../base/utils.js');
ensureNodeModuleResolution(module);

const puppeteer = require('puppeteer');
const {
    openTabInChromeSession,
    acquireSessionLock,
    inspectChromeSessionArtifacts,
    connectToPage,
    waitForChromeSessionState,
    closeTabInChromeSession,
} = require('./chrome_utils.js');

// Extractor metadata
const PLUGIN_NAME = 'chrome_tab';
const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || '.').trim());
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
let currentCdpUrl = null;
let monitorBrowser = null;
let monitorPage = null;
let shuttingDown = false;
const SNAPSHOT_PAGE_MARKER_FILES = [
    'target_id.txt',
    'url.txt',
    'navigation.json',
];
const SNAPSHOT_ARTIFACT_FILES = [
    'cdp_url.txt',
    'chrome.pid',
    'target_id.txt',
    'url.txt',
    'navigation.json',
    'extensions.json',
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

    emitArchiveResultRecord(
        status,
        outputStr,
        cmdVersion ? { cmd_version: cmdVersion } : {},
    );
}

function publishSuccess(outputStr, versionOverride = '') {
    finalStatus = 'succeeded';
    finalOutput = outputStr || '';
    finalError = '';
    cmdVersion = versionOverride || cmdVersion || '';
    emitResult('succeeded');
}

function cleanupFiles(fileNames, reason) {
    let removed = 0;
    for (const fileName of fileNames) {
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

function cleanupSnapshotPageMarkers(reason) {
    cleanupFiles(SNAPSHOT_PAGE_MARKER_FILES, reason);
}

function cleanupSnapshotArtifacts(reason) {
    cleanupFiles(SNAPSHOT_ARTIFACT_FILES, reason);
}

async function stopTargetMonitor() {
    if (monitorPage) {
        try {
            monitorPage.removeAllListeners('close');
        } catch (error) {}
        monitorPage = null;
    }
    if (monitorBrowser) {
        try {
            await monitorBrowser.disconnect();
        } catch (error) {}
        monitorBrowser = null;
    }
}

async function startTargetMonitor() {
    await stopTargetMonitor();
    if (!currentCdpUrl || !targetId) {
        return;
    }

    const expectedTargetId = targetId;
    const connection = await connectToPage({
        chromeSessionDir: OUTPUT_DIR,
        timeoutMs: 5000,
        missingTargetGraceMs: 0,
        requireTargetId: true,
        puppeteer,
    });
    monitorBrowser = connection.browser;
    monitorPage = connection.page;
    monitorPage.once('close', async () => {
        if (shuttingDown) {
            return;
        }
        if (targetId !== expectedTargetId) {
            return;
        }
        console.error(`[*] Snapshot target ${expectedTargetId} closed unexpectedly, clearing snapshot page markers`);
        targetId = null;
        cleanupSnapshotPageMarkers(`target ${expectedTargetId} disappeared`);
        await stopTargetMonitor();
    });
}

async function startTargetMonitorBestEffort() {
    try {
        await startTargetMonitor();
    } catch (error) {
        const message = error?.message || String(error);
        console.error(`[*] Skipping target monitor setup: ${message}`);
        await stopTargetMonitor();
    }
}

// Cleanup handler for SIGTERM - close this snapshot's tab
async function cleanup(signal) {
    if (signal) {
        console.error(`\nReceived ${signal}, closing chrome tab...`);
    }
    shuttingDown = true;
    try {
        if (keepAliveTimer) {
            clearInterval(keepAliveTimer);
            keepAliveTimer = null;
        }
        await stopTargetMonitor();
        const currentSession = await waitForChromeSessionState(OUTPUT_DIR, {
            timeoutMs: 250,
        });
        const cdpUrl = currentCdpUrl || currentSession?.cdpUrl;
        const currentTargetId = targetId || currentSession?.targetId;
        await closeTabInChromeSession({ cdpUrl, targetId: currentTargetId, puppeteer });
    } catch (e) {
        // Best effort
    }
    cleanupSnapshotArtifacts('snapshot teardown');
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
    let releaseLock = null;

    if (!url) {
        console.error('Usage: on_Snapshot__10_chrome_tab.daemon.bg.js --url=<url>');
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

        const isolation = getEnv('CHROME_ISOLATION', 'crawl').toLowerCase() === 'snapshot' ? 'snapshot' : 'crawl';
        const cdpUrlOverride = getEnv('CHROME_CDP_URL', '');
        const processIsLocal = cdpUrlOverride ? false : getEnvBool('CHROME_IS_LOCAL', true);
        const timeoutSeconds = getEnvInt('CHROME_TAB_TIMEOUT', getEnvInt('CHROME_TIMEOUT', getEnvInt('TIMEOUT', 60)));
        const existingSnapshotSession = await inspectChromeSessionArtifacts(OUTPUT_DIR, {
            requireTargetId: true,
            processIsLocal,
        });
        const existingTargetId = existingSnapshotSession.state?.targetId;
        if (!existingTargetId) {
            cleanupSnapshotPageMarkers('missing target_id.txt');
        }
        if (existingSnapshotSession.hasArtifacts && !existingSnapshotSession.stale && existingSnapshotSession.state?.targetId) {
            let reusableTarget = false;
            let existingBrowser = null;
            try {
                const existingConnection = await connectToPage({
                    chromeSessionDir: OUTPUT_DIR,
                    // This is only a fast liveness probe for the currently
                    // published target. If it is already dead, this same hook
                    // invocation is responsible for replacing it, so do not
                    // tolerate any stale-target grace period here.
                    timeoutMs: Math.min(timeoutSeconds * 1000, 1000),
                    missingTargetGraceMs: 0,
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
                currentCdpUrl = existingSnapshotSession.state.cdpUrl;
                targetId = existingSnapshotSession.state.targetId;
                fs.writeFileSync(path.join(OUTPUT_DIR, 'cdp_url.txt'), currentCdpUrl);
                if (existingSnapshotSession.state.pid) {
                    fs.writeFileSync(path.join(OUTPUT_DIR, 'chrome.pid'), String(existingSnapshotSession.state.pid));
                } else {
                    try { fs.unlinkSync(path.join(OUTPUT_DIR, 'chrome.pid')); } catch (error) {}
                }
                fs.writeFileSync(existingUrlFile, url);
                status = 'succeeded';
                output = `target=${targetId} port=${getPortFromCdpUrl(currentCdpUrl)}`;
                releaseLock();
                releaseLock = null;
                console.log('[*] Reusing existing live snapshot tab');
                publishSuccess(output, version || '');
                console.log('[*] Chrome tab created, waiting for cleanup signal...');
                await startTargetMonitorBestEffort();
                keepAliveTimer = setInterval(() => {}, 1000);
                await new Promise(() => {});
            }
            cleanupSnapshotPageMarkers(`discarded dead target ${existingSnapshotSession.state.targetId}`);
        }

        if (isolation === 'snapshot') {
            const snapshotSession = await waitForChromeSessionState(OUTPUT_DIR, {
                timeoutMs: timeoutSeconds * 1000,
            });
            if (!snapshotSession?.cdpUrl) {
                throw new Error('No snapshot-scoped Chrome session found');
            }
            currentCdpUrl = snapshotSession.cdpUrl;

            const opened = await openTabInChromeSession({
                cdpUrl: currentCdpUrl,
                timeoutMs: timeoutSeconds * 1000,
                puppeteer,
            });
            targetId = opened.targetId;
            if (!targetId) {
                throw new Error('Failed to resolve target ID for snapshot-scoped tab');
            }

            fs.writeFileSync(path.join(OUTPUT_DIR, 'cdp_url.txt'), currentCdpUrl);
            if (snapshotSession.pid) {
                fs.writeFileSync(path.join(OUTPUT_DIR, 'chrome.pid'), String(snapshotSession.pid));
            } else {
                try { fs.unlinkSync(path.join(OUTPUT_DIR, 'chrome.pid')); } catch (error) {}
            }
            fs.writeFileSync(path.join(OUTPUT_DIR, 'target_id.txt'), targetId);
            fs.writeFileSync(path.join(OUTPUT_DIR, 'url.txt'), url);

            status = 'succeeded';
            output = `target=${targetId} port=${getPortFromCdpUrl(currentCdpUrl)}`;

            console.log(`[+] Chrome tab ready`);
            console.log(`[+] CDP URL: ${currentCdpUrl}`);
            console.log(`[+] Page target ID: ${targetId}`);
            releaseLock();
            releaseLock = null;
            publishSuccess(output, version || '');
            await startTargetMonitorBestEffort();
        } else {
            const crawlChromeDir = path.join(path.resolve(getEnv('CRAWL_DIR', '.')), 'chrome');
            const crawlSession = await waitForChromeSessionState(crawlChromeDir, {
                timeoutMs: timeoutSeconds * 1000,
            });
            if (!crawlSession?.cdpUrl) {
                throw new Error('No Chrome session found (chrome plugin must run first)');
            }
            console.log('[*] Found existing Chrome session');
            currentCdpUrl = crawlSession.cdpUrl;

            if (existingTargetId) {
                try {
                    await closeTabInChromeSession({
                        cdpUrl: crawlSession.cdpUrl,
                        targetId: existingTargetId,
                        puppeteer,
                    });
                    cleanupSnapshotPageMarkers(`replaced stale target ${existingTargetId}`);
                } catch (error) {
                    cleanupSnapshotPageMarkers(`failed to reuse target ${existingTargetId}`);
                }
            }

            const opened = await openTabInChromeSession({
                cdpUrl: crawlSession.cdpUrl,
                puppeteer,
                timeoutMs: timeoutSeconds * 1000,
            });
            targetId = opened.targetId;
            if (!targetId) {
                throw new Error('Failed to resolve target ID for new tab');
            }

            fs.writeFileSync(path.join(OUTPUT_DIR, 'cdp_url.txt'), crawlSession.cdpUrl);
            if (crawlSession.pid) {
                fs.writeFileSync(path.join(OUTPUT_DIR, 'chrome.pid'), String(crawlSession.pid));
            } else {
                try { fs.unlinkSync(path.join(OUTPUT_DIR, 'chrome.pid')); } catch (error) {}
            }
            fs.writeFileSync(path.join(OUTPUT_DIR, 'target_id.txt'), targetId);
            fs.writeFileSync(path.join(OUTPUT_DIR, 'url.txt'), url);

            status = 'succeeded';
            output = `target=${targetId} port=${getPortFromCdpUrl(crawlSession.cdpUrl)}`;

            console.log(`[+] Chrome tab ready`);
            console.log(`[+] CDP URL: ${crawlSession.cdpUrl}`);
            console.log(`[+] Page target ID: ${targetId}`);
            releaseLock();
            releaseLock = null;
            publishSuccess(output, version || '');

            try {
                const extensionsSession = await waitForChromeSessionState(crawlChromeDir, {
                    timeoutMs: 10000,
                    requireExtensionsLoaded: true,
                });
                const extensions = extensionsSession?.extensions;
                if (!extensions) {
                    throw new Error('Missing extensions metadata');
                }
                fs.writeFileSync(
                    path.join(OUTPUT_DIR, 'extensions.json'),
                    JSON.stringify(extensions, null, 2)
                );
            } catch (err) {}
            await startTargetMonitorBestEffort();
        }
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
