#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
const path = require("node:path");
const fs = require("node:fs");
const {
  loadConfig,
  emitArchiveResultRecord,
  ensureNodeModuleResolution,
  PROCESS_EXIT_SKIPPED,
} = require("../base/utils.js");
const config = loadConfig(path.join(__dirname, "config.json"), {
  hydrateUserAgent: false,
});
const script = config.PUPPETEER_SCRIPT.trim();
if (!config.PUPPETEER_ENABLED || !script) {
  emitArchiveResultRecord("skipped", "No script configured");
  process.exit(PROCESS_EXIT_SKIPPED);
}
ensureNodeModuleResolution(module);
const {
  attachSnapshot,
  confirmMainWorld,
  summaryExpression,
} = require("../chrome/snapshot.js");
const { writeDownloadsIndex } = require("../base/downloads.js");
const outputDir = path.join(path.resolve(config.SNAP_DIR || "."), "puppeteer");
fs.mkdirSync(outputDir, { recursive: true });
process.chdir(outputDir);
const timeoutMs = config.PUPPETEER_TIMEOUT * 1000;
let connection, browser, page, client, evaluate, disconnect;
let timer;

async function run() {
  connection = await attachSnapshot(config, timeoutMs);
  browser = connection.browser;
  page = connection.page;
  disconnect = () => browser.disconnect();
  evaluate = (expression) => page.evaluate(expression);
  await confirmMainWorld(evaluate, connection);
  if (script) {
    const AsyncFunction = Object.getPrototypeOf(
      async function () {},
    ).constructor;
    await new AsyncFunction(
      "browser",
      "page",
      "config",
      "client",
      "outputDir",
      script,
    )(browser, page, config, client, outputDir);
  } else {
    throw new Error("A script is required");
  }
  const result = await evaluate(summaryExpression);
  const output = outputDir;
  fs.mkdirSync(output, { recursive: true });
  fs.writeFileSync(
    path.join(output, "result.log"),
    JSON.stringify(
      { target_id: connection.targetId, title: result.title, url: result.url },
      null,
      2,
    ) + "\n",
  );
  return writeDownloadsIndex(outputDir)
    ? "puppeteer/index.html"
    : "Page prepared";
}

async function cleanup() {
  try {
    if (disconnect) await disconnect();
  } finally {
    if (connection) await connection.browser.disconnect();
  }
}
process.once("SIGTERM", () => {
  cleanup().finally(() => process.exit(143));
});

Promise.race([
  run(),
  new Promise((_, reject) => {
    timer = setTimeout(
      () => reject(new Error("puppeteer timed out")),
      timeoutMs,
    );
  }),
])
  .then(
    async (output) => {
      await cleanup();
      emitArchiveResultRecord("succeeded", output);
    },
    async (error) => {
      emitArchiveResultRecord("failed", error.message);
      try {
        await cleanup();
      } catch (cleanupError) {
        console.error(cleanupError.message);
      }
      process.exitCode = 1;
    },
  )
  .catch((error) => {
    emitArchiveResultRecord("failed", error.message);
    process.exitCode = 1;
  })
  .finally(() => {
    clearTimeout(timer);
    process.exit(process.exitCode || 0);
  });
