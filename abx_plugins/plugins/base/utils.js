/**
 * Shared utilities for abx plugins (JavaScript).
 *
 * Provides common helpers used across multiple plugins:
 * - Environment variable parsing (getEnv, getEnvBool, getEnvInt, getEnvArray)
 * - CLI argument parsing (parseArgs)
 * - JSONL record emission (emitArchiveResultRecord, emitSnapshotRecord)
 * - Atomic file writing (writeFileAtomic)
 * - Sibling plugin output checking (hasStaticFileOutput)
 */

const fs = require("fs");
const os = require("os");
const path = require("path");

const BASE_CONFIG_PATH = path.join(__dirname, "config.json");
const PROCESS_EXIT_SKIPPED = 10;
const configCache = new Map();

function writeFdFully(fd, text) {
  const buffer = Buffer.from(text, "utf8");
  let offset = 0;
  while (offset < buffer.length) {
    offset += fs.writeSync(fd, buffer, offset, buffer.length - offset);
  }
}

// ---------------------------------------------------------------------------
// Environment variable helpers
// ---------------------------------------------------------------------------

function getCallerFile(skipFn) {
  const original = Error.prepareStackTrace;
  try {
    Error.prepareStackTrace = (_, stack) => stack;
    const err = new Error();
    Error.captureStackTrace(err, skipFn);
    const stack = err.stack || [];
    for (const site of stack) {
      const fileName =
        site && typeof site.getFileName === "function"
          ? site.getFileName()
          : null;
      if (fileName && fileName !== __filename) {
        return fileName;
      }
    }
  } finally {
    Error.prepareStackTrace = original;
  }
  return null;
}

function normalizeSchemaType(prop = {}) {
  const rawType = prop.type || "string";
  if (Array.isArray(rawType)) {
    const nonNullTypes = rawType.filter((typeName) => typeName !== "null");
    return {
      schemaType: nonNullTypes[0] || "string",
      nullable: rawType.includes("null"),
    };
  }
  return {
    schemaType: rawType,
    nullable: false,
  };
}

function parseArrayValue(rawValue, defaultValue) {
  const trimmed = String(rawValue || "").trim();
  if (!trimmed) return defaultValue;
  if (trimmed.startsWith("[")) {
    try {
      const parsed = JSON.parse(trimmed);
      if (Array.isArray(parsed)) return parsed.map((item) => String(item));
    } catch (error) {}
  }
  return trimmed
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function parseConfigValue(rawValue, prop = {}) {
  const { schemaType, nullable } = normalizeSchemaType(prop);
  const defaultValue = Object.prototype.hasOwnProperty.call(prop, "default")
    ? prop.default
    : nullable
    ? null
    : undefined;

  if (rawValue === undefined) {
    return defaultValue;
  }

  const trimmed = String(rawValue).trim();
  if (nullable && trimmed === "") {
    return null;
  }

  if (schemaType === "boolean") {
    const lowered = trimmed.toLowerCase();
    if (["true", "1", "yes", "on"].includes(lowered)) return true;
    if (["false", "0", "no", "off"].includes(lowered)) return false;
    return defaultValue;
  }

  if (schemaType === "integer") {
    const parsed = parseInt(trimmed, 10);
    return Number.isNaN(parsed) ? defaultValue : parsed;
  }

  if (schemaType === "number") {
    const parsed = Number(trimmed);
    return Number.isNaN(parsed) ? defaultValue : parsed;
  }

  if (schemaType === "array") {
    return parseArrayValue(trimmed, defaultValue || []);
  }

  if (schemaType === "object") {
    // Environment values are strings; CDP options (e.g. persona geolocation and
    // client hints) must reach hooks as objects, just like Python config does.
    const parsed = JSON.parse(trimmed);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("Expected a JSON object for object config");
    }
    return parsed;
  }

  return trimmed;
}

