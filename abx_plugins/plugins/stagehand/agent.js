// Execute an agent's async JavaScript with native Stagehand objects on the capture tab.
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
(async () => {
  const { Stagehand, localBrowser } = await import(
    pathToFileURL(process.env.ARCHIVEBOX_STAGEHAND_MODULE).href
  );
  const browser = await localBrowser.connect({
    cdpUrl: process.env.ARCHIVEBOX_BROWSER_CDP_URL,
  });
  try {
    const client = await Stagehand.create({ browser });
    const pages = (await browser.context.pages()).filter(
      page => page.pageId === process.env.ARCHIVEBOX_BROWSER_TARGET_ID
    );
    if (pages.length !== 1) throw new Error("Snapshot tab is no longer available");
    const page = pages[0];
    const outputDir = path.join(path.resolve(process.env.SNAP_DIR || "."), "stagehand");
    const variables = JSON.parse(process.env.STAGEHAND_VARIABLES || "{}");
    // Native substitution keeps variable values out of the agent prompt.
    const act = async (instruction) => {
      const modelName = process.env.STAGEHAND_LLM_MODEL;
      const provider = modelName.split("/")[0];
      const apiKey = process.env.STAGEHAND_API_KEY || {
        openai: process.env.OPENAI_API_KEY,
        anthropic: process.env.ANTHROPIC_API_KEY,
        google: process.env.GOOGLE_API_KEY,
        groq: process.env.GROQ_API_KEY,
        cerebras: process.env.CEREBRAS_API_KEY,
      }[provider];
      return await client.act(instruction, {
        page, variables, model: {modelName, ...(apiKey ? {apiKey} : {})},
      });
    };
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
    await new AsyncFunction("browser", "page", "client", "act", "outputDir", fs.readFileSync(0, "utf8"))(
      browser, page, client, act, outputDir
    );
  } finally {
    await browser.disconnect();
  }
})().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
