#!/usr/bin/env node
/**
 * Save a page using the SingleFile Chrome extension via an existing Chrome session.
 *
 * Usage: singlefile_extension_save.js --url=<url>
 * Output: prints saved file path on success
 */

const fs = require('fs');
const path = require('path');
const { ensureNodeModuleResolution, loadConfig, parseArgs } = require('../base/utils.js');

// Match the rest of the JS hook lifecycle: ArchiveBox resolves provider-owned
// node_modules once and passes NODE_MODULES_DIR to hook subprocesses. Helper
// scripts launched from Python must honor the same lookup path or they will
// fail to resolve shared dependencies like puppeteer-core even when the parent
// hook already has them available.
ensureNodeModuleResolution(module);

const EXTENSION = {
    webstore_id: 'mpiodijhokgodhhofbcjdecpffjipkle',
    name: 'singlefile',
};

const SNAPSHOT_OUTPUT_DIR = process.cwd();
const CHROME_SESSION_DIR = path.resolve(SNAPSHOT_OUTPUT_DIR, '..', 'chrome');
const hookConfig = loadConfig();
const DOWNLOADS_DIR = hookConfig.CHROME_DOWNLOADS_DIR ||
    path.join(hookConfig.PERSONAS_DIR,
        hookConfig.ACTIVE_PERSONA,
        'chrome_downloads');

process.env.CHROME_DOWNLOADS_DIR = DOWNLOADS_DIR;
const DOWNLOAD_POLL_INTERVAL_MS = 3000;
const DOWNLOAD_WAIT_RESERVE_MS = 10000;

