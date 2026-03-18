#!/usr/bin/env node
/**
 * Print a URL to PDF using Chrome/Puppeteer.
 *
 * Requires a Chrome session (from chrome plugin) and connects to it via CDP.
 *
 * Usage: on_Snapshot__52_pdf.js --url=<url> --snapshot-id=<uuid>
 * Output: Writes pdf/output.pdf
 *
 * Environment variables:
 *     PDF_ENABLED: Enable PDF generation (default: true)
 */

const fs = require('fs');
const path = require('path');
const {
    ensureNodeModuleResolution,
    getEnvBool,
    getEnvInt,
    parseArgs,
    emitArchiveResult,
    hasStaticFileOutput,
} = require('../base/utils.js');
ensureNodeModuleResolution(module);
const { connectToPage } = require('../chrome/chrome_utils.js');

function tempPathFor(filePath) {
    const dir = path.dirname(filePath);
    const base = path.basename(filePath);
    return path.join(dir, `.${base}.${process.pid}.tmp`);
}

// Check if PDF is enabled BEFORE requiring puppeteer
if (!getEnvBool('PDF_ENABLED', true)) {
    console.error('Skipping PDF (PDF_ENABLED=False)');
    emitArchiveResult('skipped', 'PDF_ENABLED=False');
    process.exit(0);
}

// Now safe to require puppeteer
const puppeteer = require('puppeteer-core');

// Extractor metadata
const PLUGIN_NAME = 'pdf';
const PLUGIN_DIR = path.basename(__dirname);
const SNAP_DIR = path.resolve((process.env.SNAP_DIR || '.').trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const OUTPUT_FILE = 'output.pdf';
const CHROME_SESSION_DIR = '../chrome';

async function printToPdf(url, timeoutMs) {
    // Output directory is current directory (hook already runs in output dir)
    const outputPath = path.join(OUTPUT_DIR, OUTPUT_FILE);
    const tempOutputPath = tempPathFor(outputPath);

    let browser = null;

    try {
        const connection = await connectToPage({
            chromeSessionDir: CHROME_SESSION_DIR,
            timeoutMs,
            waitForNavigationComplete: true,
            postLoadDelayMs: 200,
            puppeteer,
        });
        browser = connection.browser;
        const page = connection.page;

        // Print to PDF
        await page.pdf({
            path: tempOutputPath,
            format: 'A4',
            printBackground: true,
            margin: {
                top: '0.5in',
                right: '0.5in',
                bottom: '0.5in',
                left: '0.5in',
            },
        });

        fs.renameSync(tempOutputPath, outputPath);

        if (fs.existsSync(outputPath) && fs.statSync(outputPath).size > 0) {
            return { success: true, output: OUTPUT_FILE };
        } else {
            return { success: false, error: 'PDF file not created' };
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
        console.error('Usage: on_Snapshot__52_pdf.js --url=<url> --snapshot-id=<uuid>');
        emitArchiveResult('failed', 'missing required args');
        process.exit(1);
    }

    try {
        // Check if staticfile extractor already handled this (permanent skip)
        if (hasStaticFileOutput()) {
            console.error(`Skipping PDF - staticfile extractor already downloaded this`);
            emitArchiveResult('noresults', 'staticfile already handled');
            process.exit(0);
        }

        const timeoutMs = getEnvInt('PDF_TIMEOUT', getEnvInt('TIMEOUT', 30)) * 1000;

        const result = await printToPdf(url, timeoutMs);

        if (result.success) {
            // Success - emit ArchiveResult
            const size = fs.statSync(path.join(OUTPUT_DIR, result.output)).size;
            console.error(`PDF saved (${size} bytes)`);
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
