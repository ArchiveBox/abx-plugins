#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Keep uBlock's subresource filtering without allowing it to replace the page
 * that ArchiveBox is recording.
 *
 * uBlock Origin Lite enables strict blocking by default. A strict-blocked
 * main-frame navigation is redirected to the extension's strictblock.html,
 * which makes ArchiveWeb.page correctly detach from the now non-web tab. The
 * setting is browser-global, so configure it once after the crawl browser and
 * extensions are ready, before any snapshot hooks can create recordings.
 */

const fs = require("fs");
const path = require("path");
const {
  PROCESS_EXIT_SKIPPED,
  ensureNodeModuleResolution,
  getEnv,
  getEnvBool,
  loadConfig,
} = require("../base/utils.js");

const hookConfig = loadConfig();
const CRAWL_DIR = path.resolve((hookConfig.CRAWL_DIR || ".").trim());
const CHROME_SESSION_DIR = path.join(CRAWL_DIR, "chrome");
const OUTPUT_DIR = path.join(CRAWL_DIR, path.basename(__dirname));
fs.mkdirSync(OUTPUT_DIR, { recursive: true });
process.chdir(OUTPUT_DIR);

async function main() {
  if (!getEnvBool("UBLOCK_ENABLED", true)) {
    process.exit(PROCESS_EXIT_SKIPPED);
  }
  if (getEnv("CHROME_ISOLATION", "crawl").toLowerCase() === "snapshot") {
    process.exit(PROCESS_EXIT_SKIPPED);
  }

  ensureNodeModuleResolution(module);
  const { disableStrictBlocking } = require("./ublock_internal.js");
  await disableStrictBlocking(CHROME_SESSION_DIR);
  console.error(
    "[+] Disabled uBlock top-level strict blocking; subresource filtering remains enabled",
  );
}

main().catch((error) => {
  console.error(error && (error.stack || error.message || String(error)));
  process.exit(1);
});
