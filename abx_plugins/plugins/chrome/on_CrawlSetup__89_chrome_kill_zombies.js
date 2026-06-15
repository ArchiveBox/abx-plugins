#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries node
// /// script
// ///
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
} = require("../base/utils.js");
ensureNodeModuleResolution(module);

const {
  getChromeSessionOptionsFromConfig,
  killZombieChrome,
} = require("./chrome_utils.js");

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const CRAWL_DIR = path.resolve((hookConfig.CRAWL_DIR || ".").trim());
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || CRAWL_DIR).trim());
const CHROME_USER_DATA_DIR = getChromeSessionOptionsFromConfig(hookConfig)
  .CHROME_USER_DATA_DIR;
const OUTPUT_DIR = path.join(CRAWL_DIR, PLUGIN_DIR);
const START_CPU = process.cpuUsage();
const START_TIME = process.hrtime.bigint();
if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

function getSweepDirs() {
  const sweepDirs = [SNAP_DIR, CRAWL_DIR];
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
  process.exit(0);
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(`Fatal error: ${error.message}`);
    process.exit(1);
  });