function getPlatformUserConfigDir() {
  if (process.platform === "darwin") {
    return path.join(os.homedir(), "Library", "Application Support", "abx");
  }
  if (process.platform === "win32") {
    return path.join(
      process.env.APPDATA || path.join(os.homedir(), "AppData", "Roaming"),
      "abx"
    );
  }
  return path.join(
    process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config"),
    "abx"
  );
}

function hydrateConfigValue(value, context) {
  if (typeof value === "string") {
    return value.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (match, key) => {
      const replacement = context[key];
      if (replacement === undefined || replacement === null) return match;
      return String(replacement);
    });
  }
  if (Array.isArray(value)) {
    return value.map((item) => hydrateConfigValue(item, context));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        hydrateConfigValue(item, context),
      ])
    );
  }
  return value;
}

function resolveConfigPath(configPath = null) {
  if (configPath) return path.resolve(configPath);
  const callerFile = getCallerFile(loadConfig);
  if (!callerFile) return BASE_CONFIG_PATH;
  return path.join(path.dirname(path.resolve(callerFile)), "config.json");
}

function argvUrl() {
  const argv = process.argv.slice(2);
  for (let idx = 0; idx < argv.length; idx++) {
    const arg = String(argv[idx] || "");
    if (arg === "--url" && idx + 1 < argv.length) {
      return String(argv[idx + 1] || "").trim();
    }
    if (arg.startsWith("--url=")) {
      return arg.slice("--url=".length).trim();
    }
  }
  return "";
}

/**
 * Return the URL in the normalized form Chrome and Node report it in.
 *
 * Snapshot URLs arrive as typed (https://münchen.de/über, https://example.com),
 * while page.url(), request.url() and fetch report the WHATWG-serialized URI
 * (https://xn--mnchen-3ya.de/%C3%BCber, https://example.com/). Compare URLs
 * through this, never as raw strings. Input that does not parse is returned unchanged.
 */
function normalizeUrl(url) {
  try {
    return new URL(url).href;
  } catch (error) {
    return url;
  }
}

function maybeSkipUnsupportedSnapshotUrl(schema) {
  const scriptName = path.basename(process.argv[1] || process.argv[0] || "");
  const url = argvUrl();
  if (!scriptName.startsWith("on_Snapshot__") || !url) return;
  if (url.startsWith("http://") || url.startsWith("https://") || url.startsWith("file://")) return;
  writeFdFully(
    1,
    `${JSON.stringify({
      type: "ArchiveResult",
      status: "noresults",
      output_str: `unsupported input URL: ${url}`,
    })}\n`
  );
  process.exit(0);
}

const automaticUserAgents = new Set([
  "",
  "Mozilla/5.0 (compatible; ArchiveBox/1.0)",
  "Mozilla/5.0 (compatible; abx-dl/1.0; +https://github.com/ArchiveBox/abx-dl)",
]);

function isAutomaticUserAgent(value) {
  return automaticUserAgents.has(value || "") || /^Mozilla\/5\.0 \(Macintosh; Intel Mac OS X 10_15_7\) AppleWebKit\/537\.36 \(KHTML, like Gecko\) Chrome\/131\.0\.0\.0 Safari\/537\.36 ArchiveBox\/[0-9A-Za-z.+-]+ \(\+https:\/\/github\.com\/ArchiveBox\/ArchiveBox\/\)$/.test(value);
}

function parseChromiumUserAgentVersion(output) {
  const version = String(output || "").match(/(\d+)\.\d+\.\d+(?:\.\d+)?/);
  return version ? `${version[1]}.0.0.0` : "";
}

function replaceChromeUserAgentVersion(userAgent, browserVersionOutput) {
  const version = parseChromiumUserAgentVersion(browserVersionOutput);
  if (!version) return userAgent || "";
  return String(userAgent || "").replace(
    /(Chrome|HeadlessChrome)\/(?:\{)?\d+\.\d+\.\d+(?:\.\d+)?(?:\})?/g,
    `$1/${version}`
  );
}

