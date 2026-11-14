// Execute an agent's async JavaScript with native Stagehand objects on the capture tab.
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { ensureNodeModuleResolution } = require("../base/utils.js");
ensureNodeModuleResolution(module);
(async () => {
  const { tsImport } = require("tsx/esm/api");
  const { StagehandClient } = await tsImport(
    process.env.ARCHIVEBOX_STAGEHAND_MODULE,
    pathToFileURL(__filename).href,
  );
  const client = new StagehandClient({
    cdp_url: process.env.ARCHIVEBOX_BROWSER_CDP_URL,
    keep_alive: true,
    logging: { sentry_enabled: false },
  });
  await client.connect();
  try {
    const browser = client.browser;
    const pages = await browser.pages({
      targetId: process.env.ARCHIVEBOX_BROWSER_TARGET_ID,
    });
    if (pages.length !== 1)
      throw new Error("Snapshot tab is no longer available");
    const page = pages[0];
    const outputDir = path.join(
      path.resolve(process.env.SNAP_DIR || "."),
      "stagehand",
    );
    const variables = JSON.parse(process.env.STAGEHAND_VARIABLES || "{}");
    // Native Stagehand substitution keeps variable values out of the agent prompt.
    const act = async (instruction) => {
      const model = process.env.STAGEHAND_LLM_MODEL;
      const provider = model.split("/")[0];
      const apiKey =
        process.env.STAGEHAND_API_KEY ||
        {
          openai: process.env.OPENAI_API_KEY,
          anthropic: process.env.ANTHROPIC_API_KEY,
        }[provider];
      const llm = await client.llm.connect({
        api_key: apiKey,
        llm_model_name: model,
        ...(process.env.STAGEHAND_BASE_URL
          ? { base_url: process.env.STAGEHAND_BASE_URL }
          : {}),
      });
      return await client.ai.act({
        instruction,
        page,
        llm,
        variables,
        max_steps: 30,
        autoheal: true,
      });
    };
    const AsyncFunction = Object.getPrototypeOf(
      async function () {},
    ).constructor;
    await new AsyncFunction(
      "browser",
      "page",
      "client",
      "act",
      "outputDir",
      fs.readFileSync(0, "utf8"),
    )(browser, page, client, act, outputDir);
  } finally {
    await client.close();
  }
})().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
