#!/usr/bin/env node
/**
 * Dump the DOM of a URL using Chrome/Puppeteer.
 *
 * Requires a Chrome session (from chrome plugin) and connects to it via CDP.
 *
 * Usage: on_Snapshot__53_dom.js --url=<url> --snapshot-id=<uuid>
 * Output: Writes dom/output.html
 *
 * Environment variables:
 *     DOM_ENABLED: Enable DOM extraction (default: true)
 */

const fs = require('fs');
const path = require('path');
// Add NODE_MODULES_DIR to module resolution paths if set
if (process.env.NODE_MODULES_DIR) module.paths.unshift(process.env.NODE_MODULES_DIR);

const {
    getEnvBool,
    getEnvInt,
    parseArgs,
    readCdpUrl,
    connectToPage,
    waitForPageLoaded,
} = require('../chrome/chrome_utils.js');

function emitArchiveResult(status, outputStr) {
    console.log(JSON.stringify({
        type: 'ArchiveResult',
        status,
        output_str: outputStr,
    }));
}

function writeFileAtomic(filePath, contents) {
    const dir = path.dirname(filePath);
    const base = path.basename(filePath);
    const tmpPath = path.join(dir, `.${base}.${process.pid}.tmp`);
    fs.writeFileSync(tmpPath, contents, 'utf8');
    fs.renameSync(tmpPath, filePath);
}

// Check if DOM is enabled BEFORE requiring puppeteer
if (!getEnvBool('DOM_ENABLED', true)) {
    console.error('Skipping DOM (DOM_ENABLED=False)');
    emitArchiveResult('skipped', 'DOM_ENABLED=False');
    process.exit(0);
}

// Now safe to require puppeteer
const puppeteer = require('puppeteer-core');

// Extractor metadata
const PLUGIN_NAME = 'dom';
const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((process.env.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const OUTPUT_FILE = 'output.html';
const CHROME_SESSION_DIR = '../chrome';

// Check if staticfile extractor already downloaded this URL
const STATICFILE_DIR = '../staticfile';
function hasStaticFileOutput() {
    if (!fs.existsSync(STATICFILE_DIR)) return false;
    const stdoutPath = path.join(STATICFILE_DIR, 'stdout.log');
    if (!fs.existsSync(stdoutPath)) return false;
    const stdout = fs.readFileSync(stdoutPath, 'utf8');
    for (const line of stdout.split('\n')) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('{')) continue;
        try {
            const record = JSON.parse(trimmed);
            if (record.type === 'ArchiveResult' && record.status === 'succeeded') {
                return true;
            }
        } catch (e) {}
    }
    return false;
}

async function dumpDom(url, timeoutMs) {
    // Output directory is current directory (hook already runs in output dir)
    const outputPath = path.join(OUTPUT_DIR, OUTPUT_FILE);

    let browser = null;

    try {
        if (!readCdpUrl(CHROME_SESSION_DIR)) {
            return { success: false, error: 'No Chrome session found (chrome plugin must run first)' };
        }

        const connection = await connectToPage({
            chromeSessionDir: CHROME_SESSION_DIR,
            timeoutMs,
            puppeteer,
        });
        browser = connection.browser;
        const page = connection.page;

        await waitForPageLoaded(CHROME_SESSION_DIR, timeoutMs * 4, 200);

        // Get the full DOM content
        const domContent = await page.content();

        if (domContent && domContent.length > 100) {
            writeFileAtomic(outputPath, domContent);
            return { success: true, output: OUTPUT_FILE };
        } else {
            return { success: true, noresults: true, output: 'DOM content too short or empty' };
        }

    } catch (e) {
        return { success: false, error: `${e.name}: ${e.message}` };
    } finally {
        if (browser) {
            browser.disconnect();
        }
    }
}

async function main() {
    const args = parseArgs();
    const url = args.url;
    const snapshotId = args.snapshot_id;

    if (!url || !snapshotId) {
        console.error('Usage: on_Snapshot__53_dom.js --url=<url> --snapshot-id=<uuid>');
        emitArchiveResult('failed', 'missing required args');
        process.exit(1);
    }

    try {
        // Check if staticfile extractor already handled this (permanent skip)
        if (hasStaticFileOutput()) {
            console.error(`Skipping DOM - staticfile extractor already downloaded this`);
            emitArchiveResult('noresults', 'staticfile already handled');
            process.exit(0);
        }

        const timeoutMs = getEnvInt('DOM_TIMEOUT', getEnvInt('TIMEOUT', 30)) * 1000;

            const result = await dumpDom(url, timeoutMs);

        if (result.success) {
            if (result.noresults) {
                emitArchiveResult('noresults', result.output);
                process.exit(0);
            }
            const size = fs.statSync(path.join(OUTPUT_DIR, result.output)).size;
            console.error(`DOM saved (${size} bytes)`);
            emitArchiveResult('succeeded', result.output);
            process.exit(0);
        } else {
            console.error(`ERROR: ${result.error}`);
            emitArchiveResult('failed', result.error);
            process.exit(1);
        }
    } catch (e) {
        console.error(`ERROR: ${e.name}: ${e.message}`);
        emitArchiveResult('failed', `${e.name}: ${e.message}`);
        process.exit(1);
    }
}

main().catch(e => {
    console.error(`Fatal error: ${e.message}`);
    emitArchiveResult('failed', `${e.name}: ${e.message}`);
    process.exit(1);
});