function resolveUserAgent(config) {
  const configured = config.USER_AGENT || "";
  const automatic = isAutomaticUserAgent(configured);
  if (!automatic && !/(?:Headless)?Chrome\//.test(configured)) return configured;
  const paths = ["SNAP_DIR", "CRAWL_DIR"]
    .filter(key => config[key])
    .map(key => path.join(config[key], "chrome", "browser.json"));
  if (config.ABXPKG_LIB_DIR) paths.push(path.join(config.ABXPKG_LIB_DIR, "chrome-user-agent.json"));
  for (const filename of paths) {
    try {
      const metadata = JSON.parse(fs.readFileSync(filename, "utf8"));
      const value = (metadata?.userAgentOverride || metadata)?.userAgent;
      if (typeof value === "string" && value.trim() && !/[\r\n]/.test(value)) {
        if (automatic) return value;
        const version = value.match(/(?:Headless)?Chrome\/\d+\.\d+\.\d+(?:\.\d+)?/);
        if (version) return replaceChromeUserAgentVersion(configured, version[0]);
      }
    } catch { /* Optional metadata may be absent or malformed. */ }
  }
  return automatic ? "" : configured;
}

function hydrateUserAgent(config, properties) {
  const original = config.USER_AGENT;
  config.USER_AGENT = resolveUserAgent(config);
  for (const [name, prop] of Object.entries(properties)) {
    if (prop["x-fallback"] === "USER_AGENT" && (config[name] === original || isAutomaticUserAgent(config[name]))) {
      config[name] = config.USER_AGENT;
    }
  }
  return config;
}

function loadConfig(configPath = null, { hydrateUserAgent: hydrateIdentity = true } = {}) {
  const pluginConfigPath = resolveConfigPath(configPath);
  const pluginMtime = fs.statSync(pluginConfigPath).mtimeMs;
  const baseMtime = fs.statSync(BASE_CONFIG_PATH).mtimeMs;
  const cacheKey = pluginConfigPath;
  const cached = configCache.get(cacheKey);
  if (
    cached &&
    cached.pluginMtime === pluginMtime &&
    cached.baseMtime === baseMtime
  ) {
    return hydrateIdentity ? hydrateUserAgent({ ...cached.config }, cached.properties) : { ...cached.config };
  }

  const baseSchema = JSON.parse(fs.readFileSync(BASE_CONFIG_PATH, "utf8"));
  const pluginSchema =
    pluginConfigPath === BASE_CONFIG_PATH
      ? baseSchema
      : JSON.parse(fs.readFileSync(pluginConfigPath, "utf8"));
  maybeSkipUnsupportedSnapshotUrl(pluginSchema);

  const properties =
    pluginConfigPath === BASE_CONFIG_PATH
      ? baseSchema.properties || {}
      : {
          ...(baseSchema.properties || {}),
          ...(pluginSchema.properties || {}),
        };

  const config = {};
  const explicitConfigKeys = new Set();
  for (
    let idx = 0;
    idx < Math.max(Object.keys(properties).length, 1) + 1;
    idx++
  ) {
    let changed = false;
    for (const [name, prop] of Object.entries(properties)) {
      const choices = [name, ...(prop["x-aliases"] || [])];
      if (prop["x-fallback"]) {
        choices.push(prop["x-fallback"]);
      }

      let rawValue;
      for (const choice of choices) {
        if (Object.prototype.hasOwnProperty.call(process.env, choice)) {
          rawValue = process.env[choice];
          break;
        }
      }

      let value = parseConfigValue(rawValue, prop);
      if (rawValue !== undefined) explicitConfigKeys.add(name);
      // Match Python: only explicit overrides propagate through fallback chains.
      // A base default must not replace the plugin's own schema default.
      if (
        rawValue === undefined &&
        prop["x-fallback"] &&
        explicitConfigKeys.has(prop["x-fallback"])
      ) {
        value = config[prop["x-fallback"]];
        explicitConfigKeys.add(name);
      }
      value = hydrateConfigValue(value, config);
      if (config[name] !== value) {
        config[name] = value;
        changed = true;
      }
    }
    if (!changed) break;
  }

  if (!config.PERSONAS_DIR) {
    config.PERSONAS_DIR = path.join(getPlatformUserConfigDir(), "personas");
  }
  configCache.set(cacheKey, {
    config,
    properties,
    pluginMtime,
    baseMtime,
  });
  return hydrateIdentity ? hydrateUserAgent({ ...config }, properties) : { ...config };
}

function getConfig(configPath = null) {
  return loadConfig(configPath);
}

function resolveCallerConfigPath(skipFn) {
  const callerFile = getCallerFile(skipFn);
  if (!callerFile) return BASE_CONFIG_PATH;
  const callerConfigPath = path.join(
    path.dirname(path.resolve(callerFile)),
    "config.json"
  );
  return fs.existsSync(callerConfigPath) ? callerConfigPath : BASE_CONFIG_PATH;
}

function getConfigValue(name, defaultValue, skipFn) {
  try {
    const config = loadConfig(resolveCallerConfigPath(skipFn));
    if (Object.prototype.hasOwnProperty.call(config, name)) {
      return config[name];
    }
  } catch (error) {}

  if (Object.prototype.hasOwnProperty.call(process.env, name)) {
    return process.env[name];
  }
  return defaultValue;
}

function getEnv(name, defaultValue = "") {
  const value = getConfigValue(name, defaultValue, getEnv);
  if (value === null || value === undefined) return "";
  return String(value).trim();
}

function getEnvBool(name, defaultValue = false) {
  const value = getConfigValue(name, defaultValue, getEnvBool);
  if (typeof value === "boolean") return value;
  const val = String(value ?? "")
    .trim()
    .toLowerCase();
  if (["true", "1", "yes", "on"].includes(val)) return true;
  if (["false", "0", "no", "off"].includes(val)) return false;
  return defaultValue;
}

function getEnvInt(name, defaultValue = 0) {
  const value = getConfigValue(name, defaultValue, getEnvInt);
  if (typeof value === "number" && Number.isFinite(value)) return value;
  const val = parseInt(String(value ?? defaultValue), 10);
  return isNaN(val) ? defaultValue : val;
}

/**
 * Get array environment variable (JSON array or comma-separated string).
 *
 * If value starts with '[', parse as JSON array.
 * Otherwise, parse as comma-separated values.
 */
function getEnvArray(name, defaultValue = []) {
  const value = getConfigValue(name, defaultValue, getEnvArray);
  if (Array.isArray(value)) return value.map((item) => String(item));
  const val = String(value ?? "").trim();
  if (!val) return defaultValue;

  if (val.startsWith("[")) {
    try {
      const parsed = JSON.parse(val);
      if (Array.isArray(parsed)) return parsed;
    } catch (e) {
      // Warn when a value looks like JSON but fails to parse, then
      // fall through to comma-separated parsing below.
      writeFdFully(
        2,
        `[base/utils.js] Warning: ${name} looks like JSON but failed to parse: ${e.message}\n`
      );
    }
  }

  return val
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function getSnapDir() {
  const snapDir = getEnv("SNAP_DIR");
  if (snapDir) return path.resolve(snapDir);
  return path.resolve(process.cwd());
}

function getCrawlDir() {
  const crawlDir = getEnv("CRAWL_DIR");
  if (crawlDir) return path.resolve(crawlDir);
  return path.resolve(process.cwd());
}

function getLibDir() {
  const configured = (loadConfig(BASE_CONFIG_PATH).ABXPKG_LIB_DIR || "").trim();
  if (configured) return path.resolve(configured);
  return path.resolve(path.join(getPlatformUserConfigDir(), "lib"));
}

function getPersonasDir() {
  const configured = (loadConfig(BASE_CONFIG_PATH).PERSONAS_DIR || "").trim();
  if (configured) return path.resolve(configured);
  return path.resolve(path.join(getPlatformUserConfigDir(), "personas"));
}

function getNodeModulesDir() {
  const configured = getEnv("NODE_MODULES_DIR") || getEnv("NODE_MODULE_DIR");
  if (configured) return path.resolve(configured);

  const providerNodeModules = getProviderNodeModulePaths();
  if (providerNodeModules.length > 0) {
    return providerNodeModules[0];
  }
  throw new Error(
    "NODE_MODULES_DIR is required; run hooks through abxpkg/abx-dl/archivebox so provider env is resolved once and passed to the hook"
  );
}

function getProviderNodeModulePaths() {
  const paths = [];
  const addNodeModulesPath = (candidate) => {
    if (!candidate) return;
    const resolved = path.resolve(candidate);
    if (!paths.includes(resolved)) paths.push(resolved);
  };

  for (const entry of (process.env.NODE_PATH || "").split(path.delimiter)) {
    if (entry) addNodeModulesPath(entry);
  }
  for (const entry of [
    process.env.NODE_MODULES_DIR,
    process.env.NODE_MODULE_DIR,
  ]) {
    if (entry) addNodeModulesPath(entry);
  }

  const pnpmHome = process.env.PNPM_HOME;
  if (pnpmHome && path.basename(pnpmHome) === ".bin") {
    const nodeModulesDir = path.dirname(pnpmHome);
    if (path.basename(nodeModulesDir) === "node_modules") {
      addNodeModulesPath(nodeModulesDir);
    }
  }

  for (const entry of (process.env.PATH || "").split(path.delimiter)) {
    if (!entry || path.basename(entry) !== ".bin") continue;
    const nodeModulesDir = path.dirname(entry);
    if (path.basename(nodeModulesDir) === "node_modules") {
      addNodeModulesPath(nodeModulesDir);
    }
  }
  return paths;
}

function getMachineType() {
  if (process.env.MACHINE_TYPE) {
    return process.env.MACHINE_TYPE;
  }

  let machine = process.arch;
  const system = process.platform;

  if (machine === "arm64" || machine === "aarch64") {
    machine = "arm64";
  } else if (machine === "x64" || machine === "x86_64" || machine === "amd64") {
    machine = "x86_64";
  } else if (machine === "ia32" || machine === "x86") {
    machine = "x86";
  }

  return `${machine}-${system}`;
}

function getTestEnv() {
  const config = loadConfig(BASE_CONFIG_PATH);
  const libDir = getLibDir();
  const nodeModulesDir = getNodeModulesDir();
  const nodePath = getEnv("NODE_PATH") || nodeModulesDir;
  const pnpmBinDir = getEnv("PNPM_HOME");
  if (!pnpmBinDir) {
    throw new Error(
      "PNPM_HOME is required; run tests through fixtures that use abxpkg provider env"
    );
  }

  return {
    SNAP_DIR: getSnapDir(),
    CRAWL_DIR: getCrawlDir(),
    PERSONAS_DIR: getPersonasDir(),
    ACTIVE_PERSONA: config.ACTIVE_PERSONA || "Default",
    MACHINE_TYPE: getMachineType(),
    ABXPKG_LIB_DIR: libDir,
    NODE_MODULES_DIR: nodeModulesDir,
    NODE_PATH: nodePath,
    PNPM_BIN_DIR: pnpmBinDir,
    NPM_BIN_DIR: pnpmBinDir,
  };
}

function ensureNodeModuleResolution(moduleRef = module) {
  const Module = require("module");
  const nodeModulesDir = getNodeModulesDir();
  const nodePaths = getProviderNodeModulePaths();
  if (!nodePaths.includes(nodeModulesDir)) {
    nodePaths.unshift(nodeModulesDir);
  }

  if (!process.env.NODE_MODULES_DIR && process.env.NODE_MODULE_DIR) {
    process.env.NODE_MODULES_DIR = process.env.NODE_MODULE_DIR;
  }
  if (!process.env.NODE_MODULE_DIR && process.env.NODE_MODULES_DIR) {
    process.env.NODE_MODULE_DIR = process.env.NODE_MODULES_DIR;
  }
  process.env.NODE_MODULES_DIR = process.env.NODE_MODULES_DIR || nodeModulesDir;
  process.env.NODE_MODULE_DIR = process.env.NODE_MODULE_DIR || nodeModulesDir;
  process.env.NODE_PATH = nodePaths.join(path.delimiter);
  Module._initPaths();

  for (const nodePath of nodePaths.reverse()) {
    if (!moduleRef.paths.includes(nodePath)) {
      moduleRef.paths.unshift(nodePath);
    }
  }

  return nodeModulesDir;
}

// ---------------------------------------------------------------------------
// CLI argument parsing
// ---------------------------------------------------------------------------

/**
 * Parse --key=value arguments from process.argv.
 * Returns an object with keys (dashes converted to underscores).
 */
function parseArgs() {
  const args = {};
  process.argv.slice(2).forEach((arg) => {
    if (arg.startsWith("--")) {
      const [key, ...valueParts] = arg.slice(2).split("=");
      args[key.replace(/-/g, "_")] = valueParts.join("=") || true;
    }
  });
  return args;
}

// ---------------------------------------------------------------------------
// JSONL record emission
// ---------------------------------------------------------------------------

function parseExtraContext(raw, source) {
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed;
    }
    writeFdFully(
      2,
      `[base/utils.js] Warning: ignoring non-object extra context from ${source}\n`
    );
  } catch (error) {
    writeFdFully(
      2,
      `[base/utils.js] Warning: ignoring invalid extra context from ${source}: ${error.message}\n`
    );
  }
  return {};
}

