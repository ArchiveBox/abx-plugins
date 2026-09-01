#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/** Configure the snapshot-owned browser before any recorder can navigate it. */

const path = require("path");
const {
  PROCESS_EXIT_SKIPPED,
  emitArchiveResultRecord,
  ensureNodeModuleResolution,
  getEnv,
  getEnvBool,
  loadConfig,
} = require("../base/utils.js");

async function main() {
  if (!getEnvBool("UBLOCK_ENABLED", true)) {
    process.exit(PROCESS_EXIT_SKIPPED);
  }
  if (getEnv("CHROME_ISOLATION", "crawl").toLowerCase() !== "snapshot") {
    emitArchiveResultRecord("skipped", "CHROME_ISOLATION=crawl");
    process.exit(0);
  }

  ensureNodeModuleResolution(module);
  const hookConfig = loadConfig();
  const snapshotDir = path.resolve((hookConfig.SNAP_DIR || ".").trim());
  const { disableStrictBlocking } = require("./ublock_internal.js");
  await disableStrictBlocking(path.join(snapshotDir, "chrome"));
  console.error(
    "[+] Disabled uBlock top-level strict blocking; subresource filtering remains enabled",
  );
}

main().catch((error) => {
  console.error(error && (error.stack || error.message || String(error)));
  process.exit(1);
});
