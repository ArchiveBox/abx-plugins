#!/usr/bin/env node
/**
 * Save a page using the SingleFile Chrome extension via an existing Chrome session.
 *
 * Usage: singlefile_extension_save.js --url=<url>
 * Output: prints saved file path on success
 */

const fs = require('fs');
const path = require('path');
const os = require('os');
const { parseArgs } = require('../base/utils.js');

// Match the rest of the JS hook lifecycle: ArchiveBox resolves provider-owned
// node_modules once and passes NODE_MODULES_DIR to hook subprocesses. Helper
// scripts launched from Python must honor the same lookup path or they will
// fail to resolve shared dependencies like puppeteer-core even when the parent
// hook already has them available.
if (process.env.NODE_MODULES_DIR) {
    module.paths.unshift(process.env.NODE_MODULES_DIR);
}

const SNAPSHOT_OUTPUT_DIR = process.cwd();
const CHROME_SESSION_DIR = path.resolve(SNAPSHOT_OUTPUT_DIR, '..', 'chrome');
const DOWNLOADS_DIR = process.env.CHROME_DOWNLOADS_DIR ||
    path.join(process.env.PERSONAS_DIR || path.join(os.homedir(), '.config', 'abx', 'personas'),
        process.env.ACTIVE_PERSONA || 'Default',
        'chrome_downloads');

process.env.CHROME_DOWNLOADS_DIR = DOWNLOADS_DIR;

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
            // Fallback for newer protocol versions
            await client.send('Browser.setDownloadBehavior', {
                behavior: 'allow',
                downloadPath: downloadDir,
            });
        }
    } catch (err) {
        console.error(`[⚠️] Failed to set download directory: ${err.message || err}`);
    }
}

async function main() {
    const args = parseArgs();
    const url = args.url;
    const outputPath = args.output_path || path.join(SNAPSHOT_OUTPUT_DIR, 'singlefile.html');

    if (!url) {
        console.error('Usage: singlefile_extension_save.js --url=<url>');
        process.exit(1);
    }

    console.error(`[singlefile] helper start url=${url}`);
    console.error(`[singlefile] downloads_dir=${DOWNLOADS_DIR}`);
    if (process.env.CHROME_EXTENSIONS_DIR) {
        console.error(`[singlefile] extensions_dir=${process.env.CHROME_EXTENSIONS_DIR}`);
    }

    try {
        console.error('[singlefile] loading dependencies...');
        const puppeteer = require('puppeteer-core');
        const chromeUtils = require('../chrome/chrome_utils.js');
        const {
            EXTENSION,
            saveSinglefileWithExtension,
        } = require('./on_Crawl__82_singlefile_install.finite.bg.js');
        if (process.cwd() !== SNAPSHOT_OUTPUT_DIR) {
            process.chdir(SNAPSHOT_OUTPUT_DIR);
        }
        console.error('[singlefile] dependencies loaded');

        // Ensure extension is installed and metadata is cached
        console.error('[singlefile] ensuring extension cache...');
        const extension = await chromeUtils.installExtensionWithCache(
            EXTENSION,
            { extensionsDir: process.env.CHROME_EXTENSIONS_DIR }
        );
        if (!extension) {
            console.error('[❌] SingleFile extension not installed');
            process.exit(2);
        }
        console.error(`[singlefile] extension cache ready name=${extension.name} version=${extension.version}`);

        // Connect to existing Chrome session
        console.error('[singlefile] connecting to chrome session...');
        const { browser, page } = await chromeUtils.connectToPage({
            chromeSessionDir: CHROME_SESSION_DIR,
            timeoutMs: 60000,
            requireTargetId: true,
            puppeteer,
            puppeteerModule: puppeteer,
        });
        console.error('[singlefile] connected to chrome');

        try {
            const currentUrl = await page.url();
            const norm = (value) => (value || '').replace(/\/+$/, '');
            if (!currentUrl || currentUrl.startsWith('about:') || norm(currentUrl) !== norm(url)) {
                console.error(`[singlefile] navigating page from ${currentUrl || '<empty>'} to ${url}`);
                await page.goto(url, {
                    waitUntil: 'networkidle2',
                    timeout: 60000,
                });
            }

            // Ensure CDP target discovery is enabled so service_worker targets appear
            try {
                const client = await page.createCDPSession();
                await client.send('Target.setDiscoverTargets', { discover: true });
                await client.send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
            } catch (err) {
                console.error(`[singlefile] failed to enable target discovery: ${err.message || err}`);
            }

            // Resolve extension id from snapshot chrome session metadata and connect to target by id.
            console.error('[singlefile] waiting for extensions metadata...');
            const sessionExtensions = await chromeUtils.waitForExtensionsMetadata(CHROME_SESSION_DIR, 15000);
            const sessionEntry = chromeUtils.findExtensionMetadataByName(sessionExtensions, extension.name);
            if (!sessionEntry || !sessionEntry.id) {
                console.error(`[singlefile] extension metadata missing id for name=${extension.name}`);
                await browser.disconnect();
                process.exit(5);
            }
            extension.id = sessionEntry.id;
            console.error(`[singlefile] resolved extension id from session metadata: ${extension.id}`);

            const extensionTarget = await chromeUtils.waitForExtensionTargetHandle(browser, extension.id, 30000);
            console.error('[singlefile] loading extension from target...');
            await chromeUtils.loadExtensionFromTarget([extension], extensionTarget);
            if (typeof extension.dispatchAction !== 'function') {
                console.error(`[singlefile] extension dispatchAction missing for id=${extension.id}`);
                await browser.disconnect();
                process.exit(6);
            }
            console.error('[singlefile] setting download dir...');
            await setDownloadDir(page, DOWNLOADS_DIR);

            console.error('[singlefile] triggering save via extension...');
            const output = await saveSinglefileWithExtension(page, extension, {
                downloadsDir: DOWNLOADS_DIR,
                outputPath,
            });
            if (output && fs.existsSync(output)) {
                console.error(`[singlefile] saved: ${output}`);
                console.log(output);
                await browser.disconnect();
                process.exit(0);
            }

            console.error('[❌] SingleFile extension did not produce output');
            await browser.disconnect();
            process.exit(3);
        } catch (err) {
            await browser.disconnect();
            throw err;
        }
    } catch (err) {
        console.error(`[❌] ${err.message || err}`);
        process.exit(4);
    }
}

if (require.main === module) {
    main();
}