function getExtraContext() {
  const context = {};
  const envRaw = (loadConfig(BASE_CONFIG_PATH).EXTRA_CONTEXT || "").trim();
  if (envRaw) {
    Object.assign(context, parseExtraContext(envRaw, "EXTRA_CONTEXT"));
  }

  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--extra-context") {
      const value = argv[i + 1];
      if (value === undefined) {
        writeFdFully(
          2,
          "[base/utils.js] Warning: ignoring missing value for --extra-context\n"
        );
        return context;
      }
      Object.assign(context, parseExtraContext(value, "--extra-context"));
      return context;
    }
    if (arg.startsWith("--extra-context=")) {
      Object.assign(
        context,
        parseExtraContext(
          arg.slice("--extra-context=".length),
          "--extra-context"
        )
      );
      return context;
    }
  }

  return context;
}

function mergeExtraContext(record) {
  const extraContext = getExtraContext();
  if (!Object.keys(extraContext).length) {
    return record;
  }
  return { ...extraContext, ...record };
}

function emitArchiveResultRecord(status, outputStr, extra = {}) {
  writeFdFully(
    1,
    `${JSON.stringify(
      mergeExtraContext({
        type: "ArchiveResult",
        status,
        output_str: outputStr,
        ...extra,
      })
    )}\n`
  );
}

