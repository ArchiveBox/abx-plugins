#!/usr/bin/env node
/**
 * Write low-resolution Chrome screencast JPEGs for the admin live progress UI.
 *
 * Frames are cache-only and intentionally not stored as snapshot artifacts.
 */

const fs = require('fs');
const path = require('path');

const {
    ensureNodeModuleResolution,
    getEnvBool,
    getEnvInt,
    loadConfig,
    parseArgs,
    emitArchiveResultRecord,
} = require('../base/utils.js');
ensureNodeModuleResolution(module);

const {
    connectToPage,
    resolvePuppeteerModule,
} = require('../chrome/chrome_utils.js');
const puppeteer = resolvePuppeteerModule();

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const DATA_DIR = path.resolve((hookConfig.DATA_DIR || '').trim() || '.');
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
const SNAPSHOT_ID = path.basename(SNAP_DIR);
const CHROME_SESSION_DIR = path.join(SNAP_DIR, 'chrome');
const LIVE_DIR = path.join(DATA_DIR, 'cache', 'chrome_screencast', SNAPSHOT_ID);
const LATEST_FRAME = path.join(LIVE_DIR, 'latest.jpg');
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

let browser = null;
let cdpSession = null;
let shuttingDown = false;
let frameCount = 0;
let lastWriteAt = 0;
let nextFrameNumber = 1;

function writeFrameAtomic(filePath, data) {
    const tmpPath = path.join(path.dirname(filePath), `.${path.basename(filePath)}.${process.pid}.tmp`);
    fs.writeFileSync(tmpPath, data);
    fs.renameSync(tmpPath, filePath);
}

function cleanupOldFrames(maxFrames) {
    if (maxFrames <= 0 || frameCount % 5 !== 0) return;
    let frames = [];
    try {
        frames = fs.readdirSync(LIVE_DIR)
            .filter(name => /^frame-\d+\.jpg$/.test(name))
            .sort();
    } catch (error) {
        return;
    }
    for (const name of frames.slice(0, Math.max(0, frames.length - maxFrames))) {
        try { fs.unlinkSync(path.join(LIVE_DIR, name)); } catch (error) {}
    }
}

async function startScreencast() {
    if (!getEnvBool('CHROME_SCREENCAST_ENABLED', true)) {
        emitArchiveResultRecord('skipped', 'CHROME_SCREENCAST_ENABLED=False');
        process.exit(0);
    }
    if (!hookConfig.DATA_DIR) {
        emitArchiveResultRecord('skipped', 'DATA_DIR is not set');
        process.exit(0);
    }

    fs.mkdirSync(LIVE_DIR, { recursive: true });
    try { fs.unlinkSync(LATEST_FRAME); } catch (error) {}

    const timeoutMs = getEnvInt('CHROME_TIMEOUT', getEnvInt('TIMEOUT', 60)) * 1000;
    const connection = await connectToPage({
        chromeSessionDir: CHROME_SESSION_DIR,
        timeoutMs,
        puppeteer,
    });
    browser = connection.browser;
    cdpSession = connection.cdpSession || await connection.page.target().createCDPSession();

    const width = getEnvInt('CHROME_SCREENCAST_WIDTH', 320);
    const height = getEnvInt('CHROME_SCREENCAST_HEIGHT', 180);
    const quality = Math.max(1, Math.min(100, getEnvInt('CHROME_SCREENCAST_QUALITY', 35)));
    const fps = Math.max(1, Math.min(5, getEnvInt('CHROME_SCREENCAST_FPS', 1)));
    const bufferSize = Math.max(1, Math.min(120, getEnvInt('CHROME_SCREENCAST_BUFFER', 20)));
    const minFrameMs = Math.floor(1000 / fps);

    cdpSession.on('Page.screencastFrame', async ({ data, sessionId }) => {
        if (shuttingDown) return;
        try {
            await cdpSession.send('Page.screencastFrameAck', { sessionId });
        } catch (error) {}
        const now = Date.now();
        if (now - lastWriteAt < minFrameMs) return;
        lastWriteAt = now;
        const jpeg = Buffer.from(data, 'base64');
        const framePath = path.join(LIVE_DIR, `frame-${String(nextFrameNumber).padStart(6, '0')}.jpg`);
        nextFrameNumber += 1;
        frameCount += 1;
        try {
            writeFrameAtomic(framePath, jpeg);
            writeFrameAtomic(LATEST_FRAME, jpeg);
            cleanupOldFrames(bufferSize);
        } catch (error) {
            console.error(`WARN: failed to write screencast frame: ${error.message}`);
        }
    });

    await cdpSession.send('Page.startScreencast', {
        format: 'jpeg',
        quality,
        maxWidth: width,
        maxHeight: height,
        everyNthFrame: 1,
    });
    console.log(`screencast frames: ${LIVE_DIR}`);
}

async function stopScreencast(status = 'succeeded', output = '') {
    if (shuttingDown) return;
    shuttingDown = true;
    if (cdpSession) {
        try { await cdpSession.send('Page.stopScreencast'); } catch (error) {}
        try { cdpSession.removeAllListeners('Page.screencastFrame'); } catch (error) {}
        try { cdpSession.detach(); } catch (error) {}
        cdpSession = null;
    }
    if (browser) {
        try { browser.disconnect(); } catch (error) {}
        browser = null;
    }
    emitArchiveResultRecord(status, output || `${frameCount} screencast frames`);
    try { fs.rmSync(LIVE_DIR, { recursive: true, force: true }); } catch (error) {}
}

async function handleShutdown(signal) {
    console.error(`\nReceived ${signal}, stopping screencast...`);
    await stopScreencast();
    process.exit(0);
}

async function main() {
    const args = parseArgs();
    if (!args.url) {
        console.error('Usage: on_Snapshot__12_chrome_screencast.daemon.bg.js --url=<url>');
        process.exit(1);
    }
    process.on('SIGTERM', () => handleShutdown('SIGTERM'));
    process.on('SIGINT', () => handleShutdown('SIGINT'));

    try {
        await startScreencast();
        await new Promise(() => {});
    } catch (error) {
        const message = `${error.name}: ${error.message}`;
        console.error(`ERROR: ${message}`);
        await stopScreencast('failed', message);
        process.exit(1);
    }
}

main().catch(async (error) => {
    const message = `${error.name}: ${error.message}`;
    console.error(`Fatal error: ${message}`);
    await stopScreencast('failed', message);
    process.exit(1);
});
