#!/usr/bin/env node
/**
 * Sweep stale Chrome processes before crawl-scoped launch starts.
 *
 * This keeps the existing killZombieChrome() behavior, but runs it as its own
 * CrawlSetup stage instead of burying it inside launchChromium().
 */

const fs = require('fs');
const path = require('path');
const { ensureNodeModuleResolution, loadConfig, PROCESS_EXIT_SKIPPED } = require('../base/utils.js');
ensureNodeModuleResolution(module);

const { killZombieChrome } = require('./chrome_utils.js');

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const CRAWL_DIR = path.resolve((hookConfig.CRAWL_DIR || '.').trim());
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || CRAWL_DIR).trim());
const OUTPUT_DIR = path.join(CRAWL_DIR, PLUGIN_DIR);
const START_CPU = process.cpuUsage();
const START_TIME = process.hrtime.bigint();
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

function main() {
    const killed = killZombieChrome(SNAP_DIR, {
        excludeCrawlDirs: [CRAWL_DIR],
        quiet: true,
    });
    const elapsedMicros = Number(process.hrtime.bigint() - START_TIME) / 1000;
    const cpu = process.cpuUsage(START_CPU);
    const cpuMicros = cpu.user + cpu.system;
    const cpuUsage = elapsedMicros > 0 ? Math.round((cpuMicros / elapsedMicros) * 100) : 0;
    console.log(`${killed} chrome zombies. cpu usage: ${cpuUsage}%`);
    process.exit(PROCESS_EXIT_SKIPPED);
}

try {
    main();
    process.exit(0);
} catch (error) {
    console.error(`Fatal error: ${error.message}`);
    process.exit(1);
}