function emitSnapshotRecord(record) {
  const snapshotRecord = mergeExtraContext({
    type: "Snapshot",
    ...record,
  });
  snapshotRecord.id = snapshotRecord.id ? String(snapshotRecord.id) : "";
  writeFdFully(1, `${JSON.stringify(snapshotRecord)}\n`);
}

// ---------------------------------------------------------------------------
// Atomic file writing
// ---------------------------------------------------------------------------

function writeFileAtomic(filePath, contents) {
  const dir = path.dirname(filePath);
  const base = path.basename(filePath);
  const tmpPath = path.join(dir, `.${base}.${process.pid}.tmp`);
  fs.writeFileSync(tmpPath, contents, "utf8");
  fs.renameSync(tmpPath, filePath);
}

// ---------------------------------------------------------------------------
// Sibling plugin output checking
// ---------------------------------------------------------------------------

// Match Python's is_non_html_document, including 2xx attachment navigations.
function isNonHtmlDocument(navigationPath = "../chrome/navigation.json") {
  try {
    const navigation = JSON.parse(fs.readFileSync(navigationPath, "utf8"));
    if (!navigation || typeof navigation.content_type !== "string") return false;
    if (
      navigation.error &&
      !(
        String(navigation.error).includes("ERR_ABORTED") &&
        Number.isInteger(navigation.status) &&
        navigation.status >= 200 &&
        navigation.status < 300
      )
    ) return false;
    const mimetype = navigation.content_type.split(";", 1)[0].trim().toLowerCase();
    return mimetype.includes("/") && !["text/html", "application/xhtml+xml"].includes(mimetype);
  } catch (error) {
    return false;
  }
}