function wait(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function getSinglefileDownloadWaitTimeoutMs(config = process.env, elapsedMs = process.uptime() * 1000) {
    const configuredTimeoutSeconds = Number(config.SINGLEFILE_TIMEOUT || config.TIMEOUT || 60);
    const totalTimeoutMs = Number.isFinite(configuredTimeoutSeconds) && configuredTimeoutSeconds > 0
        ? configuredTimeoutSeconds * 1000
        : 60000;
    return Math.max(
        DOWNLOAD_POLL_INTERVAL_MS,
        totalTimeoutMs - DOWNLOAD_WAIT_RESERVE_MS - Math.max(0, elapsedMs)
    );
}

async function saveSinglefileWithExtension(page, extension, options = {}) {
    if (!extension || !extension.version) {
        throw new Error('SingleFile extension not found or not loaded');
    }

    const url = await page.url();
    console.error(`[singlefile] Triggering extension for: ${url}`);

    const URL_SCHEMES_IGNORED = ['about', 'chrome', 'chrome-extension', 'data', 'javascript', 'blob'];
    const scheme = url.split(':')[0];
    if (URL_SCHEMES_IGNORED.includes(scheme)) {
        console.log(`[⚠️] Skipping SingleFile for URL scheme: ${scheme}`);
        return null;
    }

    const downloadsDir = options.downloadsDir || DOWNLOADS_DIR;
    console.error(`[singlefile] Watching downloads dir: ${downloadsDir}`);

    await fs.promises.mkdir(downloadsDir, { recursive: true });

    const files_before = new Set(
        (await fs.promises.readdir(downloadsDir))
            .filter(fn => fn.toLowerCase().endsWith('.html') || fn.toLowerCase().endsWith('.htm'))
    );

    const out_path = options.outputPath || path.join(SNAPSHOT_OUTPUT_DIR, 'singlefile.html');

    console.error(`[singlefile] Saving via extension (${extension.id})...`);
    await page.bringToFront();

    console.error('[singlefile] Dispatching extension action...');
    try {
        const actionTimeoutMs = options.actionTimeoutMs || 5000;
        const actionPromise = extension.dispatchAction();
        const actionResult = await Promise.race([
            actionPromise,
            wait(actionTimeoutMs).then(() => 'timeout'),
        ]);
        if (actionResult === 'timeout') {
            console.error(`[singlefile] Extension action did not resolve within ${actionTimeoutMs}ms, continuing...`);
        }
    } catch (err) {
        console.error(`[singlefile] Extension action error: ${err.message || err}`);
    }

    const waitTimeoutMs = getSinglefileDownloadWaitTimeoutMs();
    const deadline = Date.now() + waitTimeoutMs;
    let files_new = [];

    console.error(`[singlefile] Waiting up to ${Math.ceil(waitTimeoutMs / 1000)}s for download...`);
    for (let attempt = 1; Date.now() < deadline; attempt++) {
        const remainingBeforeSleepMs = Math.max(1, deadline - Date.now());
        await wait(Math.min(DOWNLOAD_POLL_INTERVAL_MS, remainingBeforeSleepMs));

        const files_after = (await fs.promises.readdir(downloadsDir))
            .filter(fn => fn.toLowerCase().endsWith('.html') || fn.toLowerCase().endsWith('.htm'));

        files_new = files_after.filter(file => !files_before.has(file));

        if (files_new.length === 0) {
            const remainingAfterPollSeconds = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
            console.error(`[singlefile] No new downloads yet (${attempt}, ${remainingAfterPollSeconds}s remaining)`);
            continue;
        }

        console.error(`[singlefile] New download(s) detected: ${files_new.join(', ')}`);

        const url_variants = new Set([url]);
        if (url.endsWith('/')) {
            url_variants.add(url.slice(0, -1));
        } else {
            url_variants.add(`${url}/`);
        }

        const scored = [];
        for (const file of files_new) {
            const dl_path = path.join(downloadsDir, file);
            let header = '';
            try {
                const dl_text = await fs.promises.readFile(dl_path, 'utf-8');
                header = dl_text.slice(0, 200000);
                const stat = await fs.promises.stat(dl_path);
                console.error(`[singlefile] Download ${file} size=${stat.size} bytes`);
            } catch (err) {
                continue;
            }

            const header_lower = header.toLowerCase();
            const has_url = Array.from(url_variants).some(v => header.includes(v));
            const has_singlefile_marker = header_lower.includes('singlefile') || header_lower.includes('single-file');
            const score = (has_url ? 2 : 0) + (has_singlefile_marker ? 1 : 0);
            scored.push({ file, dl_path, score });
        }

        scored.sort((a, b) => b.score - a.score);

        if (scored.length > 0) {
            const best = scored[0];
            if (best.score > 0 || files_new.length === 1) {
                console.error(`[singlefile] Moving download from ${best.file} -> ${out_path}`);
                await fs.promises.rename(best.dl_path, out_path);
                const out_stat = await fs.promises.stat(out_path);
                console.error(`[singlefile] Moved file size=${out_stat.size} bytes`);
                return out_path;
            }
        }

        if (files_new.length > 0) {
            let newest = null;
            let newest_mtime = -1;
            for (const file of files_new) {
                const dl_path = path.join(downloadsDir, file);
                try {
                    const stat = await fs.promises.stat(dl_path);
                    if (stat.mtimeMs > newest_mtime) {
                        newest_mtime = stat.mtimeMs;
                        newest = { file, dl_path };
                    }
                } catch (err) {}
            }
            if (newest) {
                console.error(`[singlefile] Moving newest download from ${newest.file} -> ${out_path}`);
                await fs.promises.rename(newest.dl_path, out_path);
                const out_stat = await fs.promises.stat(out_path);
                console.error(`[singlefile] Moved file size=${out_stat.size} bytes`);
                return out_path;
            }
        }
    }

    console.error(`[singlefile] Failed to find SingleFile HTML in ${downloadsDir} after ${Math.ceil(waitTimeoutMs / 1000)}s`);
    console.error(`[singlefile] New files seen: ${files_new.join(', ')}`);
    return null;
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
        const chromeUtils = require('../chrome/chrome_utils.js');
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
        const { browser, page, cdpSession, extensions } = await chromeUtils.connectToPage({
            chromeSessionDir: CHROME_SESSION_DIR,
            timeoutMs: 60000,
            requireTargetId: true,
            requireExtensionsLoaded: true,
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
                await cdpSession.send('Target.setDiscoverTargets', { discover: true });
            } catch (err) {
                console.error(`[singlefile] failed to enable target discovery: ${err.message || err}`);
            }

            // Resolve extension id from snapshot chrome session metadata and connect to target by id.
            console.error('[singlefile] waiting for extensions metadata...');
            const sessionExtensions = extensions || [];
            const sessionEntry = chromeUtils.findExtensionMetadataByName(sessionExtensions, extension.name);
            if (!sessionEntry || !sessionEntry.id) {
                console.error(`[singlefile] extension metadata missing id for name=${extension.name}`);
                await browser.disconnect();
                process.exit(5);
            }
            extension.id = sessionEntry.id;
            console.error(`[singlefile] resolved extension id from session metadata: ${extension.id}`);

            const preferredTargetUrl = sessionEntry.target_url || null;
            const extensionTarget = await chromeUtils.waitForExtensionTargetHandle(
                browser,
                extension.id,
                30000,
                preferredTargetUrl
            );
            console.error('[singlefile] loading extension from target...');
            await chromeUtils.loadExtensionFromTarget([extension], extensionTarget);
            if (typeof extension.dispatchAction !== 'function') {
                console.error(`[singlefile] extension dispatchAction missing for id=${extension.id}`);
                await browser.disconnect();
                process.exit(6);
            }
            console.error('[singlefile] setting download dir...');
            await chromeUtils.setBrowserDownloadBehavior({ page, downloadPath: DOWNLOADS_DIR });

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

module.exports = {
    EXTENSION,
    getSinglefileDownloadWaitTimeoutMs,
    saveSinglefileWithExtension,
};
