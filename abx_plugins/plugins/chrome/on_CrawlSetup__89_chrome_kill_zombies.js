#!/usr/bin/env node
/**
 * Sweep stale Chrome processes before crawl-scoped launch starts.
 *
 * This keeps the existing killZombieChrome() behavior, but runs it as its own
 * CrawlSetup stage instead of burying it inside launchChromium().
 */

const fs = require("fs");
const path = require("path");
const {
  ensureNodeModuleResolution,
  loadConfig,
  PROCESS_EXIT_SKIPPED,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);

const { killZombieChrome } = require("./chrome_utils.js");

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const CRAWL_DIR = path.resolve((hookConfig.CRAWL_DIR || ".").trim());
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || CRAWL_DIR).trim());
const CHROME_USER_DATA_DIR = hookConfig.CHROME_USER_DATA_DIR
  ? path.resolve(String(hookConfig.CHROME_USER_DATA_DIR).trim())
  : null;
const OUTPUT_DIR = path.join(CRAWL_DIR, PLUGIN_DIR);
const START_CPU = process.cpuUsage();
const START_TIME = process.hrtime.bigint();
if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

function getSweepDirs() {
  const sweepDirs = [SNAP_DIR];
  let current = CRAWL_DIR;
  while (current && current !== path.dirname(current)) {
    if (path.basename(current) === "crawls") {
      sweepDirs.push(current);
      break;
    }
    current = path.dirname(current);
  }
  return Array.from(new Set(sweepDirs.map((dir) => path.resolve(dir))));
}

async function main() {
  let killed = 0;
  const sweepDirs = getSweepDirs();
  for (const [index, sweepDir] of sweepDirs.entries()) {
    killed += await killZombieChrome(sweepDir, {
      excludeCrawlDirs: [CRAWL_DIR],
      quiet: true,
      CHROME_USER_DATA_DIR: index === 0 ? CHROME_USER_DATA_DIR : null,
    });
  }
  const elapsedMicros = Number(process.hrtime.bigint() - START_TIME) / 1000;
  const cpu = process.cpuUsage(START_CPU);
  const cpuMicros = cpu.user + cpu.system;
  const cpuUsage =
    elapsedMicros > 0 ? Math.round((cpuMicros / elapsedMicros) * 100) : 0;
  console.log(`${killed} chrome zombies. cpu usage: ${cpuUsage}%`);
  process.exit(PROCESS_EXIT_SKIPPED);
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(`Fatal error: ${error.message}`);
    process.exit(1);
  });