function hasStaticFileOutput(staticfileDir = "../staticfile") {
  if (!fs.existsSync(staticfileDir)) return false;
  const resultPath = path.join(staticfileDir, "result.json");
  if (fs.existsSync(resultPath)) {
    try {
      const result = JSON.parse(fs.readFileSync(resultPath, "utf8"));
      if (result?.type !== "ArchiveResult" || result.status !== "succeeded" ||
          typeof result.output_str !== "string" || path.isAbsolute(result.output_str)) return false;
      const snapshot = fs.realpathSync(path.resolve(staticfileDir, ".."));
      const output = fs.realpathSync(path.resolve(snapshot, result.output_str));
      const relative = path.relative(snapshot, output);
      if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) return false;
      const stat = fs.statSync(output);
      return stat.isFile() && stat.size > 0;
    } catch (_) {
      return false;
    }
  }
  const stdoutPath = path.join(staticfileDir, "stdout.log");
  if (!fs.existsSync(stdoutPath)) return false;
  const stdout = fs.readFileSync(stdoutPath, "utf8");
  for (const line of stdout.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("{")) continue;
    try {
      const record = JSON.parse(trimmed);
      if (record.type === "ArchiveResult" && record.status === "succeeded") {
        return true;
      }
    } catch (e) {}
  }
  return false;
}

