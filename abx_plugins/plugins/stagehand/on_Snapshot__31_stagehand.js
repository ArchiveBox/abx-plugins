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
const script = config.STAGEHAND_SCRIPT.trim();
const prompt = (config.STAGEHAND_PROMPT || "").trim();
if (!config.STAGEHAND_ENABLED || (!script && !prompt)) {
  emitArchiveResultRecord("skipped", "No script or prompt configured");
  process.exit(PROCESS_EXIT_SKIPPED);
}
ensureNodeModuleResolution(module);
const {
  attachSnapshot,
  confirmMainWorld,
  summaryExpression,
  taskPrompt,
} = require("../chrome/snapshot.js");
const { writeDownloadsIndex } = require("../base/downloads.js");
const outputDir = path.join(path.resolve(config.SNAP_DIR || "."), "stagehand");
fs.mkdirSync(outputDir, { recursive: true });
process.chdir(outputDir);
const timeoutMs = config.STAGEHAND_TIMEOUT * 1000;
let connection, browser, page, client, evaluate, disconnect;
let timer, agentResult;
const started = Date.now();

async function run() {
  connection = await attachSnapshot(config, timeoutMs);
  const { tsImport } = require("tsx/esm/api");
  const { pathToFileURL } = require("node:url");
  const { StagehandClient } = await tsImport(
    require.resolve("shalpha"),
    pathToFileURL(__filename).href,
  );
  client = new StagehandClient({
    cdp_url: connection.browser.wsEndpoint(),
    keep_alive: true,
    logging: { sentry_enabled: false },
  });
  disconnect = () => client.close();
  await client.connect();
  browser = client.browser;
  const pages = await browser.pages({ targetId: connection.targetId });
  if (pages.length !== 1)
    throw new Error("Snapshot tab is no longer available");
  page = pages[0];
  evaluate = (expression) => page.evaluate({ expression });
  await confirmMainWorld(evaluate, connection);
  const summary = await evaluate(summaryExpression);
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
    const { runTask } = require("../opencode/task.js");
    const variableNames = Object.keys(config.STAGEHAND_VARIABLES)
      .map((name) => `%${name}%`)
      .join(", ");
    const command =
      JSON.stringify(process.execPath) +
      " " +
      JSON.stringify(path.join(__dirname, "agent.js"));
    const instructions =
      taskPrompt(summary, prompt) +
      "\n\nUse the bash tool with this exact shell syntax. The heredoc contains async JavaScript with native Stagehand browser, page, client objects already attached to this tab:\n" +
      command +
      " <<'JS'\nconsole.log(await page.evaluate({expression: 'document.title'}));\nJS\n" +
      "Use page.evaluate({expression: JAVASCRIPT_STRING}) for main-world DOM inspection and interactions. " +
      "Use native client/page APIs when DOM JavaScript is insufficient. Each call disconnects while preserving the tab. " +
      "Do not launch a browser or select a different tab. Save downloaded files to the outputDir variable provided to your code. " +
      (variableNames
        ? `Native variables: ${variableNames}. Use await act('instruction using %name%') to substitute their values through Stagehand. Never read or print the values. `
        : "");
    agentResult = await runTask({
      prompt: instructions,
      model: config.STAGEHAND_MODEL,
      timeoutMs: Math.max(1, timeoutMs - (Date.now() - started)),
      outputDir,
      env: {
        ...process.env,
        ARCHIVEBOX_STAGEHAND_MODULE: require.resolve("shalpha"),
        ARCHIVEBOX_BROWSER_CDP_URL: connection.browser.wsEndpoint(),
        ARCHIVEBOX_BROWSER_TARGET_ID: connection.targetId,
        STAGEHAND_VARIABLES: JSON.stringify(config.STAGEHAND_VARIABLES),
        STAGEHAND_API_KEY: config.STAGEHAND_API_KEY,
        STAGEHAND_BASE_URL: config.STAGEHAND_BASE_URL,
        STAGEHAND_LLM_MODEL:
          config.STAGEHAND_LLM_MODEL || config.STAGEHAND_MODEL,
      },
    });
  }
  const result = await evaluate(summaryExpression);
  const output = outputDir;
  fs.mkdirSync(output, { recursive: true });
  fs.writeFileSync(
    path.join(output, "result.log"),
    JSON.stringify(
      {
        target_id: connection.targetId,
        title: result.title,
        url: result.url,
        ...(agentResult || {}),
      },
      null,
      2,
    ) + "\n",
  );
  return writeDownloadsIndex(outputDir)
    ? "stagehand/index.html"
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
      () => reject(new Error("stagehand timed out")),
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
