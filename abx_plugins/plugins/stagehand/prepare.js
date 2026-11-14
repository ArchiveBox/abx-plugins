// Corrections to the published shalpha rc.56 SDK and its bundled extension.
// See README.md for provenance, affected upstream functions, and removal criteria.
const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { createRequire } = require("node:module");
const { getLibDir, ensureNodeModuleResolution } = require("../base/utils.js");
ensureNodeModuleResolution(module);

function replaceOnce(source, before, after) {
  if (source.split(before).length !== 2)
    throw new Error(
      "Stagehand rc.56 correction does not match installed source"
    );
  return source.replace(before, after);
}

function prepareStagehand(modulePath) {
  const source = path.dirname(fs.realpathSync(modulePath));
  const metadata = JSON.parse(
    fs.readFileSync(path.join(source, "package.json"))
  );
  if (metadata.name !== "shalpha" || metadata.version !== "4.0.0-rc.56")
    throw new Error("Stagehand corrections require shalpha 4.0.0-rc.56");
  if (metadata.archivebox_original_module)
    return prepareStagehand(metadata.archivebox_original_module);

  const sourceRequire = createRequire(path.join(source, "index.ts"));
  const { unzipSync, zipSync, strFromU8, strToU8 } = sourceRequire("fflate");
  const archiveName = "stagehand-ext-v4.0.0rc56-f3179ab9.zip";
  const originalArchive = fs.readFileSync(
    path.join(source, "stagehand", archiveName)
  );
  const archive = unzipSync(originalArchive);
  const backgroundPath = "stagehand-modcdp/background.js";
  let background = strFromU8(archive[backgroundPath]);
  background = replaceOnce(
    background,
    "Object.entries(stagehand_custom_events).map(([name, event]) => ({\n\t\tname,",
    "Object.entries(stagehand_custom_events).map(([name, event]) => ({\n\t\tname: stagehandCustomEventName(name),"
  );
  background = replaceOnce(
    background,
    "function stagehandCustomEventName(eventType) {\n\t\treturn String(eventType);",
    "function stagehandCustomEventName(eventType) {\n\t\treturn `Stagehand.${eventType}`;"
  );
  background = replaceOnce(
    background,
    "async function page_send_cdp(browser, event) {\n\t\tconst locator = await event.emit(USLocatePageEvent({ locator: event.page })).now().eventResult();",
    "async function page_send_cdp(browser, event) {\n\t\tconst locator = await event.emit(USLocatePageEvent({ locator: event.page })).now().eventResult();\n" +
      "\t\tconst frameId = event.page?.frameId ?? locator.frameId ?? (await event.emit(CDPSendEvent({\n" +
      "\t\t\tmethod: 'Mod.getTopology', params: {rootTargetId: locator.targetId, targetId: locator.targetId}, targetId: locator.targetId\n" +
      "\t\t})).now().eventResult()).rootFrameId;"
  );
  background = replaceOnce(
    background,
    "method: event.method,\n\t\t\tparams: event.params ?? {},\n\t\t\ttargetId: locator.targetId",
    "method: event.method,\n\t\t\tparams: event.params ?? {},\n\t\t\ttargetId: locator.targetId,\n\t\t\tframeId"
  );
  archive[backgroundPath] = strToU8(background);
  const runtime = replaceOnce(
    fs.readFileSync(path.join(source, "stagehand/runtime.ts"), "utf8"),
    "function busEventType(event: string): string {\n  return event;\n}",
    "function busEventType(event: string): string {\n  return event.startsWith('Stagehand.') ? event.slice('Stagehand.'.length) : event;\n}"
  );
  const index = fs.readFileSync(path.join(source, "index.ts"));
  const client = fs.readFileSync(path.join(source, "stagehand/client.ts"));
  const dependencies = {};
  // rc.56 imports abxbus without declaring it; our binary declaration supplies it.
  for (const name of [...Object.keys(metadata.dependencies), "abxbus"]) {
    let directory = path.dirname(sourceRequire.resolve(name));
    while (true) {
      const manifest = path.join(directory, "package.json");
      if (
        fs.existsSync(manifest) &&
        JSON.parse(fs.readFileSync(manifest)).name === name
      )
        break;
      if (directory === path.dirname(directory))
        throw new Error(`Cannot locate ${name} package`);
      directory = path.dirname(directory);
    }
    dependencies[name] = directory;
  }
  const identity = createHash("sha256")
    .update(source)
    .update(originalArchive)
    .update(background)
    .update(runtime)
    .update(index)
    .update(client)
    .update(JSON.stringify(dependencies))
    .digest("hex")
    .slice(0, 16);
  const parent = path.join(getLibDir(), "stagehand");
  const destination = path.join(parent, `4.0.0-rc.56-${identity}`);
  const entry = path.join(destination, "index.ts");
  if (fs.existsSync(entry)) return entry;

  fs.mkdirSync(parent, { recursive: true });
  const temporary = fs.mkdtempSync(path.join(parent, ".prepare-"));
  try {
    fs.writeFileSync(path.join(temporary, "index.ts"), index);
    fs.writeFileSync(
      path.join(temporary, "package.json"),
      JSON.stringify({
        ...metadata,
        archivebox_original_module: path.join(source, "index.ts"),
      })
    );
    fs.mkdirSync(path.join(temporary, "stagehand"));
    fs.writeFileSync(path.join(temporary, "stagehand/client.ts"), client);
    fs.writeFileSync(path.join(temporary, "stagehand/runtime.ts"), runtime);
    fs.writeFileSync(
      path.join(temporary, "stagehand", archiveName),
      zipSync(archive)
    );
    // Preserve native ESM resolution, including the SDK's undeclared abxbus import.
    for (const [name, directory] of Object.entries(dependencies)) {
      const link = path.join(temporary, "node_modules", name);
      fs.mkdirSync(path.dirname(link), { recursive: true });
      fs.symlinkSync(directory, link, "dir");
    }
    try {
      fs.renameSync(temporary, destination);
    } catch (error) {
      if (!["EEXIST", "ENOTEMPTY"].includes(error.code)) throw error;
    }
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
  return entry;
}

module.exports = { prepareStagehand };
if (require.main === module) console.log(prepareStagehand(process.argv[2]));