function iterStaticfileTextInputs(snapDir = null) {
  const base = path.resolve(snapDir || process.env.SNAP_DIR || ".");
  const staticfileDir = path.join(base, "staticfile");
  if (!fs.existsSync(staticfileDir)) return [];
  return fs
    .readdirSync(staticfileDir)
    .filter((name) => name.endsWith(".txt"))
    .map((name) => path.join(staticfileDir, name))
    .filter((filePath) => {
      try {
        return fs.statSync(filePath).isFile() && fs.statSync(filePath).size > 0;
      } catch (error) {
        return false;
      }
    })
    .sort();
}

module.exports = {
  PROCESS_EXIT_SKIPPED,
  isAutomaticUserAgent,
  parseChromiumUserAgentVersion,
  replaceChromeUserAgentVersion,
  getConfig,
  loadConfig,
  getEnv,
  getEnvBool,
  getEnvInt,
  getEnvArray,
  getSnapDir,
  getCrawlDir,
  getLibDir,
  getPersonasDir,
  getNodeModulesDir,
  getMachineType,
  getTestEnv,
  ensureNodeModuleResolution,
  parseArgs,
  normalizeUrl,
  emitArchiveResultRecord,
  emitSnapshotRecord,
  writeFileAtomic,
  hasStaticFileOutput,
  isNonHtmlDocument,
  iterStaticfileTextInputs,
};

if (require.main === module) {
  const [command] = process.argv.slice(2);
  switch (command) {
    case "getMachineType":
      console.log(getMachineType());
      break;
    case "getLibDir":
      console.log(getLibDir());
      break;
    case "getNodeModulesDir":
      console.log(getNodeModulesDir());
      break;
    case "getTestEnv":
      console.log(JSON.stringify(getTestEnv(), null, 2));
      break;
    default:
      console.error(
        "Usage: utils.js <getMachineType|getLibDir|getNodeModulesDir|getTestEnv>"
      );
      process.exit(1);
  }
}
