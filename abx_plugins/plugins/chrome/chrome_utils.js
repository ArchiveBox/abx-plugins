#!/usr/bin/env node
/**
 * Chrome Browser Session Utilities
 *
 * Handles launching browser sessions and loading already-installed extensions via CDP.
 * Ported from the TypeScript implementation in archivebox.ts
 */

const fs = require("fs");
const path = require("path");
const http = require("http");
const net = require("net");
const { spawn, execFileSync } = require("child_process");

// Import generic helpers from base plugin
const {
  getEnv,
  getEnvBool,
  getEnvInt,
  getEnvArray,
  getSnapDir,
  getCrawlDir,
  getPersonasDir,
  ensureNodeModuleResolution,
  parseArgs,
  writeFileAtomic,
} = require("../base/utils.js");

ensureNodeModuleResolution(module);

const CHROME_SESSION_REQUIRED_ERROR =
  "No Chrome session found (chrome plugin must run first)";
const CHROME_PROFILE_LOCK_FILES = [
  "SingletonLock",
  "SingletonSocket",
  "SingletonCookie",
  "DevToolsActivePort",
];

function parseChromiumVersion(output) {
  const match = String(output || "").match(/(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?/);
  if (!match) return null;
  return match.slice(1, 5).map((part) => parseInt(part || "0", 10));
}

function parseChromiumUserAgentVersion(output) {
  const version = parseChromiumVersion(output);
  return version ? `${version[0]}.0.0.0` : "";
}

function replaceChromeUserAgentVersion(userAgent, browserVersionOutput) {
  const version = parseChromiumUserAgentVersion(browserVersionOutput);
  if (!version) return userAgent || "";
  return String(userAgent || "").replace(
    /(Chrome|HeadlessChrome)\/(?:\{)?\d+\.\d+\.\d+(?:\.\d+)?(?:\})?/g,
    `$1/${version}`
  );
}

function getOption(options, key, fallback) {
  return Object.prototype.hasOwnProperty.call(options, key) &&
    options[key] !== undefined
    ? options[key]
    : fallback;
}

function resolveChromeLaunchOptions(options = {}) {
  const activePersona = getEnv("ACTIVE_PERSONA", "Default") || "Default";
  const personaDir = path.join(getPersonasDir(), activePersona);
  return {
    CHROME_USER_DATA_DIR: path.join(personaDir, "chrome_profile"),
    CHROME_DOWNLOADS_DIR: path.join(personaDir, "chrome_downloads"),
    CHROME_EXTENSIONS_DIR: getExtensionsDir(),
    CHROME_RESOLUTION: getOption(
      options,
      "CHROME_RESOLUTION",
      getEnv("CHROME_RESOLUTION") || getEnv("RESOLUTION", "1440,2000")
    ),
    CHROME_USER_AGENT: getOption(
      options,
      "CHROME_USER_AGENT",
      getEnv("CHROME_USER_AGENT") || getEnv("USER_AGENT", "")
    ),
    CHROME_HEADLESS: getOption(
      options,
      "CHROME_HEADLESS",
      getEnvBool("CHROME_HEADLESS", getEnvBool("IN_DOCKER", false))
    ),
    CHROME_SANDBOX: getOption(
      options,
      "CHROME_SANDBOX",
      getEnvBool("CHROME_SANDBOX", !getEnvBool("IN_DOCKER", false))
    ),
    CHROME_CHECK_SSL_VALIDITY: getOption(
      options,
      "CHROME_CHECK_SSL_VALIDITY",
      getEnvBool(
        "CHROME_CHECK_SSL_VALIDITY",
        getEnvBool("CHECK_SSL_VALIDITY", true)
      )
    ),
    CHROME_ARGS: getOption(
      options,
      "CHROME_ARGS",
      getEnvArray("CHROME_ARGS", [])
    ),
    CHROME_ARGS_EXTRA: getOption(
      options,
      "CHROME_ARGS_EXTRA",
      getEnvArray("CHROME_ARGS_EXTRA", [])
    ),
    CHROME_LAUNCH_ATTEMPTS: getOption(
      options,
      "CHROME_LAUNCH_ATTEMPTS",
      getEnvInt("CHROME_LAUNCH_ATTEMPTS", 3)
    ),
  };
}

function getChromeSessionOptionsFromConfig(hookConfig = {}) {
  const CHROME_CDP_URL = String(hookConfig.CHROME_CDP_URL || "").trim();
  const chromeLaunchOptions = resolveChromeLaunchOptions(hookConfig);
  return {
    CHROME_CDP_URL,
    CHROME_IS_LOCAL: CHROME_CDP_URL
      ? false
      : hookConfig.CHROME_IS_LOCAL !== false,
    CHROME_USER_DATA_DIR: path.resolve(chromeLaunchOptions.CHROME_USER_DATA_DIR),
    CHROME_RESOLUTION: String(
      hookConfig.CHROME_RESOLUTION || hookConfig.RESOLUTION || "1440,2000"
    ),
    CHROME_USER_AGENT: String(
      hookConfig.CHROME_USER_AGENT || hookConfig.USER_AGENT || ""
    ),
    CHROME_HEADLESS: hookConfig.CHROME_HEADLESS !== false,
    CHROME_SANDBOX: hookConfig.CHROME_SANDBOX !== false,
    CHROME_CHECK_SSL_VALIDITY:
      hookConfig.CHROME_CHECK_SSL_VALIDITY !== false &&
      hookConfig.CHECK_SSL_VALIDITY !== false,
    CHROME_ARGS: Array.isArray(hookConfig.CHROME_ARGS)
      ? hookConfig.CHROME_ARGS
      : [],
    CHROME_ARGS_EXTRA: Array.isArray(hookConfig.CHROME_ARGS_EXTRA)
      ? hookConfig.CHROME_ARGS_EXTRA
      : [],
    CHROME_LAUNCH_ATTEMPTS: Number(hookConfig.CHROME_LAUNCH_ATTEMPTS) || 3,
    timeoutMs: (Number(hookConfig.CHROME_TIMEOUT) || 60) * 1000,
  };
}

function getExtensionsDir() {
  const configured = getEnv("CHROMEWEBSTORE_EXTENSIONS_DIR");
  if (!configured) {
    throw new Error(
      "CHROMEWEBSTORE_EXTENSIONS_DIR is required; run Chrome hooks through abxpkg/abx-dl/archivebox so provider env is resolved once and passed to the hook"
    );
  }
  return path.resolve(configured);
}

function getNodeModulesDir() {
  const configured = getEnv("NODE_MODULES_DIR");
  if (!configured) {
    throw new Error(
      "NODE_MODULES_DIR is required; run Chrome hooks through abxpkg/abx-dl/archivebox so provider env is resolved once and passed to the hook"
    );
  }
  return path.resolve(configured);
}

function chromiumVersionAtLeast(output, minimum) {
  const version = parseChromiumVersion(output);
  if (!version) return false;
  for (let idx = 0; idx < minimum.length; idx++) {
    if (version[idx] > minimum[idx]) return true;
    if (version[idx] < minimum[idx]) return false;
  }
  return true;
}

function isSupportedChromiumVersionOutput(output) {
  return chromiumVersionAtLeast(output, [149, 0, 0]);
}

function isSupportedChromiumBinary(binaryPath) {
  if (!binaryPath) return false;
  try {
    fs.accessSync(binaryPath, fs.constants.X_OK);
    return true;
  } catch (e) {
    return false;
  }
}

/**
 * Parse resolution string into width/height.
 * @param {string} resolution - Resolution string like "1440,2000"
 * @returns {{width: number, height: number}} - Parsed dimensions
 */
function parseResolution(resolution) {
  const [width, height] = resolution
    .split(",")
    .map((x) => parseInt(x.trim(), 10));
  return { width: width || 1440, height: height || 2000 };
}

function cleanupChromeProfileLockFiles(userDataDir, options = {}) {
  const { quiet = false } = options;
  if (!userDataDir) return [];

  const cleaned = [];
  for (const fileName of CHROME_PROFILE_LOCK_FILES) {
    const filePath = path.join(userDataDir, fileName);
    try {
      fs.lstatSync(filePath);
    } catch (error) {
      if (error && error.code === "ENOENT") continue;
      if (!quiet)
        console.error(
          `[!] Failed to inspect Chrome profile file ${filePath}: ${error.message}`
        );
      continue;
    }
    try {
      fs.unlinkSync(filePath);
      cleaned.push(filePath);
      if (!quiet)
        console.error(`[+] Removed stale Chrome profile file: ${filePath}`);
    } catch (error) {
      if (!quiet)
        console.error(
          `[!] Failed to remove Chrome profile file ${filePath}: ${error.message}`
        );
    }
  }
  return cleaned;
}

// ============================================================================
// PID file management
// ============================================================================

/**
 * Write PID file with specific mtime for process validation.
 * @param {string} filePath - Path to PID file
 * @param {number} pid - Process ID
 * @param {number} startTimeSeconds - Process start time in seconds
 */
function writePidWithMtime(filePath, pid, startTimeSeconds) {
  fs.writeFileSync(filePath, String(pid));
  const startTimeMs = startTimeSeconds * 1000;
  fs.utimesSync(filePath, new Date(startTimeMs), new Date(startTimeMs));
}

/**
 * Write a shell script that can re-run the Chrome command.
 * @param {string} filePath - Path to script file
 * @param {string} binary - Chrome binary path
 * @param {string[]} args - Chrome arguments
 */
function writeCmdScript(filePath, binary, args) {
  const shellQuote = (arg) => `'${String(arg).replace(/'/g, "'\\''")}'`;
  fs.writeFileSync(
    filePath,
    `#!/bin/bash\n${[binary, ...args].map(shellQuote).join(" ")}\n`
  );
  fs.chmodSync(filePath, 0o755);
}

// ============================================================================
// Port management
// ============================================================================

/**
 * Find a free port on localhost.
 * @returns {Promise<number>} - Available port number
 */
function findFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

/**
 * Wait for Chrome's DevTools port to be ready.
 * @param {number} port - Debug port number
 * @param {number} [timeout=30000] - Timeout in milliseconds
 * @returns {Promise<Object>} - Chrome version info
 */
function waitForDebugPort(port, timeout = 30000) {
  const startTime = Date.now();
  let lastFailure = "no response yet";
  const hosts = ["127.0.0.1", "::1", "localhost"];

  const normalizeWsUrl = (rawWsUrl) => {
    try {
      const parsed = new URL(rawWsUrl);
      if (!parsed.port) parsed.port = String(port);
      return parsed.toString();
    } catch (e) {
      return rawWsUrl;
    }
  };

  const probeDebugPort = (host) =>
    new Promise((resolve, reject) => {
      const req = http.request(
        {
          host,
          port,
          path: "/json/version",
          method: "GET",
          headers: {
            Host: `${host}:${port}`,
            Connection: "close",
          },
          timeout: 5000,
        },
        (res) => {
          let data = "";
          res.on("data", (chunk) => (data += chunk));
          res.on("end", () => {
            if ((res.statusCode || 0) >= 400) {
              reject(new Error(`HTTP ${res.statusCode}`));
              return;
            }
            try {
              const info = JSON.parse(data);
              if (!info?.webSocketDebuggerUrl) {
                reject(
                  new Error(
                    "missing webSocketDebuggerUrl in /json/version response"
                  )
                );
                return;
              }
              info.webSocketDebuggerUrl = normalizeWsUrl(
                info.webSocketDebuggerUrl
              );
              resolve(info);
            } catch (error) {
              reject(
                new Error(`invalid /json/version payload: ${error.message}`)
              );
            }
          });
        }
      );
      req.on("error", reject);
      req.on("timeout", () => {
        req.destroy(new Error("request timeout"));
      });
      req.end();
    });

  return new Promise((resolve, reject) => {
    const tryConnect = async () => {
      if (Date.now() - startTime > timeout) {
        reject(
          new Error(
            `Timeout waiting for Chrome debug port ${port} (${lastFailure})`
          )
        );
        return;
      }

      for (const host of hosts) {
        try {
          const info = await probeDebugPort(host);
          resolve(info);
          return;
        } catch (error) {
          lastFailure = `${host}: ${error.message}`;
        }
      }

      setTimeout(tryConnect, 100);
    };

    tryConnect();
  });
}

function fetchDebugJson(port, pathName, timeout = 5000) {
  const hosts = ["127.0.0.1", "::1", "localhost"];

  const probeHost = (host) =>
    new Promise((resolve, reject) => {
      const req = http.request(
        {
          host,
          port,
          path: pathName,
          method: "GET",
          headers: {
            Host: `${host}:${port}`,
            Connection: "close",
          },
          timeout,
        },
        (res) => {
          let data = "";
          res.on("data", (chunk) => (data += chunk));
          res.on("end", () => {
            if ((res.statusCode || 0) >= 400) {
              reject(new Error(`HTTP ${res.statusCode}`));
              return;
            }
            try {
              resolve(JSON.parse(data));
            } catch (error) {
              reject(
                new Error(`invalid ${pathName} payload: ${error.message}`)
              );
            }
          });
        }
      );
      req.on("error", reject);
      req.on("timeout", () => {
        req.destroy(new Error("request timeout"));
      });
      req.end();
    });

  return new Promise((resolve, reject) => {
    let remaining = hosts.length;
    let lastFailure = "no response yet";
    for (const host of hosts) {
      probeHost(host)
        .then(resolve)
        .catch((error) => {
          lastFailure = `${host}: ${error.message}`;
          remaining -= 1;
          if (remaining === 0) {
            reject(new Error(lastFailure));
          }
        });
    }
  });
}

async function waitForDebugTargetsStable(port, timeout = 30000, stableMs = 500) {
  if (stableMs <= 0) return;

  const startedAt = Date.now();
  let lastSignature = null;
  let stableSince = 0;
  let lastFailure = "target list not stable yet";

  while (Date.now() - startedAt <= timeout) {
    try {
      const targets = await fetchDebugJson(port, "/json/list", 5000);
      const signature = JSON.stringify(
        (Array.isArray(targets) ? targets : [])
          .map((target) => ({
            id: target.id,
            type: target.type,
            url: target.url,
            attached: target.attached,
          }))
          .sort((left, right) =>
            String(left.id).localeCompare(String(right.id))
          )
      );
      if (signature === lastSignature) {
        if (!stableSince) stableSince = Date.now();
        if (Date.now() - stableSince >= stableMs) {
          return;
        }
      } else {
        lastSignature = signature;
        stableSince = Date.now();
      }
    } catch (error) {
      lastFailure = error?.message || String(error);
      lastSignature = null;
      stableSince = 0;
    }
    await sleep(100);
  }

  throw new Error(
    `Timeout waiting for Chrome DevTools targets to stabilize (${lastFailure})`
  );
}

// ============================================================================
// Zombie process cleanup
// ============================================================================

/**
 * Kill zombie Chrome processes from stale crawls.
 * Recursively scans SNAP_DIR for any .../chrome/chrome.pid files whose owning
 * crawl no longer has a live ``.heartbeat.json`` lease.
 * @param {string} [snapDir] - Snapshot directory (defaults to SNAP_DIR env or cwd)
 * @param {Object} [options={}] - Cleanup options
 * @param {string[]} [options.excludeCrawlDirs=[]] - Crawl directories to never treat as stale
 * @param {boolean} [options.excludeCurrentRuntimeDirs=true] - Whether to auto-skip the current CRAWL_DIR/SNAP_DIR
 * @param {string|null} [options.CHROME_USER_DATA_DIR=null] - Active Chrome profile dir whose stale lock files may be cleared
 * @returns {number} - Number of zombies killed
 */
async function killZombieChrome(snapDir = null, options = {}) {
  snapDir = snapDir || getSnapDir();
  let killed = 0;
  const currentPid = process.pid;
  const quiet = Boolean(options.quiet);
  const activeUserDataDir = options.CHROME_USER_DATA_DIR
    ? String(options.CHROME_USER_DATA_DIR).trim()
    : "";
  const excludeCurrentRuntimeDirs = options.excludeCurrentRuntimeDirs !== false;
  const excludeCrawlDirs = new Set(
    (options.excludeCrawlDirs || []).map((dir) => path.resolve(dir))
  );
  const excludeSessionDirs = new Set(
    (options.excludeSessionDirs || []).map((dir) => path.resolve(dir))
  );
  const resolvedSnapRoot = path.resolve(snapDir);
  if (excludeCurrentRuntimeDirs) {
    excludeSessionDirs.add(path.resolve(getSnapDir()));
    excludeSessionDirs.add(path.resolve(getCrawlDir()));
  }

  if (!quiet) console.error("[*] Checking for zombie Chrome processes...");

  if (!fs.existsSync(snapDir)) {
    if (!quiet) console.error("[+] No snapshot directory found");
    return 0;
  }

  function pathIsWithinSnapRoot(dir) {
    const relativePath = path.relative(resolvedSnapRoot, path.resolve(dir));
    return (
      relativePath === "" ||
      (relativePath &&
        !relativePath.startsWith("..") &&
        !path.isAbsolute(relativePath))
    );
  }

  function findOwningCrawlDir(sessionDir) {
    let currentDir = path.resolve(sessionDir);
    while (pathIsWithinSnapRoot(currentDir)) {
      if (fs.existsSync(path.join(currentDir, ".heartbeat.json"))) {
        return currentDir;
      }
      if (
        excludeCrawlDirs.has(currentDir) ||
        excludeSessionDirs.has(currentDir)
      ) {
        return currentDir;
      }
      const parentDir = path.dirname(currentDir);
      if (parentDir === currentDir) {
        break;
      }
      currentDir = parentDir;
    }
    return path.resolve(sessionDir);
  }

  function crawlHeartbeatIsAlive(crawlDir) {
    const heartbeatFile = path.join(crawlDir, ".heartbeat.json");
    try {
      const heartbeat = JSON.parse(fs.readFileSync(heartbeatFile, "utf8"));
      const ownerPid = parseInt(String(heartbeat.owner_pid), 10);
      const lastAliveAt = Number(heartbeat.last_alive_at);
      const killAfterSeconds = Number(heartbeat.kill_after_seconds || 180);
      if (isNaN(ownerPid) || ownerPid <= 0 || !Number.isFinite(lastAliveAt)) {
        return false;
      }
      if (!isProcessAlive(ownerPid)) {
        return false;
      }
      return Date.now() / 1000 - lastAliveAt <= killAfterSeconds;
    } catch (error) {
      return false;
    }
  }

  function getHeartbeatOwnerPid(crawlDir) {
    const heartbeatFile = path.join(crawlDir, ".heartbeat.json");
    try {
      const heartbeat = JSON.parse(fs.readFileSync(heartbeatFile, "utf8"));
      const ownerPid = parseInt(String(heartbeat.owner_pid), 10);
      return Number.isNaN(ownerPid) || ownerPid <= 0 ? null : ownerPid;
    } catch (error) {
      return null;
    }
  }

  function findChromeRuntimeFiles(dir, depth = 0, results = null) {
    if (depth > 10) return results || { chromePids: [], hookPids: [], personaDirs: [] };

    const found = results || { chromePids: [], hookPids: [], personaDirs: [] };
    const normalizeHookPidFileName = (fileName) =>
      fileName.slice(0, -4).replace(/\.[0-9a-f]{32}$/i, "");

    try {
      const entries = fs.readdirSync(dir, { withFileTypes: true });
      for (const entry of entries) {
        if (!entry.isDirectory()) continue;
        const fullPath = path.join(dir, entry.name);

        if (entry.name === "chrome") {
          const crawlDir = dir;
          const chromePidFile = path.join(fullPath, "chrome.pid");
          try {
            if (fs.existsSync(chromePidFile)) {
              found.chromePids.push({
                pidFile: chromePidFile,
                chromeDir: fullPath,
                sessionDir: crawlDir,
              });
            }
            for (const chromeEntry of fs.readdirSync(fullPath, {
              withFileTypes: true,
            })) {
              if (!chromeEntry.isFile()) continue;
              if (!chromeEntry.name.endsWith(".pid")) continue;
              if (chromeEntry.name === "chrome.pid") continue;
              if (!chromeEntry.name.startsWith("on_")) continue;
              if (!chromeEntry.name.includes("chrome_")) continue;
              found.hookPids.push({
                pidFile: path.join(fullPath, chromeEntry.name),
                hookName: normalizeHookPidFileName(chromeEntry.name),
                chromeDir: fullPath,
                sessionDir: crawlDir,
              });
            }
          } catch (error) {
            // Skip unreadable chrome directories.
          }
          continue;
        }

        if (entry.name === ".persona") {
          found.personaDirs.push({
            personaDir: fullPath,
            sessionDir: path.dirname(fullPath),
          });
          continue;
        }

        if (!entry.name.startsWith(".") && entry.name !== "node_modules") {
          findChromeRuntimeFiles(fullPath, depth + 1, found);
        }
      }
    } catch (error) {
      // Skip unreadable directories.
    }

    return found;
  }

  function getParentPid(pid) {
    try {
      const output = execFileSync("ps", ["-o", "ppid=", "-p", String(pid)], {
        encoding: "utf8",
        timeout: 5000,
        stdio: ["ignore", "pipe", "ignore"],
      }).trim();
      const parentPid = parseInt(output, 10);
      return Number.isNaN(parentPid) || parentPid <= 0 ? null : parentPid;
    } catch (error) {
      return null;
    }
  }

  function processHasAncestorPid(pid, ancestorPid) {
    if (!ancestorPid || !isProcessAlive(ancestorPid)) {
      return false;
    }
    const seen = new Set();
    let currentPid = pid;
    while (currentPid && !seen.has(currentPid)) {
      if (currentPid === ancestorPid) {
        return true;
      }
      seen.add(currentPid);
      currentPid = getParentPid(currentPid);
    }
    return false;
  }

  function getProcessCommand(pid) {
    try {
      return execFileSync("ps", ["-o", "command=", "-p", String(pid)], {
        encoding: "utf8",
        timeout: 5000,
        stdio: ["ignore", "pipe", "ignore"],
      }).trim();
    } catch (error) {
      return "";
    }
  }

  function getProcessWorkingDir(pid) {
    const procCwdPath = `/proc/${pid}/cwd`;
    try {
      if (fs.existsSync(procCwdPath)) {
        return path.resolve(fs.readlinkSync(procCwdPath));
      }
    } catch (error) {
      // Fall back to lsof on platforms without /proc, e.g. macOS.
    }

    try {
      const output = execFileSync(
        "lsof",
        ["-a", "-p", String(pid), "-d", "cwd", "-Fn"],
        {
          encoding: "utf8",
          timeout: 500,
          stdio: ["ignore", "pipe", "ignore"],
        }
      );
      for (const line of output.split("\n")) {
        if (line.startsWith("n")) {
          return path.resolve(line.slice(1).trim());
        }
      }
    } catch (error) {
      return null;
    }
    return null;
  }

  function findChromeHookProcesses() {
    try {
      const output = execFileSync("ps", ["-axo", "pid=,command="], {
        encoding: "utf8",
        timeout: 5000,
      });
      const hookMatches = [];
      for (const line of output.split("\n")) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        const match = trimmed.match(/^(\d+)\s+(.*)$/);
        if (!match) continue;
        const pid = parseInt(match[1], 10);
        const command = match[2];
        if (Number.isNaN(pid) || pid <= 0) continue;
        if (command.includes("on_CrawlSetup__90_chrome_launch.daemon.bg.js")) {
          hookMatches.push({
            pid,
            hookName: "on_CrawlSetup__90_chrome_launch.daemon.bg",
          });
          continue;
        }
        if (command.includes("on_Snapshot__09_chrome_launch.daemon.bg.js")) {
          hookMatches.push({
            pid,
            hookName: "on_Snapshot__09_chrome_launch.daemon.bg",
          });
          continue;
        }
        if (command.includes("on_Snapshot__10_chrome_tab.daemon.bg.js")) {
          hookMatches.push({
            pid,
            hookName: "on_Snapshot__10_chrome_tab.daemon.bg",
          });
        }
      }
      return hookMatches;
    } catch (error) {
      return [];
    }
  }

  let chromeHookProcesses = null;
  const hookWorkingDirCache = new Map();

  function getChromeHookProcesses() {
    if (chromeHookProcesses === null) {
      chromeHookProcesses = findChromeHookProcesses();
    }
    return chromeHookProcesses;
  }

  function getChromeHookWorkingDir(pid) {
    if (!hookWorkingDirCache.has(pid)) {
      hookWorkingDirCache.set(pid, getProcessWorkingDir(pid));
    }
    return hookWorkingDirCache.get(pid);
  }

  function crawlHasLiveChromeHook(crawlDir) {
    const resolvedCrawlDir = path.resolve(crawlDir);
    for (const { pid } of getChromeHookProcesses()) {
      if (pid === currentPid || !isProcessAlive(pid)) {
        continue;
      }
      const currentWorkingDir = getChromeHookWorkingDir(pid);
      if (!currentWorkingDir) {
        continue;
      }
      const sessionDir =
        path.basename(currentWorkingDir) === "chrome"
          ? path.dirname(currentWorkingDir)
          : currentWorkingDir;
      if (findOwningCrawlDir(sessionDir) === resolvedCrawlDir) {
        return true;
      }
    }
    return false;
  }

  async function killHookProcess(pid, expectedHookName) {
    const currentCommand = getProcessCommand(pid);
    if (!currentCommand || !currentCommand.includes(expectedHookName)) {
      return false;
    }

    try {
      process.kill(pid, "SIGTERM");
    } catch (error) {
      if (error.code !== "ESRCH") {
        console.error(
          `[!] Failed to SIGTERM hook PID ${pid}: ${error.message}`
        );
      }
    }

    const deadline = Date.now() + 1000;
    while (Date.now() < deadline) {
      if (!isProcessAlive(pid)) {
        return true;
      }
      await sleep(200);
    }

    if (isProcessAlive(pid)) {
      try {
        process.kill(pid, "SIGKILL");
      } catch (error) {
        if (error.code !== "ESRCH") {
          console.error(
            `[!] Failed to SIGKILL hook PID ${pid}: ${error.message}`
          );
        }
      }
    }

    const killDeadline = Date.now() + 1000;
    while (Date.now() < killDeadline) {
      if (!isProcessAlive(pid)) {
        return true;
      }
      await sleep(200);
    }

    return !isProcessAlive(pid);
  }

  try {
    const { chromePids, hookPids, personaDirs } = findChromeRuntimeFiles(snapDir);
    const handledHookPids = new Set();

    for (const { pidFile, chromeDir, sessionDir } of chromePids) {
      const resolvedCrawlDir = findOwningCrawlDir(sessionDir);

      if (excludeCrawlDirs.has(resolvedCrawlDir)) {
        continue;
      }
      if (excludeSessionDirs.has(resolvedCrawlDir)) {
        continue;
      }
      if (crawlHeartbeatIsAlive(resolvedCrawlDir)) {
        continue;
      }
      if (crawlHasLiveChromeHook(resolvedCrawlDir)) {
        continue;
      }

      // Crawl is stale, check PID
      try {
        const pid = parseInt(fs.readFileSync(pidFile, "utf8").trim(), 10);
        if (isNaN(pid) || pid <= 0) continue;

        // Check if process exists
        try {
          process.kill(pid, 0);
        } catch (e) {
          // Process dead, remove stale PID file
          try {
            fs.unlinkSync(pidFile);
          } catch (e) {}
          continue;
        }

        // Process alive and crawl is stale - zombie!
        if (!quiet)
          console.error(
            `[!] Found zombie (PID ${pid}) from stale crawl ${path.basename(
              resolvedCrawlDir
            )}`
          );

        try {
          if (await killChrome(pid, chromeDir)) {
            killed++;
            if (!quiet) console.error(`[+] Killed zombie (PID ${pid})`);
          } else if (!quiet) {
            console.error(`[!] Failed to fully kill zombie (PID ${pid})`);
          }
          try {
            fs.unlinkSync(pidFile);
          } catch (e) {}
        } catch (e) {
          if (!quiet)
            console.error(`[!] Failed to kill PID ${pid}: ${e.message}`);
        }
      } catch (e) {
        // Skip invalid PID files
      }
    }

    for (const { pidFile, hookName, sessionDir } of hookPids) {
      const resolvedCrawlDir = findOwningCrawlDir(sessionDir);

      try {
        const pid = parseInt(fs.readFileSync(pidFile, "utf8").trim(), 10);
        if (isNaN(pid) || pid <= 0) continue;
        if (pid === currentPid) continue;
        if (!isProcessAlive(pid)) {
          try {
            fs.unlinkSync(pidFile);
          } catch (error) {}
          continue;
        }
        handledHookPids.add(pid);
        if (crawlHeartbeatIsAlive(resolvedCrawlDir)) {
          continue;
        }

        if (!quiet) {
          console.error(
            `[!] Found stale chrome hook ${hookName} (PID ${pid}) from crawl ${path.basename(
              resolvedCrawlDir
            )}`
          );
        }
        if (await killHookProcess(pid, hookName)) {
          killed++;
          if (!quiet) {
            console.error(
              `[+] Killed stale chrome hook ${hookName} (PID ${pid})`
            );
          }
          try {
            fs.unlinkSync(pidFile);
          } catch (error) {}
        } else if (!quiet) {
          console.error(
            `[!] Failed to kill stale chrome hook ${hookName} (PID ${pid})`
          );
        }
      } catch (error) {
        // Skip invalid PID files
      }
    }

    for (const { pid, hookName } of getChromeHookProcesses()) {
      if (handledHookPids.has(pid)) {
        continue;
      }
      if (pid === currentPid) {
        continue;
      }
      const currentWorkingDir = getChromeHookWorkingDir(pid);
      if (!currentWorkingDir) {
        continue;
      }
      const sessionDir =
        path.basename(currentWorkingDir) === "chrome"
          ? path.dirname(currentWorkingDir)
          : currentWorkingDir;
      if (!pathIsWithinSnapRoot(sessionDir)) {
        continue;
      }
      const resolvedCrawlDir = findOwningCrawlDir(sessionDir);
      if (crawlHeartbeatIsAlive(resolvedCrawlDir)) {
        continue;
      }
      if (!quiet) {
        console.error(
          `[!] Found orphaned chrome hook ${hookName} (PID ${pid}) from crawl ${path.basename(
            resolvedCrawlDir
          )}`
        );
      }
      if (await killHookProcess(pid, hookName)) {
        killed++;
        if (!quiet) {
          console.error(
            `[+] Killed orphaned chrome hook ${hookName} (PID ${pid})`
          );
        }
      } else if (!quiet) {
        console.error(
          `[!] Failed to kill orphaned chrome hook ${hookName} (PID ${pid})`
        );
      }
    }

    for (const { personaDir, sessionDir } of personaDirs) {
      const resolvedCrawlDir = findOwningCrawlDir(sessionDir);
      if (
        excludeCrawlDirs.has(resolvedCrawlDir) ||
        excludeSessionDirs.has(resolvedCrawlDir)
      ) {
        continue;
      }
      if (crawlHeartbeatIsAlive(resolvedCrawlDir)) {
        continue;
      }
      if (crawlHasLiveChromeHook(resolvedCrawlDir)) {
        continue;
      }
      try {
        fs.rmSync(personaDir, { recursive: true, force: true });
        if (!quiet) {
          console.error(`[+] Removed stale runtime persona dir: ${personaDir}`);
        }
      } catch (error) {
        if (!quiet) {
          console.error(
            `[!] Failed to remove stale runtime persona dir ${personaDir}: ${error.message}`
          );
        }
      }
    }
  } catch (e) {
    if (!quiet)
      console.error(`[!] Error scanning for Chrome processes: ${e.message}`);
  }

  if (killed > 0) {
    if (!quiet) console.error(`[+] Killed ${killed} zombie process(es)`);
  } else {
    if (!quiet) console.error("[+] No zombies found");
  }

  if (activeUserDataDir) {
    cleanupChromeProfileLockFiles(path.resolve(activeUserDataDir), { quiet });
  }

  return killed;
}

// ============================================================================
// Chrome launching
// ============================================================================

/**
 * Launch Chromium and return the live browser process + browser-level CDP endpoint.
 *
 * This helper only performs process startup and debug-port verification. The
 * caller publishes the browser-level CDP endpoint as soon as this returns; later
 * runtime setup such as extension loading publishes its own readiness metadata.
 *
 * @param {Object} options - Launch options
 * @param {string} [options.binary] - Chrome binary path (auto-detected if not provided)
 * @param {string} [options.outputDir='chrome'] - Directory for output files
 * @param {string} [options.CHROME_USER_DATA_DIR] - Chrome user data directory for persistent sessions
 * @param {string} [options.CHROME_RESOLUTION='1440,2000'] - Window resolution
 * @param {string} [options.CHROME_USER_AGENT=''] - User agent string
 * @param {boolean} [options.CHROME_HEADLESS=true] - Run in headless mode
 * @param {boolean} [options.CHROME_SANDBOX=true] - Enable Chrome sandbox
 * @param {boolean} [options.CHROME_CHECK_SSL_VALIDITY=true] - Check SSL certificates
 * @param {boolean} [options.enableExtensionDebugging=false] - Enable CDP extension loading/debugging
 * @param {Array<string>} [options.extensionPaths=[]] - Unpacked extension paths to load after launch via CDP Extensions.loadUnpacked
 * @param {Array<string>} [options.CHROME_ARGS=[]] - Hydrated base Chrome args from plugin config
 * @param {Array<string>} [options.CHROME_ARGS_EXTRA=[]] - Hydrated extra Chrome args from plugin config
 * @param {number} [options.CHROME_LAUNCH_ATTEMPTS=3] - Hydrated launch retry count from plugin config
 * @param {number} [options.timeoutMs] - Hydrated Chrome operation timeout in milliseconds
 * @returns {Promise<Object>} - {success, cdpUrl, pid, port, process, error}
 */
async function launchChromium(options = {}) {
  const {
    binary = findChromium(),
    outputDir = "chrome",
    enableExtensionDebugging = false,
    extensionPaths = [],
    timeoutMs = getEnvInt("CHROME_TIMEOUT", 60) * 1000,
  } = options;
  const {
    CHROME_USER_DATA_DIR,
    CHROME_RESOLUTION,
    CHROME_USER_AGENT,
    CHROME_HEADLESS,
    CHROME_SANDBOX,
    CHROME_CHECK_SSL_VALIDITY,
    CHROME_ARGS,
    CHROME_ARGS_EXTRA,
    CHROME_LAUNCH_ATTEMPTS,
  } = resolveChromeLaunchOptions(options);
  const launchAttempts = Math.max(1, Number(CHROME_LAUNCH_ATTEMPTS) || 1);
  const userDataDir = CHROME_USER_DATA_DIR;

  if (!binary) {
    return { success: false, error: "Chrome binary not found" };
  }
  if (!isSupportedChromiumBinary(binary)) {
    return {
      success: false,
      error: `Chrome binary is not executable: ${binary}`,
    };
  }

  const { width, height } = parseResolution(CHROME_RESOLUTION);
  const chromeUserAgent = CHROME_USER_AGENT;

  // Create output directory
  if (!fs.existsSync(outputDir)) {
    fs.mkdirSync(outputDir, { recursive: true });
  }

  // Create user data directory and clear lock files left by crashed sessions.
  if (!fs.existsSync(userDataDir)) {
    fs.mkdirSync(userDataDir, { recursive: true });
    console.error(`[*] Created user data directory: ${userDataDir}`);
  }
  cleanupChromeProfileLockFiles(userDataDir);

  // Find a free port
  const debugPort = await findFreePort();
  console.error(`[*] Using debug port: ${debugPort}`);

  // Get base Chrome args from config (static flags from CHROME_ARGS env var)
  // These come from config.json defaults, merged by get_config() in Python
  const baseArgs = Array.isArray(CHROME_ARGS)
    ? CHROME_ARGS
    : getEnvArray("CHROME_ARGS", []);

  const extraArgs = Array.isArray(CHROME_ARGS_EXTRA)
    ? CHROME_ARGS_EXTRA
    : getEnvArray("CHROME_ARGS_EXTRA", []);

  // Build dynamic Chrome arguments (these must be computed at runtime)
  const dynamicArgs = [
    // Remote debugging setup
    `--remote-debugging-port=${debugPort}`,
    "--remote-debugging-address=127.0.0.1",

    // Sandbox settings
    ...(CHROME_SANDBOX ? [] : ["--no-sandbox", "--disable-setuid-sandbox"]),

    // Docker-specific workarounds
    "--disable-dev-shm-usage",

    // Window size
    `--window-size=${width},${height}`,

    // User data directory (for persistent sessions with persona)
    ...(userDataDir ? [`--user-data-dir=${userDataDir}`] : []),

    // User agent
    ...(chromeUserAgent ? [`--user-agent=${chromeUserAgent}`] : []),

    // Headless mode
    ...(CHROME_HEADLESS ? ["--headless=new"] : []),

    // SSL certificate checking
    ...(CHROME_CHECK_SSL_VALIDITY ? [] : ["--ignore-certificate-errors"]),
  ];

  // Combine all args: base (from config) + dynamic (runtime) + extra (user overrides)
  // Dynamic args come after base so they can override if needed
  const chromiumArgs = [...baseArgs, ...dynamicArgs, ...extraArgs];

  // Ensure keychain prompts are disabled on macOS
  if (!chromiumArgs.includes("--use-mock-keychain")) {
    chromiumArgs.push("--use-mock-keychain");
  }

  if (
    enableExtensionDebugging &&
    !chromiumArgs.includes("--enable-unsafe-extension-debugging")
  ) {
    chromiumArgs.push("--enable-unsafe-extension-debugging");
  }

  if (
    enableExtensionDebugging &&
    Array.isArray(extensionPaths) &&
    extensionPaths.filter(Boolean).length > 0
  ) {
    console.error(
      "[*] Loading Chrome extensions after launch with CDP Extensions.loadUnpacked"
    );
  }
  chromiumArgs.push("about:blank");

  // Write command script for debugging
  writeCmdScript(path.join(outputDir, "cmd.sh"), binary, chromiumArgs);

  const chromeLaunchLock = path.join(userDataDir, ".chrome-launch.lock");
  let lastError = "Unknown Chromium launch failure";

  // Chromium startup has two distinct phases:
  // 1. process/bootstrap: the native browser process starts, initializes the
  //    profile, binds the remote debugging port, and prints DevTools metadata
  // 2. post-port stabilization: the browser remains alive long enough for a
  //    real CDP client to attach and for the initial about:blank page to be
  //    usable
  //
  // In principle this should be deterministic, but in practice we sometimes
  // see first-launch native failures inside Chromium itself, especially when
  // using a fresh profile and/or loading unpacked extensions in headless mode.
  // Those crashes happen *after* we have already done our deterministic setup
  // (profile dir creation, SingletonLock cleanup, debug port selection, args
  // construction, launch locking), so there is no higher-level app signal we
  // can check in advance to know the first attempt will die.
  //
  // The important boundary here is that we only retry failures that clearly
  // occurred during Chromium's own early startup lifecycle:
  // - the process exits before the DevTools port is ready
  // - the process exits during the short post-launch settle window
  // - the DevTools socket opens, but a real CDP session cannot be stabilized
  //
  // We intentionally do *not* retry arbitrary failures forever. Persistent
  // config issues (bad binary path, invalid flags, broken permissions, etc.)
  // should still fail deterministically on the first attempt.
  for (let attempt = 1; attempt <= launchAttempts; attempt++) {
    let chromiumProcess = null;
    let chromePid = null;
    let recentStderr = "";
    let recentStdout = "";
    let releaseLaunchLock = null;

    try {
      releaseLaunchLock = await acquireSessionLock(
        chromeLaunchLock,
        getEnvInt("CHROME_LAUNCH_LOCK_TIMEOUT_MS", 120000)
      );
      console.error(
        `[*] Spawning Chromium (headless=${CHROME_HEADLESS}) [attempt ${attempt}/${launchAttempts}]...`
      );
      chromiumProcess = spawn(binary, chromiumArgs, {
        stdio: ["ignore", "pipe", "pipe"],
        detached: true,
      });

      chromePid = chromiumProcess.pid;
      const chromeStartTime = Date.now() / 1000;

      if (chromePid) {
        console.error(`[*] Chromium spawned (PID: ${chromePid})`);
        writePidWithMtime(
          path.join(outputDir, "chrome.pid"),
          chromePid,
          chromeStartTime
        );
      }

      const logSubprocessOutput = getEnvBool(
        "CHROME_LOG_SUBPROCESS_OUTPUT",
        false
      );
      chromiumProcess.stdout.on("data", (data) => {
        recentStdout = `${recentStdout}${String(data)}`.slice(-4000);
        if (logSubprocessOutput) {
          process.stderr.write(`[chromium:stdout] ${data}`);
        }
      });
      chromiumProcess.stderr.on("data", (data) => {
        recentStderr = `${recentStderr}${String(data)}`.slice(-4000);
        if (logSubprocessOutput) {
          process.stderr.write(`[chromium:stderr] ${data}`);
        }
      });

      // This watches the raw spawned process before we have a reliable CDP
      // session. If Chromium crashes here, all we know is the native exit
      // code/signal and a small stderr tail.
      const chromiumExit = new Promise((_, reject) => {
        chromiumProcess.once("error", (error) => {
          reject(
            new Error(`Chromium process failed to start: ${error.message}`)
          );
        });
        chromiumProcess.once("exit", (code, signal) => {
          reject(
            new Error(
              `Chromium exited before opening the debug port (code=${
                code ?? "null"
              }, signal=${signal || "none"})`
            )
          );
        });
      });
      chromiumExit.catch(() => {});

      // The DevTools port coming up is only a coarse readiness signal.
      // Chromium can still crash immediately afterwards, so we follow this
      // with verifyStableChromiumSession() before declaring success.
      console.error(`[*] Waiting for debug port ${debugPort}...`);
      const debugProbeTimeoutMs = getEnvInt(
        "CHROME_DEBUG_PORT_TIMEOUT_MS",
        30000
      );
      const versionInfo = await Promise.race([
        waitForDebugPort(debugPort, debugProbeTimeoutMs),
        chromiumExit,
      ]);
      const wsUrl = versionInfo.webSocketDebuggerUrl;

      // /json/version only proves the debugging socket is bound. The target
      // list can still churn while Chrome finishes startup pages or extension
      // background targets. Puppeteer attaches during connect, and CDP
      // correctly reports "No target with given id found" if one of those
      // early targets disappears between discovery and attach.
      await waitForDebugTargetsStable(
        debugPort,
        Math.min(timeoutMs, debugProbeTimeoutMs),
        getEnvInt("CHROME_DEBUG_TARGET_STABLE_MS", 500)
      );
      console.error(`[+] Chromium ready: ${wsUrl}`);

      const result = {
        success: true,
        cdpUrl: wsUrl,
        pid: chromePid,
        port: debugPort,
        process: chromiumProcess,
        userDataDir,
      };

      await verifyStableChromiumSession({
        chromePid,
        cdpUrl: wsUrl,
        outputDir,
        headless: CHROME_HEADLESS,
        enableExtensionDebugging,
        timeoutMs,
      });

      return result;
    } catch (e) {
      if (chromePid) {
        await cleanupLaunchArtifacts(outputDir, chromePid);
      }
      const extraOutput = [
        recentStdout ? `stdout=${recentStdout.trim()}` : "",
        recentStderr ? `stderr=${recentStderr.trim()}` : "",
      ]
        .filter(Boolean)
        .join(" ");
      lastError = extraOutput
        ? `${e.name}: ${e.message} (${extraOutput})`
        : `${e.name}: ${e.message}`;
      // Only retry failures that map to Chromium's startup/stabilization
      // window. Everything else should bubble out directly so permanent
      // misconfiguration still fails fast and loudly.
      const isTransientStartupFailure =
        lastError.includes("Chromium exited before opening the debug port") ||
        lastError.includes("Timeout waiting for Chrome debug port") ||
        lastError.includes("Chrome DevTools targets to stabilize") ||
        lastError.includes("Chromium exited during startup") ||
        lastError.includes("Chromium exited after opening the debug port") ||
        lastError.includes("Chromium CDP session not stable after startup");
      if (attempt >= launchAttempts || !isTransientStartupFailure) {
        return {
          success: false,
          error: lastError,
        };
      }
      console.error(
        `[!] Chromium launch attempt ${attempt}/${launchAttempts} failed, retrying...`
      );
      await sleep(1000);
    } finally {
      if (releaseLaunchLock) {
        releaseLaunchLock();
      }
    }
  }

  return { success: false, error: lastError };
}

/**
 * Check if a process is still running.
 * @param {number} pid - Process ID to check
 * @returns {boolean} - True if process exists
 */
function isProcessAlive(pid) {
  try {
    process.kill(pid, 0); // Signal 0 checks existence without killing
    return true;
  } catch (e) {
    return false;
  }
}

async function acquireSessionLock(
  lockFile,
  timeoutMs = 10000,
  intervalMs = 100
) {
  const startedAt = Date.now();
  const token = `${process.pid}:${startedAt}:${Math.random()
    .toString(16)
    .slice(2)}`;
  const staleLockMs = Math.max(2000, intervalMs * 10);
  fs.mkdirSync(path.dirname(lockFile), { recursive: true });

  while (Date.now() - startedAt < timeoutMs) {
    try {
      const fd = fs.openSync(lockFile, "wx");
      fs.writeFileSync(
        fd,
        JSON.stringify({
          pid: process.pid,
          token,
          createdAt: new Date().toISOString(),
        })
      );
      fs.closeSync(fd);
      return () => {
        try {
          const current = JSON.parse(fs.readFileSync(lockFile, "utf-8"));
          if (current?.token === token) {
            fs.unlinkSync(lockFile);
          }
        } catch (error) {}
      };
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
      try {
        const current = JSON.parse(fs.readFileSync(lockFile, "utf-8"));
        if (!current?.pid || !isProcessAlive(current.pid)) {
          fs.unlinkSync(lockFile);
          continue;
        }
      } catch (readError) {
        try {
          const stat = fs.statSync(lockFile);
          const ageMs = Date.now() - stat.mtimeMs;
          if (ageMs >= staleLockMs) {
            fs.unlinkSync(lockFile);
            continue;
          }
        } catch (statError) {}
      }
    }
    await sleep(intervalMs);
  }

  throw new Error(`Timeout acquiring lock: ${path.basename(lockFile)}`);
}

/**
 * Find all Chrome child processes for a given debug port.
 * @param {number} port - Debug port number
 * @returns {Array<number>} - Array of PIDs
 */
function findChromeProcessesByPort(port) {
  const debugPort = parseInt(port, 10);
  if (!Number.isInteger(debugPort) || debugPort <= 0) return [];

  const pids = [];

  try {
    const output = execFileSync("ps", ["-axo", "pid=,command="], {
      encoding: "utf8",
      timeout: 5000,
    });

    for (const line of output.split("\n")) {
      const match = line.trim().match(/^(\d+)\s+(.*)$/);
      if (!match) continue;
      const pid = parseInt(match[1], 10);
      const command = match[2].toLowerCase();
      if (
        !Number.isNaN(pid) &&
        pid > 0 &&
        (command.includes("chrome") || command.includes("chromium")) &&
        command.includes(`--remote-debugging-port=${debugPort}`)
      ) {
        pids.push(pid);
      }
    }
  } catch (e) {
    // Command failed or no processes found
  }

  return pids;
}

async function waitForChromeProcessTreeExit(
  pid,
  debugPort = null,
  timeoutMs = 5000,
  intervalMs = 200
) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const mainAlive = pid ? isProcessAlive(pid) : false;
    const relatedPids = debugPort ? findChromeProcessesByPort(debugPort) : [];
    if (!mainAlive && relatedPids.length === 0) {
      return true;
    }
    await sleep(intervalMs);
  }

  const mainAlive = pid ? isProcessAlive(pid) : false;
  const relatedPids = debugPort ? findChromeProcessesByPort(debugPort) : [];
  return !mainAlive && relatedPids.length === 0;
}

/**
 * Kill a Chrome process by PID.
 * Always sends SIGTERM before SIGKILL, then verifies death.
 *
 * @param {number} pid - Process ID to kill
 * @param {string} [outputDir] - Directory containing PID files to clean up
 */
async function killChrome(pid, outputDir = null) {
  // Get debug port for finding child processes
  let debugPort = null;
  if (outputDir) {
    try {
      const cdpFile = path.join(outputDir, "cdp_url.txt");
      if (fs.existsSync(cdpFile)) {
        debugPort = getChromeDebugPortFromCdpUrl(
          fs.readFileSync(cdpFile, "utf8").trim()
        );
      }
    } catch (e) {}
  }

  const initialRelatedPids = debugPort
    ? findChromeProcessesByPort(debugPort)
    : [];
  const hasLiveParent = Boolean(pid && isProcessAlive(pid));
  if (!hasLiveParent && initialRelatedPids.length === 0) {
    return true;
  }

  console.error(
    `[*] Killing Chrome process tree (${
      hasLiveParent ? `PID ${pid}` : `port ${debugPort}`
    })...`
  );

  // Step 1: Ask the main browser process to exit cleanly. Chromium itself is
  // responsible for shutting down its renderer/helper children without
  // corrupting the profile dir, so we only send SIGTERM to the parent.
  if (hasLiveParent) {
    console.error(`[*] Sending SIGTERM to Chrome parent process ${pid}...`);
    try {
      process.kill(pid, "SIGTERM");
    } catch (error) {
      if (error.code !== "ESRCH") {
        console.error(`[!] SIGTERM failed: ${error.message}`);
      }
    }
  }

  let processTreeExited = await waitForChromeProcessTreeExit(
    pid,
    debugPort,
    5000
  );
  if (processTreeExited) {
    console.error("[+] Chrome process tree terminated gracefully");
  } else {
    const remainingPids = new Set();
    if (pid) {
      remainingPids.add(pid);
    }
    for (const relatedPid of debugPort
      ? findChromeProcessesByPort(debugPort)
      : initialRelatedPids) {
      remainingPids.add(relatedPid);
    }

    console.error(
      `[*] Chrome did not exit cleanly in time, sending SIGKILL to ${remainingPids.size} remaining processes...`
    );
    for (const remainingPid of remainingPids) {
      if (!remainingPid || !isProcessAlive(remainingPid)) {
        continue;
      }
      try {
        process.kill(remainingPid, "SIGKILL");
      } catch (error) {
        if (error.code !== "ESRCH") {
          console.error(
            `[!] SIGKILL failed for ${remainingPid}: ${error.message}`
          );
        }
      }
    }

    processTreeExited = await waitForChromeProcessTreeExit(
      pid,
      debugPort,
      5000
    );
    if (!processTreeExited) {
      console.error(
        `[!] WARNING: Chrome process tree for PID ${pid} is still alive after SIGKILL`
      );
      console.error(
        `[!] This typically means Chromium is stuck in an uninterruptible kernel wait state`
      );
    } else {
      console.error("[+] Chrome process tree killed successfully");
    }
  }

  // Step 8: Clean up PID files
  // Note: hook-specific .pid files are cleaned up by run_hook() and Snapshot.cleanup()
  if (outputDir && processTreeExited) {
    try {
      fs.unlinkSync(path.join(outputDir, "chrome.pid"));
    } catch (e) {}
  }

  if (!processTreeExited) {
    console.error(
      "[!] Chrome cleanup completed, but some browser processes are still alive"
    );
    return false;
  }

  console.error("[*] Chrome cleanup completed");
  return true;
}

/**
 * Check if a Puppeteer target is an MV3 extension service worker.
 *
 * @param {Object} target - Puppeteer target object
 * @returns {Promise<Object>} - Object with target_is_bg, extension_id, manifest_version, etc.
 */
const CHROME_EXTENSION_URL_PREFIX = "chrome-extension://";
const EXTENSION_BACKGROUND_TARGET_TYPES = new Set(["service_worker"]);

/**
 * Parse extension ID from a target URL.
 *
 * @param {string|null|undefined} targetUrl - URL from Puppeteer target
 * @returns {string|null} - Extension ID if URL is a chrome-extension URL
 */
function getExtensionIdFromUrl(targetUrl) {
  if (!targetUrl || !targetUrl.startsWith(CHROME_EXTENSION_URL_PREFIX))
    return null;
  return (
    targetUrl.slice(CHROME_EXTENSION_URL_PREFIX.length).split("/")[0] || null
  );
}

/**
 * Filter extension list to entries with unpacked paths.
 *
 * @param {Array} extensions - Extension metadata list
 * @returns {Array} - Extensions with unpacked_path
 */
function getValidInstalledExtensions(extensions) {
  if (!Array.isArray(extensions) || extensions.length === 0) return [];
  return extensions.filter((ext) => ext?.unpacked_path);
}

async function tryGetExtensionContext(target, targetType) {
  if (targetType !== "service_worker") return null;
  return await target.worker();
}

async function waitForExtensionTargetType(
  browser,
  extensionId,
  targetType,
  timeout
) {
  const target = await browser.waitForTarget(
    (candidate) =>
      candidate.type() === targetType &&
      getExtensionIdFromUrl(candidate.url()) === extensionId,
    { timeout }
  );
  return await tryGetExtensionContext(target, targetType);
}

/**
 * Wait for a Puppeteer target handle for a specific extension id.
 *
 * @param {Object} browser - Puppeteer browser instance
 * @param {string} extensionId - Extension ID
 * @param {number} [timeout=30000] - Timeout in milliseconds
 * @param {string|null} [preferredTargetUrl=null] - Exact extension target URL to prefer
 * @returns {Promise<Object>} - Puppeteer target
 */
async function waitForExtensionTargetHandle(
  browser,
  extensionId,
  timeout = 30000,
  preferredTargetUrl = null,
  options = {}
) {
  const deadline = Date.now() + Math.max(timeout, 0);
  let lastCandidates = [];
  let wakeAttempted = false;
  const wakePath = options.wakePath || null;

  async function wakeExtension() {
    if (!wakePath || wakeAttempted) return;
    wakeAttempted = true;
    let wakePage = null;
    try {
      wakePage = await browser.newPage();
      await wakePage.goto(
        `${CHROME_EXTENSION_URL_PREFIX}${extensionId}${wakePath}`,
        {
          waitUntil: "load",
          timeout: Math.min(Math.max(deadline - Date.now(), 1000), 10000),
        }
      );
      await wakePage.evaluate(() => {
        return new Promise((resolve) => {
          const runtime = globalThis.chrome?.runtime;
          if (!runtime?.sendMessage) {
            resolve(null);
            return;
          }
          try {
            runtime.sendMessage({ method: "ping" }, (response) =>
              resolve(response || null)
            );
          } catch (error) {
            resolve(null);
          }
        });
      });
    } catch (error) {
      if (wakePage) {
        try {
          await wakePage.close();
        } catch (closeError) {}
      }
    }
  }

  while (Date.now() < deadline) {
    const candidates = browser
      .targets()
      .filter(
        (target) =>
          getExtensionIdFromUrl(target.url()) === extensionId &&
          target.type() === "service_worker"
      );

    if (preferredTargetUrl) {
      const exactMatch = candidates.find(
        (target) => target.url() === preferredTargetUrl
      );
      if (exactMatch) {
        return exactMatch;
      }
    } else {
      const backgroundTarget = candidates.find((target) =>
        EXTENSION_BACKGROUND_TARGET_TYPES.has(target.type())
      );
      if (backgroundTarget) {
        return backgroundTarget;
      }
      if (candidates.length > 0) {
        return candidates[0];
      }
    }

    lastCandidates = candidates.map(
      (target) => `${target.type()}:${target.url()}`
    );
    if (candidates.length === 0) {
      await wakeExtension();
    }
    await sleep(100);
  }

  const error = new Error(
    `Timed out waiting for extension target ${extensionId}` +
      (preferredTargetUrl ? ` (${preferredTargetUrl})` : "") +
      (lastCandidates.length ? `; last seen: ${lastCandidates.join(", ")}` : "")
  );
  error.name = "TimeoutError";
  throw error;
}

async function isTargetExtension(target, options = {}) {
  const manifestTimeoutMs = Math.max(
    250,
    Number(options.manifestTimeoutMs || 1000)
  );
  let target_type;
  let target_ctx;
  let target_url;

  try {
    target_type = target.type();
    target_ctx = (await target.worker()) || (await target.page()) || null;
    target_url = target.url() || target_ctx?.url() || null;
  } catch (err) {
    if (String(err).includes("No target with given id found")) {
      // Target closed during check, ignore harmless race condition
      target_type = "closed";
      target_ctx = null;
      target_url = "about:closed";
    } else {
      throw err;
    }
  }

  // Check if this is an MV3 extension service worker
  const extension_id = getExtensionIdFromUrl(target_url);
  const is_chrome_extension = Boolean(extension_id);
  const is_service_worker = target_type === "service_worker";
  const target_is_bg = is_chrome_extension && is_service_worker;

  let manifest_version = null;
  let manifest = null;
  let manifest_name = null;
  const target_is_extension = is_chrome_extension || target_is_bg;

  if (target_is_extension) {
    try {
      if (target_ctx) {
        manifest = await withTimeout(
          () => target_ctx.evaluate(() => chrome.runtime.getManifest()),
          manifestTimeoutMs,
          `Timed out reading manifest for extension ${extension_id}`
        );
        manifest_version = manifest?.manifest_version || null;
        manifest_name = manifest?.name || null;
      }
    } catch (err) {
      // Failed to get extension metadata
    }
  }

  return {
    target_is_extension,
    target_is_bg,
    target_type,
    target_ctx,
    target_url,
    extension_id,
    manifest_version,
    manifest,
    manifest_name,
  };
}

/**
 * Load extension metadata and connection handlers from a browser target.
 *
 * @param {Array} extensions - Array of extension metadata objects to update
 * @param {Object} target - Puppeteer target object
 * @returns {Promise<Object|null>} - Updated extension object or null if not an extension
 */
async function loadExtensionFromTarget(extensions, target, options = {}) {
  const {
    target_is_bg,
    target_is_extension,
    target_type,
    target_ctx,
    target_url,
    extension_id,
    manifest_version,
    manifest,
  } = await isTargetExtension(target, options);

  if (!(target_is_bg && extension_id && target_ctx)) {
    return null;
  }

  // Find matching extension in our list
  const extension = extensions.find((ext) => ext.id === extension_id);
  if (!extension) {
    console.warn(
      `[⚠️] Found loaded extension ${extension_id} that's not in CHROME_EXTENSIONS list`
    );
    return null;
  }

  if (!manifest) {
    console.error(`[❌] Failed to read manifest for extension ${extension_id}`);
    return null;
  }

  // Create dispatch methods for communicating with the extension
  const new_extension = {
    ...extension,
    target,
    target_type,
    target_url,
    manifest,
    manifest_version,

    // Trigger extension toolbar button click
    dispatchAction: async (tab) => {
      return await target_ctx.evaluate(async (tab) => {
        const browserApi = (typeof browser !== "undefined" && browser) || null;
        const chromeApi = (typeof chrome !== "undefined" && chrome) || null;
        const tabsApi = browserApi?.tabs || chromeApi?.tabs || null;

        if (!tab && tabsApi?.query) {
          const tabs = await tabsApi.query({
            currentWindow: true,
            active: true,
          });
          tab = tabs?.[0] || null;
        }

        if (browserApi?.action?.onClicked?.dispatch) {
          return await browserApi.action.onClicked.dispatch(tab);
        }

        if (chromeApi?.action?.onClicked?.dispatch) {
          return await chromeApi.action.onClicked.dispatch(tab);
        }

        throw new Error("Extension action dispatch not available");
      }, tab || null);
    },

    // Send message to extension
    dispatchMessage: async (message, options = {}) => {
      return await target_ctx.evaluate(
        (msg, opts) => {
          return new Promise((resolve) => {
            chrome.runtime.sendMessage(msg, opts, (response) => {
              resolve(response);
            });
          });
        },
        message,
        options
      );
    },

    // Trigger extension command (keyboard shortcut)
    dispatchCommand: async (command) => {
      return await target_ctx.evaluate((cmd) => {
        return new Promise((resolve) => {
          chrome.commands.onCommand.addListener((receivedCommand) => {
            if (receivedCommand === cmd) {
              resolve({ success: true, command: receivedCommand });
            }
          });
          // Note: Actually triggering commands programmatically is not directly supported
          // This would need to be done via CDP or keyboard simulation
        });
      }, command);
    },
  };

  // Update the extension in the array
  Object.assign(extension, new_extension);

  console.error(
    `[🔌] Connected to extension ${extension.name} (${extension.version})`
  );

  return new_extension;
}

async function loadUnpackedExtensionsIntoBrowser(
  browser,
  extensions,
  timeout = 30000
) {
  const validExtensions = getValidInstalledExtensions(extensions);
  if (validExtensions.length === 0) {
    return extensions;
  }

  console.error(
    `[⚙️] Loading ${validExtensions.length} unpacked chrome extensions into browser...`
  );
  const perExtensionTimeout = Math.max(
    250,
    getEnvInt("CHROME_EXTENSION_DISCOVERY_TIMEOUT_MS", Math.min(timeout, 2000))
  );
  let cdpSession = null;
  try {
    cdpSession = await browser.target().createCDPSession();
  } catch (error) {
    const loadError = `${error.name}: ${error.message}`;
    for (const extension of validExtensions) {
      extension.load_error = loadError;
    }
    throw new Error(
      `Extensions.loadUnpacked requires Chromium >=149.0.0 and a browser CDP session; failed to create CDP session: ${loadError}`
    );
  }

  try {
    for (const extension of validExtensions) {
      try {
        const { id } = await cdpSession.send("Extensions.loadUnpacked", {
          path: extension.unpacked_path,
        });
        if (!id) {
          throw new Error(
            `Extensions.loadUnpacked did not return an id for ${extension.unpacked_path}`
          );
        }
        extension.id = id;
        delete extension.load_error;
      } catch (error) {
        const detail = `${error.name}: ${error.message}`;
        extension.load_error = detail;
        throw new Error(
          `Failed to load Chrome extension ${
            extension.name || extension.unpacked_path
          } from ${extension.unpacked_path} via Extensions.loadUnpacked: ${detail}`
        );
      }

      try {
        const target = await waitForExtensionTargetHandle(
          browser,
          extension.id,
          perExtensionTimeout
        );
        const loaded = await withTimeout(
          () =>
            loadExtensionFromTarget(extensions, target, {
              manifestTimeoutMs: Math.min(perExtensionTimeout, 1000),
            }),
          perExtensionTimeout,
          `Timed out attaching extension target for ${extension.id}`
        );
        if (!loaded) {
          throw new Error(
            `Unable to attach extension target for ${extension.id}`
          );
        }
        delete extension.target_error;
      } catch (error) {
        const detail = `${error.name}: ${error.message}`;
        extension.target_error = detail;
        if (!extension.manifest) {
          const manifest = loadExtensionManifest(extension.unpacked_path);
          if (manifest) {
            extension.manifest = manifest;
            extension.manifest_version = manifest.manifest_version || null;
          }
        }
        console.warn(
          `[⚠️] Could not attach Chrome extension ${
            extension.name || extension.unpacked_path
          } target after Extensions.loadUnpacked returned ${
            extension.id
          }: ${detail}`
        );
      }
    }
  } finally {
    try {
      await cdpSession.detach();
    } catch (error) {}
  }

  return extensions;
}

/**
 * Load extension manifest.json file
 *
 * @param {string} unpacked_path - Path to unpacked extension directory
 * @returns {object|null} - Parsed manifest object or null if not found/invalid
 */
function loadExtensionManifest(unpacked_path) {
  const manifest_path = path.join(unpacked_path, "manifest.json");

  if (!fs.existsSync(manifest_path)) {
    return null;
  }

  try {
    const manifest_content = fs.readFileSync(manifest_path, "utf-8");
    return JSON.parse(manifest_content);
  } catch (error) {
    // Invalid JSON or read error
    return null;
  }
}

/**
 * Get unpacked extension paths for CDP Extensions.loadUnpacked.
 *
 * @param {Array} extensions - Array of extension metadata objects
 * @returns {Array<string>} - Array of extension unpacked paths
 */
function getExtensionPaths(extensions) {
  return getValidInstalledExtensions(extensions).map(
    (ext) => ext.unpacked_path
  );
}

/**
 * Wait for an extension target to be available in the browser.
 * Following puppeteer best practices for accessing extension contexts.
 *
 * For Manifest V3 extensions (service workers):
 *   const worker = await waitForExtensionTarget(browser, extensionId);
 *   // worker is a WebWorker context
 *
 * @param {Object} browser - Puppeteer browser instance
 * @param {string} extensionId - Runtime extension ID returned by Extensions.loadUnpacked
 * @param {number} [timeout=30000] - Timeout in milliseconds
 * @returns {Promise<Object>} - Worker or Page context for the extension
 */
async function waitForExtensionTarget(browser, extensionId, timeout = 30000) {
  return await waitForExtensionTargetType(
    browser,
    extensionId,
    "service_worker",
    timeout
  );
}

/**
 * Read browser setup metadata from chrome session directory.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @returns {{ready: boolean, extensions: Array<Object>}|null} - Parsed browser metadata or null if unavailable
 */
function readBrowserMetadata(chromeSessionDir) {
  const browserFile = path.join(path.resolve(chromeSessionDir), "browser.json");
  if (!fs.existsSync(browserFile)) return null;
  try {
    const parsed = JSON.parse(fs.readFileSync(browserFile, "utf8"));
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return null;
    }
    return {
      ready: parsed.ready === true,
      extensions: Array.isArray(parsed.extensions) ? parsed.extensions : [],
    };
  } catch (e) {
    return null;
  }
}

function writeBrowserMetadata(chromeSessionDir, extensions = []) {
  writeFileAtomic(
    path.join(path.resolve(chromeSessionDir), "browser.json"),
    JSON.stringify(
      {
        ready: true,
        extensions: Array.isArray(extensions) ? extensions : [],
      },
      null,
      2
    )
  );
}

/**
 * Find extension metadata entry by name.
 *
 * @param {Array<Object>} extensions - Parsed extensions metadata list
 * @param {string} extensionName - Extension name to match
 * @returns {Object|null} - Matching extension metadata entry
 */
function findExtensionMetadataByName(extensions, extensionName) {
  const wanted = (extensionName || "").toLowerCase();
  return (
    extensions.find((ext) => (ext?.name || "").toLowerCase() === wanted) || null
  );
}

/**
 * Get all loaded extension targets from a browser.
 *
 * @param {Object} browser - Puppeteer browser instance
 * @returns {Array<Object>} - Array of extension target info objects
 */
function getExtensionTargets(browser) {
  return browser
    .targets()
    .filter(
      (target) =>
        getExtensionIdFromUrl(target.url()) ||
        EXTENSION_BACKGROUND_TARGET_TYPES.has(target.type())
    )
    .map((target) => ({
      type: target.type(),
      url: target.url(),
      extensionId: getExtensionIdFromUrl(target.url()),
    }));
}

/**
 * Resolve the Chromium-compatible browser binary to launch.
 *
 * Resolution order matters because tests and runtime callers may override the
 * browser at the environment layer:
 * 1. `CHROME_BINARY`, if explicitly provided at runtime
 * 2. `/usr/bin/chromium` on CI/Linux hosts
 * 3. Chromium-family browsers on the host
 * 5. abxpkg-managed Playwright/Puppeteer provider shims under `ABXPKG_LIB_DIR`
 *
 * This helper intentionally avoids auto-selecting Google Chrome stable. Users
 * may explicitly provide another Chromium-based browser through CHROME_BINARY;
 * extension loading remains the runtime capability check.
 *
 * @returns {string|null} - Absolute path to browser binary or null if not found
 */
function findChromium() {
  const validateBinary = (binaryPath) => isSupportedChromiumBinary(binaryPath);

  const resolveBinaryReference = (binaryPath) => {
    if (!binaryPath) return null;

    const hasPathSeparator =
      binaryPath.includes(path.sep) ||
      (path.sep === "\\" && binaryPath.includes("/"));
    if (path.isAbsolute(binaryPath) || hasPathSeparator) {
      const absPath = path.resolve(binaryPath);
      return validateBinary(absPath) ? absPath : null;
    }

    try {
      const locator = process.platform === "win32" ? "where" : "which";
      const resolved = execFileSync(locator, [binaryPath], {
        encoding: "utf8",
        timeout: 5000,
        stdio: "pipe",
      })
        .split(/\r?\n/)
        .find(Boolean)
        ?.trim();
      return resolved && validateBinary(resolved) ? resolved : null;
    } catch (e) {
      return validateBinary(binaryPath) ? binaryPath : null;
    }
  };

  // 1. Check CHROME_BINARY env var first
  const chromeBinary = getEnv("CHROME_BINARY");
  if (chromeBinary) {
    const resolvedBinary = resolveBinaryReference(chromeBinary);
    if (resolvedBinary) {
      return resolvedBinary;
    }
    console.error(
      `[!] Warning: CHROME_BINARY="${chromeBinary}" is not an executable browser binary.`
    );
  }

  const ciChromiumPath = "/usr/bin/chromium";
  if (validateBinary(ciChromiumPath)) {
    return ciChromiumPath;
  }

  const macCanaryPath =
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary";
  if (process.platform === "darwin" && validateBinary(macCanaryPath)) {
    return macCanaryPath;
  }

  const hostChromiumCandidates =
    process.platform === "darwin"
      ? ["/Applications/Chromium.app/Contents/MacOS/Chromium"]
      : ["chromium", "chromium-browser"];
  for (const candidate of hostChromiumCandidates) {
    const resolvedChromium = resolveBinaryReference(candidate);
    if (resolvedChromium) {
      return resolvedChromium;
    }
  }

  // 2. Warn that no CHROME_BINARY is configured, searching managed installs
  if (!chromeBinary) {
    console.error(
      "[!] Warning: CHROME_BINARY not set, searching managed installs..."
    );
  }

  // 3. Search the stable shims created by abxpkg browser binproviders.
  // Do not walk provider cache internals here; Puppeteer/Playwright own that.
  const libDir = getEnv("ABXPKG_LIB_DIR");
  if (libDir) {
    const libCandidates = [
      path.join(libDir, "env", "bin", "chromium"),
      path.join(libDir, "env", "bin", "chrome"),
      path.join(libDir, "puppeteer", "bin", "chromium"),
      path.join(libDir, "puppeteer", "bin", "chrome"),
      path.join(libDir, "puppeteer", "bin", "chrome-headless-shell"),
      path.join(libDir, "playwright", "bin", "chromium"),
      path.join(libDir, "playwright", "bin", "chrome"),
    ];
    for (const c of libCandidates) {
      if (validateBinary(c)) return c;
    }
  }

  return null;
}

/**
 * Find the supported test/local browser path. Prefers explicit CHROME_BINARY,
 * then CI Chromium, Chrome Canary, host Chromium, then managed Chromium.
 *
 * @returns {string|null} - Absolute path or command name to browser binary
 */
function findAnyChromiumBinary() {
  const chromiumBinary = findChromium();
  if (chromiumBinary) return chromiumBinary;
  return null;
}

// ============================================================================
// Snapshot Hook Utilities (for CDP-based plugins like ssl, responses, dns)
// ============================================================================

const CHROME_SESSION_FILES = Object.freeze({
  cdpUrl: "cdp_url.txt",
  targetId: "target_id.txt",
  chromePid: "chrome.pid",
  browser: "browser.json",
});

/**
 * Parse command line arguments into an object.
 * Handles --key=value and --flag formats.
 *
 * @returns {Object} - Parsed arguments object
 */
/**
 * Resolve the canonical marker/artifact paths for one crawl- or snapshot-level
 * Chrome session directory.
 *
 * The crawl-level session typically owns the long-lived browser markers
 * (`chrome.pid`, `cdp_url.txt`, `browser.json`). Snapshot-level sessions
 * reuse the same schema and add per-tab markers such as `target_id.txt`,
 * `url.txt`, and `navigation.json`.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @returns {{sessionDir: string, cdpFile: string, targetIdFile: string, chromePidFile: string, browserFile: string, urlFile: string, navigationFile: string}}
 */
function getChromeSessionPaths(chromeSessionDir) {
  const sessionDir = path.resolve(chromeSessionDir);
  return {
    sessionDir,
    cdpFile: path.join(sessionDir, CHROME_SESSION_FILES.cdpUrl),
    targetIdFile: path.join(sessionDir, CHROME_SESSION_FILES.targetId),
    chromePidFile: path.join(sessionDir, CHROME_SESSION_FILES.chromePid),
    browserFile: path.join(sessionDir, CHROME_SESSION_FILES.browser),
    urlFile: path.join(sessionDir, "url.txt"),
    navigationFile: path.join(sessionDir, "navigation.json"),
  };
}

/**
 * Read and trim a text file value if it exists.
 *
 * @param {string} filePath - File path
 * @returns {string|null} - Trimmed file value or null
 */
function readSessionTextFile(filePath) {
  if (!fs.existsSync(filePath)) return null;
  const value = fs.readFileSync(filePath, "utf8").trim();
  return value || null;
}

/**
 * Return all persisted marker/artifact files that should be cleaned together
 * when a session is determined to be stale.
 *
 * The list intentionally includes both readiness markers and navigation
 * byproducts. Leaving old `navigation.json` or `browser.json` files behind
 * can trick later hooks/tests into believing a brand-new session has already
 * advanced further than it actually has.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @returns {string[]} - Absolute file paths
 */
function getChromeSessionArtifactPaths(chromeSessionDir) {
  const {
    sessionDir,
    cdpFile,
    targetIdFile,
    chromePidFile,
    browserFile,
  } = getChromeSessionPaths(chromeSessionDir);
  return [
    cdpFile,
    targetIdFile,
    chromePidFile,
    browserFile,
    path.join(sessionDir, "url.txt"),
    path.join(sessionDir, "navigation.json"),
  ];
}

/**
 * Extract the debug port from a Chrome browser websocket endpoint.
 *
 * @param {string|null} cdpUrl - Browser websocket endpoint
 * @returns {number|null} - Parsed port or null
 */
function getChromeDebugPortFromCdpUrl(cdpUrl) {
  if (!cdpUrl) return null;
  try {
    const parsed = new URL(cdpUrl);
    const port = parseInt(parsed.port, 10);
    return Number.isFinite(port) && port > 0 ? port : null;
  } catch (e) {
    const match = cdpUrl.match(/:(\d+)\/devtools\//);
    if (!match) return null;
    const port = parseInt(match[1], 10);
    return Number.isFinite(port) && port > 0 ? port : null;
  }
}

/**
 * Convert a Chrome websocket endpoint into the corresponding DevTools HTTP base URL.
 *
 * Puppeteer accepts HTTP(S) browser URLs for connection setup, while ArchiveBox
 * usually persists browser websocket endpoints in `cdp_url.txt`.
 *
 * @param {string|null} cdpUrl - Browser websocket or HTTP endpoint
 * @returns {string|null} - HTTP(S) browser-server URL or null if invalid
 */
function getBrowserCdpUrlFromCdpUrl(cdpUrl) {
  if (!cdpUrl) return null;

  try {
    const endpoint = new URL(cdpUrl);
    if (endpoint.protocol === "http:" || endpoint.protocol === "https:") {
      endpoint.pathname = "";
      endpoint.search = "";
      endpoint.hash = "";
      return endpoint.toString().replace(/\/+$/, "");
    }
    if (endpoint.protocol !== "ws:" && endpoint.protocol !== "wss:") {
      return null;
    }
    endpoint.protocol = endpoint.protocol === "wss:" ? "https:" : "http:";
    endpoint.pathname = "";
    endpoint.search = "";
    endpoint.hash = "";
    return endpoint.toString().replace(/\/+$/, "");
  } catch (error) {
    return null;
  }
}

function getPuppeteerConnectOptionsForCdpUrl(cdpUrl) {
  if (!cdpUrl) {
    throw new Error("Missing CDP URL");
  }

  try {
    const endpoint = new URL(cdpUrl);
    if (endpoint.protocol === "http:" || endpoint.protocol === "https:") {
      return { browserURL: getBrowserCdpUrlFromCdpUrl(cdpUrl) || cdpUrl };
    }
    if (endpoint.protocol === "ws:" || endpoint.protocol === "wss:") {
      return { browserWSEndpoint: cdpUrl };
    }
    throw new Error(`Invalid CDP URL protocol: ${endpoint.protocol}`);
  } catch (error) {
    if (error instanceof Error) {
      throw error;
    }
    throw new Error(`Invalid CDP URL: ${cdpUrl}`);
  }
}

async function connectToBrowserEndpoint(
  puppeteer,
  cdpUrl,
  connectOptions = {}
) {
  const options = {
    ...getPuppeteerConnectOptionsForCdpUrl(cdpUrl),
    ...connectOptions,
  };
  const deadline =
    Date.now() + getEnvInt("CHROME_CONNECT_RETRY_TIMEOUT_MS", 5000);
  let lastError = null;

  while (Date.now() <= deadline) {
    try {
      return await puppeteer.connect(options);
    } catch (error) {
      lastError = error;
      const message = String(error?.message || error || "");
      const isTargetChurn =
        message.includes("No target with given id found") ||
        message.includes("Target closed") ||
        message.includes("Session closed");
      if (!isTargetChurn || Date.now() >= deadline) {
        throw error;
      }
      await sleep(100);
    }
  }

  throw lastError;
}

async function withTimeout(promiseFactory, timeoutMs, timeoutMessage) {
  let timeoutHandle = null;
  try {
    return await Promise.race([
      promiseFactory(),
      new Promise((_, reject) => {
        timeoutHandle = setTimeout(
          () => reject(new Error(timeoutMessage)),
          timeoutMs
        );
      }),
    ]);
  } finally {
    if (timeoutHandle) {
      clearTimeout(timeoutHandle);
    }
  }
}

async function canConnectToChromeBrowser(cdpUrl, options = {}) {
  const { timeoutMs = 1500, puppeteer = resolvePuppeteerModule() } = options;

  let browser = null;
  try {
    browser = await withTimeout(
      () =>
        connectToBrowserEndpoint(puppeteer, cdpUrl, { defaultViewport: null }),
      timeoutMs,
      `Timed out connecting to browser at ${cdpUrl}`
    );
    return true;
  } catch (error) {
    return false;
  } finally {
    if (browser) {
      try {
        await browser.disconnect();
      } catch (disconnectError) {}
    }
  }
}

/**
 * Inspect whether persisted session markers still correspond to a live attachable
 * Chrome session.
 *
 * This is the boundary between "files exist" and "the session is actually
 * reusable". It validates the saved websocket endpoint, optional target marker,
 * and pid state, then probes the DevTools port so callers can distinguish stale
 * leftovers from a healthy reusable session.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @param {Object} [options={}] - Validation options
 * @param {boolean} [options.requireTargetId=false] - Require target ID marker to consider the session healthy
 * @param {number} [options.probeTimeoutMs=1500] - Timeout for probing the CDP endpoint
 * @param {boolean} [options.validateLiveness=true] - Probe whether the session is actually reusable
 * @param {Object} [options.puppeteer] - Puppeteer module for target-level liveness checks
 * @returns {Promise<{hasArtifacts: boolean, stale: boolean, state: Object, reason: string|null}>}
 */
async function inspectChromeSessionArtifacts(chromeSessionDir, options = {}) {
  const {
    requireTargetId = false,
    probeTimeoutMs = 1500,
    validateLiveness = true,
    processIsLocal = getEnv("CHROME_CDP_URL", "")
      ? false
      : getEnvBool("CHROME_IS_LOCAL", true),
    puppeteer = null,
  } = options;

  const artifactPaths = getChromeSessionArtifactPaths(chromeSessionDir);
  const hasArtifacts = artifactPaths.some((filePath) =>
    fs.existsSync(filePath)
  );
  const sessionPaths = getChromeSessionPaths(chromeSessionDir);
  const cdpUrl = readSessionTextFile(sessionPaths.cdpFile);
  const targetId = readSessionTextFile(sessionPaths.targetIdFile);
  const rawPid = readSessionTextFile(sessionPaths.chromePidFile);
  const parsedPid = rawPid ? parseInt(rawPid, 10) : NaN;
  const pid = Number.isFinite(parsedPid) && parsedPid > 0 ? parsedPid : null;
  const browserMetadata = readBrowserMetadata(chromeSessionDir);
  const state = {
    sessionDir: sessionPaths.sessionDir,
    cdpUrl,
    targetId,
    pid,
    browser: browserMetadata,
    extensions: browserMetadata?.extensions ?? null,
  };
  state.ready = state.browser?.ready === true;

  if (!hasArtifacts) {
    return { hasArtifacts: false, stale: false, state, reason: null };
  }

  if (!state.cdpUrl) {
    return {
      hasArtifacts: true,
      stale: true,
      state,
      reason: "missing cdp_url.txt",
    };
  }

  if (requireTargetId && !state.targetId) {
    return {
      hasArtifacts: true,
      stale: true,
      state,
      reason: "missing target_id.txt",
    };
  }

  if (!validateLiveness) {
    return { hasArtifacts: true, stale: false, state, reason: null };
  }

  if (requireTargetId && state.targetId) {
    let browser = null;
    try {
      const puppeteerModule = puppeteer || resolvePuppeteerModule();
      browser = await withTimeout(
        () =>
          connectToBrowserEndpoint(puppeteerModule, state.cdpUrl, {
            defaultViewport: null,
          }),
        probeTimeoutMs,
        `Timed out connecting to browser at ${state.cdpUrl}`
      );
      const page = await resolvePageByTargetId(
        browser,
        state.targetId,
        probeTimeoutMs
      );
      if (!page) {
        return {
          hasArtifacts: true,
          stale: true,
          state,
          reason: `target ${state.targetId} not found`,
        };
      }
      return { hasArtifacts: true, stale: false, state, reason: null };
    } catch (error) {
      return {
        hasArtifacts: true,
        stale: true,
        state,
        reason: error?.message || `cdp unreachable at ${state.cdpUrl}`,
      };
    } finally {
      if (browser) {
        try {
          await browser.disconnect();
        } catch (disconnectError) {}
      }
    }
  }

  if (processIsLocal) {
    if (state.pid && !isProcessAlive(state.pid)) {
      return {
        hasArtifacts: true,
        stale: true,
        state,
        reason: `chrome pid ${state.pid} is not running`,
      };
    }
    if (!getChromeDebugPortFromCdpUrl(state.cdpUrl)) {
      return {
        hasArtifacts: true,
        stale: true,
        state,
        reason: `invalid cdp url: ${state.cdpUrl}`,
      };
    }
    if (
      await canConnectToChromeBrowser(state.cdpUrl, {
        timeoutMs: probeTimeoutMs,
        puppeteer: puppeteer || resolvePuppeteerModule(),
      })
    ) {
      return { hasArtifacts: true, stale: false, state, reason: null };
    }
    return {
      hasArtifacts: true,
      stale: true,
      state,
      reason: `cdp unreachable at ${state.cdpUrl}`,
    };
  }

  if (
    await canConnectToChromeBrowser(state.cdpUrl, { timeoutMs: probeTimeoutMs })
  ) {
    return { hasArtifacts: true, stale: false, state, reason: null };
  }

  return {
    hasArtifacts: true,
    stale: true,
    state,
    reason: `cdp unreachable at ${state.cdpUrl}`,
  };
}

/**
 * Delete stale marker files for a session directory while leaving healthy ones
 * intact.
 *
 * This should be used before reusing a crawl/snapshot chrome directory. It is
 * safer than blindly unlinking only one file because the readiness lifecycle is
 * multi-step and stale markers tend to cluster.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @param {Object} [options={}] - Validation options
 * @param {boolean} [options.requireTargetId=false] - Require target ID marker to consider the session healthy
 * @param {number} [options.probeTimeoutMs=1500] - Timeout for probing the CDP endpoint
 * @returns {Promise<{hasArtifacts: boolean, stale: boolean, state: Object, reason: string|null, cleanedFiles: string[]}>}
 */
async function cleanupStaleChromeSessionArtifacts(
  chromeSessionDir,
  options = {}
) {
  const inspection = await inspectChromeSessionArtifacts(
    chromeSessionDir,
    options
  );
  const cleanedFiles = [];

  if (!inspection.stale) {
    return { ...inspection, cleanedFiles };
  }

  for (const filePath of getChromeSessionArtifactPaths(chromeSessionDir)) {
    if (!fs.existsSync(filePath)) continue;
    try {
      fs.unlinkSync(filePath);
      cleanedFiles.push(filePath);
    } catch (error) {}
  }

  return { ...inspection, cleanedFiles };
}

/**
 * Wait for the persisted marker state to contain the required fields.
 *
 * This waits for persisted session markers and can optionally require that the
 * published browser endpoint is actually CDP-connectable before succeeding.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @param {Object} [options={}] - Wait/validation options
 * @param {number} [options.timeoutMs=60000] - Timeout in milliseconds
 * @param {number} [options.intervalMs=100] - Poll interval in milliseconds
 * @param {boolean} [options.requireTargetId=false] - Require target ID marker
 * @param {boolean} [options.requireBrowserReady=false] - Require browser.json to be ready
 * @param {boolean} [options.requireConnectable=false] - Require the browser endpoint to be CDP-connectable
 * @param {number} [options.probeTimeoutMs=min(intervalMs, 1000)] - Timeout for each CDP connectability probe
 * @param {Object} [options.puppeteer] - Puppeteer module for target-level connectability checks
 * @returns {Promise<{sessionDir: string, cdpUrl: string|null, targetId: string|null, pid: number|null, browser: Object|null, extensions: Array<Object>|null}|null>}
 */
async function waitForChromeSessionState(chromeSessionDir, options = {}) {
  const {
    timeoutMs = 60000,
    intervalMs = 100,
    requireTargetId = false,
    requireBrowserReady = false,
    requireConnectable = false,
    probeTimeoutMs = Math.min(Math.max(intervalMs, 100), 1000),
    puppeteer = null,
  } = options;
  const startTime = Date.now();

  while (Date.now() - startTime < timeoutMs) {
    const inspection = await inspectChromeSessionArtifacts(chromeSessionDir, {
      requireTargetId,
      validateLiveness: requireConnectable,
      probeTimeoutMs,
      puppeteer,
    });
    const state = inspection.state;
    if (
      state?.cdpUrl &&
      (!requireTargetId || state.targetId) &&
      (!requireBrowserReady || state.ready) &&
      (!requireConnectable || !inspection.stale)
    ) {
      return state;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }

  return null;
}

/**
 * Ensure puppeteer module was passed in by callers.
 *
 * @param {Object} puppeteer - Puppeteer module
 * @param {string} callerName - Caller function name for errors
 * @returns {Object} - Puppeteer module
 * @throws {Error} - If puppeteer is missing
 */
function requirePuppeteerModule(puppeteer, callerName) {
  if (!puppeteer) {
    throw new Error(`puppeteer module must be passed to ${callerName}()`);
  }
  return puppeteer;
}

/**
 * Resolve puppeteer module from installed dependencies.
 *
 * @returns {Object} - Loaded puppeteer module
 * @throws {Error} - If no puppeteer package is installed
 */
function resolvePuppeteerModule() {
  const searchPaths = [getNodeModulesDir(), process.cwd(), __dirname];
  for (const moduleName of ["puppeteer-core", "puppeteer"]) {
    try {
      return require(require.resolve(moduleName, { paths: searchPaths }));
    } catch (e) {}
  }
  throw new Error(
    "Missing puppeteer dependency (need puppeteer-core or puppeteer)"
  );
}

async function waitForChromeLaunchPrerequisites(options = {}) {
  const {
    requireLocalBinary = true,
    timeoutMs = Math.max(getEnvInt("CHROME_TIMEOUT", 60) * 1000, 300000),
    initialIntervalMs = 100,
    maxIntervalMs = 1000,
  } = options;

  const startedAt = Date.now();
  let intervalMs = initialIntervalMs;
  let lastPuppeteerError = "";
  let lastBinaryError = "";

  while (Date.now() - startedAt < timeoutMs) {
    let puppeteer = null;
    let binary = null;

    try {
      puppeteer = resolvePuppeteerModule();
      lastPuppeteerError = "";
    } catch (error) {
      lastPuppeteerError = error.message;
    }

    if (requireLocalBinary) {
      binary = findChromium();
      if (!binary) {
        lastBinaryError = "Chromium binary not found yet";
      } else {
        lastBinaryError = "";
      }
    }

    if (puppeteer && (!requireLocalBinary || binary)) {
      return { puppeteer, binary };
    }

    await sleep(intervalMs);
    intervalMs = Math.min(maxIntervalMs, Math.round(intervalMs * 1.5));
  }

  const details = [lastPuppeteerError, lastBinaryError]
    .filter(Boolean)
    .join("; ");
  throw new Error(
    details
      ? `Timed out waiting for Chrome launch prerequisites: ${details}`
      : "Timed out waiting for Chrome launch prerequisites"
  );
}

/**
 * Connect to a running browser, run an operation, and always disconnect.
 *
 * @param {Object} options - Connection options
 * @param {Object} options.puppeteer - Puppeteer module
 * @param {string} options.browserWSEndpoint - Browser websocket endpoint
 * @param {Object} [options.connectOptions={}] - Additional puppeteer connect options
 * @param {Function} operation - Async callback receiving the browser
 * @returns {Promise<*>} - Operation return value
 */
async function withConnectedBrowser(options, operation) {
  const {
    puppeteer,
    browserWSEndpoint,
    browserURL,
    cdpUrl,
    connectOptions = {},
  } = options;

  const endpoint = browserURL || browserWSEndpoint || cdpUrl;
  const browser = await connectToBrowserEndpoint(
    puppeteer,
    endpoint,
    connectOptions
  );
  try {
    return await operation(browser);
  } finally {
    await browser.disconnect();
  }
}

/**
 * Configure Chrome's download behavior over the live CDP session.
 *
 * This is the supported way to set the downloads directory for ArchiveBox's
 * Chrome lifecycle. Call it after the browser is reachable but before crawl
 * readiness is published so later snapshot hooks inherit a fully-configured
 * browser without needing to mutate on-disk profile `Preferences`.
 *
 * @param {Object} options - Download behavior options
 * @param {Object} options.browser - Connected puppeteer browser instance
 * @param {string} options.downloadPath - Directory to save downloads in
 * @returns {Promise<boolean>} - True if configuration succeeded
 */
async function setBrowserDownloadBehavior(options = {}) {
  const { browser, page, downloadPath } = options;

  if (!browser && !page) {
    throw new Error("setBrowserDownloadBehavior requires a browser or page");
  }
  if (!downloadPath) {
    throw new Error("setBrowserDownloadBehavior requires downloadPath");
  }

  await fs.promises.mkdir(downloadPath, { recursive: true });
  const sessionTarget = page ? page.target() : browser.target();
  const session = await sessionTarget.createCDPSession();

  // Keep the CDP session alive for the lifetime of the caller's browser/page
  // connection. Extension-driven downloads regress if we detach immediately
  // after configuring download behavior.
  if (page) {
    try {
      await session.send("Page.setDownloadBehavior", {
        behavior: "allow",
        downloadPath,
      });
      console.error(
        `[+] Configured Chrome download directory via CDP: ${downloadPath}`
      );
      return true;
    } catch (pageError) {
      if (!browser) {
        throw new Error(
          `Page.setDownloadBehavior failed: ${pageError.message}`
        );
      }
    }
  }

  try {
    await session.send("Browser.setDownloadBehavior", {
      behavior: "allow",
      downloadPath,
    });
    console.error(
      `[+] Configured Chrome download directory via CDP: ${downloadPath}`
    );
    return true;
  } catch (browserError) {
    throw new Error(`Browser.setDownloadBehavior failed: ${browserError.message}`);
  }
}

function getTargetIdFromTarget(target) {
  if (!target) return null;
  return target._targetId || target._targetInfo?.targetId || null;
}

function getTargetIdFromPage(page) {
  if (!page || typeof page.target !== "function") return null;
  try {
    return getTargetIdFromTarget(page.target());
  } catch (error) {
    return null;
  }
}

async function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function cleanupLaunchArtifacts(outputDir, chromePid = null) {
  if (chromePid) {
    try {
      await killChrome(chromePid, outputDir);
    } catch (error) {}
  }

  try {
    await cleanupStaleChromeSessionArtifacts(outputDir, {
      probeTimeoutMs: 250,
    });
  } catch (error) {}
}

/**
 * Verify that a freshly launched browser survives long enough to be considered
 * stable for downstream hooks.
 *
 * This is stronger than "debug port opened once". It waits through the fragile
 * startup window and proves the websocket is attachable with Puppeteer.
 *
 * It must stay strictly earlier than crawl-level extension loading. The caller
 * is responsible for inspecting extension targets and later writing
 * `browser.json`; waiting for that file here would deadlock the launch flow.
 *
 * @param {Object} options - Verification options
 * @param {number} options.chromePid - Spawned Chrome PID
 * @param {string} options.cdpUrl - Browser websocket endpoint
 * @param {boolean} [options.headless=true] - Whether browser is headless
 * @param {boolean} [options.enableExtensionDebugging=false] - Whether extension debugging is enabled
 * @param {number} [options.timeoutMs] - Hydrated Chrome operation timeout in milliseconds
 * @returns {Promise<void>}
 */
async function verifyStableChromiumSession(options = {}) {
  const {
    chromePid,
    cdpUrl,
    headless = true,
    enableExtensionDebugging = false,
    timeoutMs = getEnvInt("CHROME_TIMEOUT", 60) * 1000,
  } = options;

  const hasExtensions = enableExtensionDebugging;
  // Deterministic readiness signal: actively poll for "connect via CDP".
  // Extension startup cannot synthesize a probe page here because extensions
  // need to finish their pre-page-load setup before the first snapshot tab.
  const overallTimeoutMs = getEnvInt(
    "CHROME_LAUNCH_STABILITY_MS",
    Math.max(timeoutMs, hasExtensions ? 15000 : 10000)
  );

  if (!chromePid || !isProcessAlive(chromePid)) {
    throw new Error(
      hasExtensions && headless
        ? "Chromium exited during headless extension startup"
        : "Chromium exited during startup"
    );
  }

  const deadline = Date.now() + overallTimeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    if (!isProcessAlive(chromePid)) {
      throw new Error(
        hasExtensions && headless
          ? "Chromium exited during headless extension startup"
          : "Chromium exited during startup"
      );
    }
    let browser = null;
    try {
      const puppeteer = resolvePuppeteerModule();
      browser = await connectToBrowserEndpoint(puppeteer, cdpUrl, {
        defaultViewport: null,
      });
      if (hasExtensions) {
        const remainingMs = Math.max(1000, deadline - Date.now());
        await withTimeout(
          () => browser.version(),
          remainingMs,
          `Timed out probing browser version after ${remainingMs}ms`
        );
      } else {
        await waitForBrowserPageReady({
          browser,
          timeoutMs: Math.max(1000, deadline - Date.now()),
          requireAboutBlank: true,
          createPageIfMissing: true,
        });
      }
      return;
    } catch (error) {
      lastError = error;
    } finally {
      if (browser) {
        try {
          await browser.disconnect();
        } catch (disconnectError) {}
      }
    }
    await sleep(50);
  }

  throw new Error(
    `Chromium CDP session not stable after startup: ${
      lastError?.message || "timeout"
    }`
  );
}

async function waitForBrowserPageReady(options = {}) {
  const {
    browser = null,
    puppeteer = null,
    cdpUrl = null,
    timeoutMs = 10000,
    requireAboutBlank = false,
    createPageIfMissing = true,
  } = options;

  let ownedBrowser = null;
  let connectedBrowser = browser;
  let createdProbePage = false;
  let lastError = null;
  const deadline = Date.now() + Math.max(timeoutMs, 0);

  if (!connectedBrowser) {
    const puppeteerModule = requirePuppeteerModule(
      puppeteer,
      "waitForBrowserPageReady"
    );
    connectedBrowser = await connectToBrowserEndpoint(puppeteerModule, cdpUrl, {
      defaultViewport: null,
    });
    ownedBrowser = connectedBrowser;
  }

  try {
    while (Date.now() <= deadline) {
      let pages = [];
      try {
        pages = await connectedBrowser.pages();
      } catch (error) {
        lastError = error;
      }

      let page =
        pages.find(
          (candidate) => candidate && candidate.url() === "about:blank"
        ) ||
        pages[0] ||
        null;
      if (
        (!page || (requireAboutBlank && page.url() !== "about:blank")) &&
        createPageIfMissing &&
        !createdProbePage
      ) {
        try {
          page = await connectedBrowser.newPage();
          createdProbePage = true;
        } catch (error) {
          lastError = error;
        }
      }

      if (page) {
        try {
          const url = page.url();
          if (requireAboutBlank && url !== "about:blank") {
            lastError = new Error(
              `Expected about:blank probe page, found ${url || "<empty>"}`
            );
          } else {
            const remainingMs = Math.max(250, deadline - Date.now());
            const title = await withTimeout(
              () => page.title(),
              remainingMs,
              `Timed out probing page title after ${remainingMs}ms`
            );
            const targetId = getTargetIdFromPage(page);
            if (!targetId) {
              throw new Error("Missing target ID for probe page");
            }
            return { browser: connectedBrowser, page, targetId, url, title };
          }
        } catch (error) {
          lastError = error;
        }
      } else if (!lastError) {
        lastError = new Error("No page targets available yet");
      }

      await sleep(100);
    }

    throw new Error(
      lastError?.message || "Timed out waiting for a usable Chrome page"
    );
  } finally {
    if (ownedBrowser) {
      try {
        await ownedBrowser.disconnect();
      } catch (disconnectError) {}
    }
  }
}

async function closeExistingTabs(browser) {
  let aboutBlankPage = null;
  let pages = [];

  try {
    pages = await browser.pages();
  } catch (error) {
    // Extension service-worker targets can disappear while Puppeteer enumerates
    // pages during launch. Chrome is already CDP-ready here; tab cleanup is a
    // best-effort hygiene step and must not invalidate the browser session.
    console.warn(`[⚠️] Could not enumerate Chrome tabs for cleanup: ${error}`);
    return;
  }

  aboutBlankPage =
    pages.find((page) => (page.url() || "") === "about:blank") || null;
  if (!aboutBlankPage) {
    aboutBlankPage = await browser.newPage();
  }

  let cleanupPages = [];
  try {
    cleanupPages = await browser.pages();
  } catch (error) {
    console.warn(`[⚠️] Could not re-enumerate Chrome tabs for cleanup: ${error}`);
    return;
  }

  for (const page of cleanupPages) {
    const url = page.url() || "";
    if (
      page === aboutBlankPage ||
      url.startsWith(CHROME_EXTENSION_URL_PREFIX)
    ) {
      continue;
    }
    try {
      await page.close();
    } catch (error) {}
  }
}

async function resolvePageByTargetId(browser, targetId, timeoutMs = 0) {
  const deadline = Date.now() + Math.max(timeoutMs, 0);
  let discoverySession = null;

  async function ensureDiscoverySession() {
    if (discoverySession) {
      return discoverySession;
    }
    try {
      discoverySession = await browser.target().createCDPSession();
      await discoverySession.send("Target.setDiscoverTargets", {
        discover: true,
      });
    } catch (error) {
      discoverySession = null;
    }
    return discoverySession;
  }

  async function targetIsKnownToCdp() {
    const session = await ensureDiscoverySession();
    if (!session) {
      return false;
    }
    try {
      const { targetInfos = [] } = await session.send("Target.getTargets");
      return targetInfos.some(
        (targetInfo) =>
          targetInfo?.targetId === targetId &&
          (!targetInfo.type || targetInfo.type === "page")
      );
    } catch (error) {
      return false;
    }
  }

  async function pageFromTarget(target) {
    if (!target) {
      return null;
    }
    try {
      return (await target.page()) || null;
    } catch (error) {
      return null;
    }
  }

  try {
    await ensureDiscoverySession();

    while (true) {
      const targets = browser.targets();
      const target = targets.find(
        (candidate) => getTargetIdFromTarget(candidate) === targetId
      );
      const targetPage = await pageFromTarget(target);
      if (targetPage) {
        return targetPage;
      }

      const pages = await browser.pages();
      const pageMatch = pages.find(
        (page) => getTargetIdFromPage(page) === targetId
      );
      if (pageMatch) {
        return pageMatch;
      }

      const remainingMs = Math.max(deadline - Date.now(), 0);
      if (remainingMs > 0 && typeof browser.waitForTarget === "function") {
        const knownToCdp = await targetIsKnownToCdp();
        if (knownToCdp) {
          try {
            const waitedTarget = await browser.waitForTarget(
              (candidate) => getTargetIdFromTarget(candidate) === targetId,
              { timeout: Math.min(remainingMs, 250) }
            );
            const waitedPage = await pageFromTarget(waitedTarget);
            if (waitedPage) {
              return waitedPage;
            }
          } catch (error) {}
        }
      }

      if (Date.now() >= deadline) {
        return null;
      }

      await sleep(100);
    }
  } finally {
    if (discoverySession) {
      try {
        await discoverySession.detach();
      } catch (error) {}
    }
  }
}

/**
 * Resolve a live browser-level CDP endpoint from an already-published session dir.
 *
 * This is the browser-level analogue to `connectToPage(...)`: it waits for the
 * marker contract, verifies the underlying session is still reusable, then
 * returns the raw persisted endpoint. Current `single-file-cli --browser-server`
 * expects the browser websocket endpoint; passing the HTTP DevTools base URL
 * makes it connect to `/` and fail with a 404 on Chrome 148.
 *
 * @param {string} [chromeSessionDir='../chrome'] - Session directory to inspect
 * @param {Object} [options={}] - Resolution options
 * @param {number} [options.timeoutMs=60000] - Timeout waiting for markers
 * @param {boolean} [options.requireTargetId=true] - Require target_id.txt
 * @returns {Promise<string>} - Browser-level CDP endpoint
 * @throws {Error} - If no reusable Chrome session is available
 */
async function getBrowserCdpUrl(chromeSessionDir = "../chrome", options = {}) {
  const { timeoutMs = 60000, requireTargetId = true } = options;
  const processIsLocal =
    options.processIsLocal ??
    (getEnv("CHROME_CDP_URL", "")
      ? false
      : getEnvBool("CHROME_IS_LOCAL", true));

  const state = await waitForChromeSessionState(chromeSessionDir, {
    timeoutMs,
    requireTargetId,
  });
  if (!state?.cdpUrl) {
    throw new Error(CHROME_SESSION_REQUIRED_ERROR);
  }

  const inspection = await inspectChromeSessionArtifacts(chromeSessionDir, {
    requireTargetId,
    probeTimeoutMs: Math.min(Math.max(timeoutMs, 250), 2000),
    processIsLocal,
  });
  if (inspection.stale || !inspection.state?.cdpUrl) {
    throw new Error(CHROME_SESSION_REQUIRED_ERROR);
  }

  const cdpUrl = inspection.state.cdpUrl;
  getPuppeteerConnectOptionsForCdpUrl(cdpUrl);

  const endpoint = new URL(cdpUrl);
  if (endpoint.protocol === "http:" || endpoint.protocol === "https:") {
    const versionUrl = new URL("/json/version", endpoint);
    const response = await fetch(versionUrl);
    if (!response.ok) {
      throw new Error(
        `Invalid CDP URL in chrome session: ${response.status} ${response.statusText}`
      );
    }
    const versionInfo = await response.json();
    if (!versionInfo?.webSocketDebuggerUrl) {
      throw new Error(
        "Invalid CDP URL in chrome session: missing webSocketDebuggerUrl"
      );
    }
    return versionInfo.webSocketDebuggerUrl;
  }

  return cdpUrl;
}

/**
 * Open a blank page target inside an existing crawl-level browser session.
 *
 * This helper only asks DevTools to create the target and returns its runtime
 * `targetId`. Persisting snapshot-level markers such as `target_id.txt`,
 * `cdp_url.txt`, or copied `browser.json` remains the responsibility of the
 * snapshot tab hook.
 *
 * @param {Object} options - Tab open options
 * @param {string} options.cdpUrl - Browser CDP websocket URL
 * @param {Object} options.puppeteer - Puppeteer module
 * @returns {Promise<{targetId: string}>}
 */
async function openTabInChromeSession(options = {}) {
  const { cdpUrl, puppeteer, timeoutMs = 10000, intervalMs = 250 } = options;
  if (!cdpUrl) {
    throw new Error(CHROME_SESSION_REQUIRED_ERROR);
  }
  const puppeteerModule = requirePuppeteerModule(
    puppeteer,
    "openTabInChromeSession"
  );
  const { retry } = require("abxbus");

  return retry({
    semaphore_limit: 1,
    semaphore_name: "chrome.openTabInChromeSession",
    semaphore_scope: "multiprocess",
    semaphore_timeout: Math.max(Math.ceil(timeoutMs / 1000), 1),
    semaphore_lax: false,
  })(async function openSharedChromeTab() {
    const deadline = Date.now() + Math.max(timeoutMs, 0);
    let lastError = null;

    while (Date.now() <= deadline) {
      try {
        return await withConnectedBrowser(
          {
            puppeteer: puppeteerModule,
            cdpUrl,
            connectOptions: { defaultViewport: null },
          },
          async (browser) => {
            const remainingMs = Math.max(
              1000,
              Math.min(5000, deadline - Date.now())
            );
            const page = await withTimeout(
              () => browser.newPage(),
              remainingMs,
              `Timed out creating new page after ${remainingMs}ms`
            );
            await withTimeout(
              () => page.title(),
              remainingMs,
              `Timed out probing new page after ${remainingMs}ms`
            );
            const targetId = getTargetIdFromPage(page);
            if (!targetId) {
              throw new Error("Failed to resolve target ID for new tab");
            }
            await withConnectedBrowser(
              {
                puppeteer: puppeteerModule,
                cdpUrl,
                connectOptions: { defaultViewport: null },
              },
              async (verificationBrowser) => {
                const verificationPage = await resolvePageByTargetId(
                  verificationBrowser,
                  targetId,
                  Math.max(1000, deadline - Date.now())
                );
                if (!verificationPage) {
                  throw new Error(
                    `New tab target ${targetId} was not visible from a fresh Chrome session`
                  );
                }
              }
            );
            return { targetId };
          }
        );
      } catch (error) {
        lastError = error;
        if (Date.now() >= deadline) {
          break;
        }
        await sleep(intervalMs);
      }
    }

    throw lastError || new Error("Failed to open a new Chrome tab");
  })();
}

/**
 * Close a tab by target ID in an existing Chrome session.
 *
 * @param {Object} options - Tab close options
 * @param {string} options.cdpUrl - Browser CDP websocket URL
 * @param {string} options.targetId - Target ID to close
 * @param {Object} options.puppeteer - Puppeteer module
 * @returns {Promise<boolean>} - True if a tab was found and closed
 */
async function closeTabInChromeSession(options = {}) {
  const { cdpUrl, targetId, puppeteer } = options;
  if (!cdpUrl || !targetId) {
    return false;
  }
  const puppeteerModule = requirePuppeteerModule(
    puppeteer,
    "closeTabInChromeSession"
  );

  return withConnectedBrowser(
    {
      puppeteer: puppeteerModule,
      cdpUrl,
      connectOptions: { defaultViewport: null },
    },
    async (browser) => {
      const page = await resolvePageByTargetId(browser, targetId, 1000);
      if (!page) {
        return false;
      }
      await page.close();
      return true;
    }
  );
}

/**
 * Attach to a persisted session directory and resolve the corresponding page.
 *
 * This is the high-level handoff from filesystem readiness markers to a live
 * Puppeteer page object. On success it transfers browser ownership to the
 * caller; on failure before handoff it disconnects immediately so callers do
 * not inherit half-initialized state.
 *
 * @param {Object} options - Connection options
 * @param {string} [options.chromeSessionDir='../chrome'] - Path to chrome session directory
 * @param {number} [options.timeoutMs=60000] - Timeout for waiting
 * @param {boolean} [options.requireTargetId=true] - Require target_id.txt in session dir
 * @param {boolean} [options.requireBrowserReady=false] - Require browser.json to be ready
 * @param {boolean} [options.waitForNavigationComplete=false] - Wait for navigation.json success before attaching
 * @param {number} [options.pageLoadTimeoutMs=timeoutMs] - Timeout for navigation.json readiness
 * @param {number} [options.postLoadDelayMs=0] - Additional delay after successful navigation
 * @param {number} [options.missingTargetGraceMs=3000] - How long to tolerate a missing published target before failing
 * @param {Object} options.puppeteer - Puppeteer module
 * @returns {Promise<Object>} - { browser, page, cdpSession, targetId, cdpUrl, extensions }
 * @throws {Error} - If connection fails or page not found
 */
async function connectToPage(options = {}) {
  const {
    chromeSessionDir = "../chrome",
    timeoutMs = 60000,
    requireTargetId = true,
    requireBrowserReady = false,
    waitForNavigationComplete: shouldWaitForNavigationComplete = false,
    pageLoadTimeoutMs = timeoutMs,
    postLoadDelayMs = 0,
    missingTargetGraceMs = 3000,
    puppeteer,
  } = options;

  const resolvedPuppeteer = puppeteer || resolvePuppeteerModule();
  const initialInspection = await inspectChromeSessionArtifacts(
    chromeSessionDir,
    {
      requireTargetId,
      validateLiveness: false,
    }
  );
  if (!initialInspection.hasArtifacts) {
    throw new Error(CHROME_SESSION_REQUIRED_ERROR);
  }
  if (!initialInspection.state?.cdpUrl) {
    throw new Error(CHROME_SESSION_REQUIRED_ERROR);
  }
  getPuppeteerConnectOptionsForCdpUrl(initialInspection.state.cdpUrl);
  if (requireTargetId && !initialInspection.state?.targetId) {
    const sessionPaths = getChromeSessionPaths(chromeSessionDir);
    const hasLaterSnapshotMarkers = [
      sessionPaths.urlFile,
      sessionPaths.navigationFile,
    ].some((filePath) => fs.existsSync(filePath));
    if (hasLaterSnapshotMarkers) {
      throw new Error("No target_id.txt found");
    }
  }

  if (shouldWaitForNavigationComplete) {
    await waitForNavigationComplete(
      chromeSessionDir,
      pageLoadTimeoutMs,
      postLoadDelayMs
    );
  }

  const deadline = Date.now() + timeoutMs;
  let lastError = new Error(CHROME_SESSION_REQUIRED_ERROR);
  let missingTargetKey = null;
  let missingTargetSince = 0;
  const staleTargetGraceMs = Math.min(
    timeoutMs,
    Math.max(0, missingTargetGraceMs)
  );

  while (Date.now() < deadline) {
    const remainingMs = Math.max(deadline - Date.now(), 0);
    const state = await waitForChromeSessionState(chromeSessionDir, {
      timeoutMs: Math.min(remainingMs, 500),
      intervalMs: 100,
      requireTargetId,
      requireBrowserReady,
    });
    if (!state) {
      missingTargetKey = null;
      missingTargetSince = 0;
      if (Date.now() >= deadline) {
        break;
      }
      await sleep(100);
      continue;
    }

    const targetId = state.targetId;
    const browser = await connectToBrowserEndpoint(
      resolvedPuppeteer,
      state.cdpUrl,
      { defaultViewport: null }
    ).catch((error) => {
      lastError = error instanceof Error ? error : new Error(String(error));
      return null;
    });

    if (!browser) {
      if (Date.now() >= deadline) break;
      await sleep(100);
      continue;
    }

    try {
      let page = null;

      if (targetId) {
        page = await resolvePageByTargetId(
          browser,
          targetId,
          Math.min(remainingMs, 1000)
        );
        if (!page && requireTargetId) {
          const currentTargetKey = `${state.cdpUrl}::${targetId}`;
          const now = Date.now();
          if (missingTargetKey !== currentTargetKey) {
            missingTargetKey = currentTargetKey;
            missingTargetSince = now;
          } else if (now - missingTargetSince >= staleTargetGraceMs) {
            const error = new Error(
              `Target ${targetId} not found in Chrome session`
            );
            error.code = "TARGET_NOT_FOUND_STABLE";
            throw error;
          }
          throw new Error(`Target ${targetId} not found in Chrome session`);
        }
        missingTargetKey = null;
        missingTargetSince = 0;
      }

      const pages = await browser.pages();
      if (!page && !requireTargetId) {
        page = pages[pages.length - 1];
      }

      if (!page) {
        throw new Error("No page found in browser");
      }
      if (requireTargetId && targetId && getTargetIdFromPage(page) !== targetId) {
        throw new Error(`Resolved page does not match target ${targetId}`);
      }
      if (requireTargetId && targetId) {
        try {
          const targetSession = await browser.target().createCDPSession();
          await targetSession.send("Target.activateTarget", { targetId });
          await targetSession.detach();
        } catch (error) {}
      }
      if (requireTargetId && targetId && typeof page.bringToFront === "function") {
        await page.bringToFront();
      }

      const cdpSession = await page.target().createCDPSession();
      await cdpSession.send("Target.setAutoAttach", {
        autoAttach: true,
        waitForDebuggerOnStart: false,
        flatten: true,
      });

      return {
        ...state,
        browser,
        page,
        cdpSession,
        targetId,
      };
    } catch (error) {
      lastError = error instanceof Error ? error : new Error(String(error));
      try {
        await browser.disconnect();
      } catch (disconnectError) {}
      if (lastError.code === "TARGET_NOT_FOUND_STABLE") {
        break;
      }
    }

    if (Date.now() >= deadline) {
      break;
    }
    await sleep(100);
  }

  throw lastError;
}

function loadInstalledExtensionsFromCache(extensionsDir = getExtensionsDir()) {
  const installedExtensions = [];

  if (!fs.existsSync(extensionsDir)) {
    return { installedExtensions };
  }

  for (const file of fs.readdirSync(extensionsDir)) {
    if (!file.endsWith(".extension.json")) continue;

    try {
      const extPath = path.join(extensionsDir, file);
      const extData = JSON.parse(fs.readFileSync(extPath, "utf-8"));
      if (!extData.unpacked_path || !fs.existsSync(extData.unpacked_path))
        continue;
      delete extData.id;
      delete extData.target;
      delete extData.target_type;
      delete extData.target_url;
      delete extData.manifest;
      delete extData.manifest_version;
      delete extData.load_error;
      delete extData.target_error;
      installedExtensions.push(extData);
    } catch (error) {}
  }

  return { installedExtensions };
}

function parseCookiesTxt(contents) {
  const cookies = [];
  let skipped = 0;

  for (const rawLine of contents.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;

    let httpOnly = false;
    let dataLine = line;

    if (dataLine.startsWith("#HttpOnly_")) {
      httpOnly = true;
      dataLine = dataLine.slice("#HttpOnly_".length);
    } else if (dataLine.startsWith("#")) {
      continue;
    }

    const parts = dataLine.split("\t");
    if (parts.length < 7) {
      skipped += 1;
      continue;
    }

    const [
      domainRaw,
      includeSubdomainsRaw,
      pathRaw,
      secureRaw,
      expiryRaw,
      name,
      value,
    ] = parts;
    if (!name || !domainRaw) {
      skipped += 1;
      continue;
    }

    const includeSubdomains =
      (includeSubdomainsRaw || "").toUpperCase() === "TRUE";
    let domain = domainRaw;
    if (includeSubdomains && !domain.startsWith(".")) domain = `.${domain}`;
    if (!includeSubdomains && domain.startsWith(".")) domain = domain.slice(1);

    const cookie = {
      name,
      value,
      domain,
      path: pathRaw || "/",
      secure: (secureRaw || "").toUpperCase() === "TRUE",
      httpOnly,
    };

    const expires = parseInt(expiryRaw, 10);
    if (!isNaN(expires) && expires > 0) {
      cookie.expires = expires;
    }

    cookies.push(cookie);
  }

  return { cookies, skipped };
}

async function importCookiesFromFile(browser, cookiesFile, userDataDir) {
  if (!cookiesFile) return;

  if (!fs.existsSync(cookiesFile)) {
    console.error(`[!] Cookies file not found: ${cookiesFile}`);
    return;
  }

  let contents = "";
  try {
    contents = fs.readFileSync(cookiesFile, "utf-8");
  } catch (e) {
    console.error(`[!] Failed to read COOKIES_FILE: ${e.message}`);
    return;
  }

  const { cookies, skipped } = parseCookiesTxt(contents);
  if (cookies.length === 0) {
    console.error("[!] No cookies found to import");
    return;
  }

  console.error(
    `[*] Importing ${cookies.length} cookies from ${cookiesFile}...`
  );
  if (skipped) {
    console.error(`[*] Skipped ${skipped} malformed cookie line(s)`);
  }
  if (!userDataDir) {
    console.error(
      "[!] CHROME_USER_DATA_DIR not set; cookies will not persist beyond this session"
    );
  }

  const page = await browser.newPage();
  const client = await page.target().createCDPSession();
  await client.send("Network.enable");

  const chunkSize = 200;
  let imported = 0;
  for (let i = 0; i < cookies.length; i += chunkSize) {
    const chunk = cookies.slice(i, i + chunkSize);
    try {
      await client.send("Network.setCookies", { cookies: chunk });
      imported += chunk.length;
    } catch (e) {
      console.error(
        `[!] Failed to import cookies ${i + 1}-${i + chunk.length}: ${
          e.message
        }`
      );
    }
  }

  await page.close();
  console.error(`[+] Imported ${imported}/${cookies.length} cookies`);
}

async function waitForProcessExit(pid, timeoutMs = 5000, intervalMs = 100) {
  if (!pid) return true;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!isProcessAlive(pid)) {
      return true;
    }
    await sleep(intervalMs);
  }
  return !isProcessAlive(pid);
}

async function waitForBrowserEndpointGone(
  cdpUrl,
  timeoutMs = 5000,
  intervalMs = 200
) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (
      !(await canConnectToChromeBrowser(cdpUrl, {
        timeoutMs: Math.min(intervalMs, 1000),
      }))
    ) {
      return true;
    }
    await sleep(intervalMs);
  }
  return !(await canConnectToChromeBrowser(cdpUrl, {
    timeoutMs: Math.min(intervalMs, 1000),
  }));
}

async function closeBrowserInChromeSession(options = {}) {
  const {
    cdpUrl = null,
    pid = null,
    outputDir = null,
    puppeteer = resolvePuppeteerModule(),
    processIsLocal = getEnv("CHROME_CDP_URL", "")
      ? false
      : getEnvBool("CHROME_IS_LOCAL", true),
    forceKillTimeoutMs = getEnvInt("CHROME_CLOSE_TIMEOUT_MS", 5000),
  } = options;

  if (!cdpUrl && !(processIsLocal && pid)) {
    return false;
  }

  if (cdpUrl) {
    let browser = null;
    try {
      browser = await connectToBrowserEndpoint(puppeteer, cdpUrl, {
        defaultViewport: null,
      });
      const session = await browser.target().createCDPSession();
      await withTimeout(
        () => session.send("Browser.close"),
        forceKillTimeoutMs,
        `Timed out closing browser at ${cdpUrl}`
      );
    } catch (error) {
      console.error(`[!] Browser.close failed: ${error.message}`);
    } finally {
      if (browser) {
        try {
          await browser.disconnect();
        } catch (disconnectError) {}
      }
    }
  }

  const debugPort = cdpUrl ? getChromeDebugPortFromCdpUrl(cdpUrl) : null;
  let closed = false;
  if (processIsLocal && pid) {
    closed = await waitForProcessExit(pid, forceKillTimeoutMs);
    if (!closed) {
      closed = await killChrome(pid, outputDir);
    }
  } else if (cdpUrl) {
    closed = await waitForBrowserEndpointGone(cdpUrl, forceKillTimeoutMs);
    if (closed && debugPort) {
      const relatedPids = findChromeProcessesByPort(debugPort);
      if (relatedPids.length > 0) {
        closed = await killChrome(relatedPids[0], outputDir);
      }
    }
  }

  if (outputDir && closed) {
    try {
      await cleanupStaleChromeSessionArtifacts(outputDir, {
        processIsLocal,
        probeTimeoutMs: Math.min(Math.max(forceKillTimeoutMs, 250), 1000),
      });
    } catch (error) {}
  }

  return closed;
}

async function ensureChromeSession(options = {}) {
  const chromeLaunchOptions = resolveChromeLaunchOptions(options);
  const {
    outputDir = ".",
    puppeteer = resolvePuppeteerModule(),
    CHROME_CDP_URL = getEnv("CHROME_CDP_URL", ""),
    CHROME_IS_LOCAL = CHROME_CDP_URL
      ? false
      : getEnvBool("CHROME_IS_LOCAL", true),
    downloadsDir = chromeLaunchOptions.CHROME_DOWNLOADS_DIR,
    cookiesFile = getEnv("COOKIES_FILE"),
    extensionsDir = chromeLaunchOptions.CHROME_EXTENSIONS_DIR,
    timeoutMs = getEnvInt("CHROME_TIMEOUT", 60) * 1000,
    reuseExisting = !CHROME_CDP_URL,
    binary = null,
  } = options;
  const cdpUrl = CHROME_CDP_URL;
  const processIsLocal = CHROME_CDP_URL ? false : CHROME_IS_LOCAL;
  const userDataDir = chromeLaunchOptions.CHROME_USER_DATA_DIR;

  if (!fs.existsSync(outputDir)) {
    fs.mkdirSync(outputDir, { recursive: true });
  }

  const { installedExtensions } = loadInstalledExtensionsFromCache(
    extensionsDir
  );

  const existingSession = await inspectChromeSessionArtifacts(outputDir, {
    processIsLocal,
  });
  const reusingExplicitCdpUrl =
    Boolean(cdpUrl) &&
    existingSession.hasArtifacts &&
    !existingSession.stale &&
    existingSession.state?.cdpUrl === cdpUrl;

  if (
    reuseExisting &&
    existingSession.hasArtifacts &&
    !existingSession.stale &&
    existingSession.state?.cdpUrl
  ) {
    if (installedExtensions.length > 0) {
      let browser = null;
      try {
        browser = await connectToBrowserEndpoint(
          puppeteer,
          existingSession.state.cdpUrl,
          { defaultViewport: null }
        );
        await loadUnpackedExtensionsIntoBrowser(
          browser,
          installedExtensions,
          timeoutMs
        );
        writeBrowserMetadata(outputDir, installedExtensions);
      } finally {
        if (browser) {
          try {
            await browser.disconnect();
          } catch (error) {}
        }
      }
    }
    writeBrowserMetadata(outputDir, installedExtensions);
    return {
      cdpUrl: existingSession.state.cdpUrl,
      pid: existingSession.state.pid,
      port: getChromeDebugPortFromCdpUrl(existingSession.state.cdpUrl),
      installedExtensions,
      processIsLocal,
      reusedExisting: true,
      binary,
    };
  }

  if (
    !reusingExplicitCdpUrl &&
    existingSession.hasArtifacts &&
    existingSession.state?.cdpUrl
  ) {
    try {
      await closeBrowserInChromeSession({
        cdpUrl: existingSession.state.cdpUrl,
        pid: existingSession.state.pid,
        outputDir,
        puppeteer,
        processIsLocal: Boolean(existingSession.state.pid),
      });
    } catch (error) {}
  }

  if (!reusingExplicitCdpUrl) {
    const staleSession = await cleanupStaleChromeSessionArtifacts(outputDir, {
      processIsLocal: existingSession.state?.pid ? true : processIsLocal,
    });
    if (staleSession.cleanedFiles.length === 0) {
      for (const filePath of getChromeSessionArtifactPaths(outputDir)) {
        if (!fs.existsSync(filePath)) continue;
        try {
          fs.unlinkSync(filePath);
        } catch (error) {}
      }
    }
  }

  let resolvedBinary = binary;
  let resolvedPid =
    reusingExplicitCdpUrl && processIsLocal
      ? existingSession.state?.pid || null
      : null;
  let resolvedCdpUrl = reusingExplicitCdpUrl
    ? existingSession.state?.cdpUrl
    : cdpUrl;
  let resolvedUserDataDir = userDataDir;
  let launchedNewBrowser = false;

  if (!resolvedCdpUrl) {
    if (!processIsLocal) {
      throw new Error(
        "CHROME_IS_LOCAL=false requires CHROME_CDP_URL or an upstream published chrome session"
      );
    }

    resolvedBinary = resolvedBinary || findChromium();
    if (!resolvedBinary) {
      throw new Error("Chromium binary not found");
    }
    if (installedExtensions.length > 0) {
      console.error(
        `[*] Loading ${installedExtensions.length} extension(s) after Chrome launch with CDP Extensions.loadUnpacked`
      );
    }

    const result = await launchChromium({
      binary: resolvedBinary,
      outputDir,
      ...chromeLaunchOptions,
      CHROME_USER_DATA_DIR: userDataDir,
      enableExtensionDebugging: installedExtensions.length > 0,
      extensionPaths: getExtensionPaths(installedExtensions),
      timeoutMs,
    });
    if (!result.success) {
      throw new Error(result.error || "Failed to launch Chromium");
    }

    resolvedPid = result.pid;
    resolvedCdpUrl = result.cdpUrl;
    resolvedUserDataDir = result.userDataDir || resolvedUserDataDir;
    launchedNewBrowser = true;
  }

  if (resolvedPid) {
    fs.writeFileSync(path.join(outputDir, "chrome.pid"), String(resolvedPid));
  } else {
    try {
      fs.unlinkSync(path.join(outputDir, "chrome.pid"));
    } catch (error) {}
  }
  fs.writeFileSync(path.join(outputDir, "cdp_url.txt"), resolvedCdpUrl);

  // Open a single browser connection for all post-launch CDP work: extension
  // load, cookie import, download dir config, page-ready probe, and tab
  // cleanup. Each connectToBrowserEndpoint call costs ~150-200ms (it
  // enumerates all targets), so reusing one browser saves multiple seconds
  // across the full setup path.
  const needsPostLaunchBrowser =
    downloadsDir ||
    cookiesFile ||
    installedExtensions.length > 0 ||
    launchedNewBrowser;
  if (needsPostLaunchBrowser) {
    let browser = null;
    try {
      browser = await connectToBrowserEndpoint(puppeteer, resolvedCdpUrl, {
        defaultViewport: null,
      });

      if (installedExtensions.length > 0) {
        // Keep this existing browser connection after Extensions.loadUnpacked.
        // A fresh Puppeteer connect enumerates extension targets and can lose a
        // race against short-lived MV3/archiveweb.page targets that close after
        // Chrome reports them but before Target.attachToTarget runs.
        await loadUnpackedExtensionsIntoBrowser(
          browser,
          installedExtensions,
          timeoutMs
        );
      }

      if (downloadsDir) {
        await setBrowserDownloadBehavior({
          browser,
          downloadPath: downloadsDir,
        });
      }

      if (cookiesFile) {
        await importCookiesFromFile(browser, cookiesFile, resolvedUserDataDir);
      }

      await waitForBrowserPageReady({
        browser,
        timeoutMs: getEnvInt("CHROME_PAGE_READY_TIMEOUT_MS", 10000),
        requireAboutBlank: true,
        createPageIfMissing: true,
      });

      if (launchedNewBrowser) {
        await closeExistingTabs(browser);
      }
    } finally {
      if (browser) {
        try {
          await browser.disconnect();
        } catch (error) {}
      }
    }
  } else {
    await waitForBrowserPageReady({
      puppeteer,
      cdpUrl: resolvedCdpUrl,
      timeoutMs: getEnvInt("CHROME_PAGE_READY_TIMEOUT_MS", 10000),
      requireAboutBlank: true,
      createPageIfMissing: true,
    });
  }

  if (processIsLocal && resolvedPid) {
    // Final readiness gate: chrome can be "process alive + port bound" but
    // still not fully ready to serve fresh CDP connections, especially on
    // slow machines under load where extension initialization happens in
    // the background after our setup work returns. Poll for a fresh CDP
    // probe to succeed — that's the deterministic signal that downstream
    // snapshot hooks will be able to connect. Process-alive is checked
    // inside the loop so a crash fails fast.
    const stabilityDeadline =
      Date.now() +
      getEnvInt(
        "CHROME_LAUNCH_STABILITY_MS",
        Math.max(timeoutMs, installedExtensions.length > 0 ? 15000 : 10000)
      );
    let probedOk = false;
    let lastProbeFailure = null;
    while (Date.now() < stabilityDeadline) {
      if (!isProcessAlive(resolvedPid)) {
        throw new Error(
          `Chrome process ${resolvedPid} exited during launch setup`
        );
      }
      try {
        const reachable = await canConnectToChromeBrowser(resolvedCdpUrl, {
          timeoutMs: Math.max(
            500,
            Math.min(stabilityDeadline - Date.now(), 1500)
          ),
          puppeteer,
        });
        if (reachable) {
          probedOk = true;
          break;
        }
        lastProbeFailure = "CDP probe returned unreachable";
      } catch (error) {
        lastProbeFailure = error?.message || String(error);
      }
      await sleep(50);
    }
    if (!probedOk) {
      throw new Error(
        `Chrome session not CDP-responsive after launch setup: ${
          lastProbeFailure || "timeout"
        }`
      );
    }
  }

  writeBrowserMetadata(outputDir, installedExtensions);

  return {
    cdpUrl: resolvedCdpUrl,
    pid: resolvedPid,
    port: getChromeDebugPortFromCdpUrl(resolvedCdpUrl),
    installedExtensions,
    processIsLocal,
    reusedExisting: false,
    binary: resolvedBinary,
    userDataDir: resolvedUserDataDir,
  };
}

/**
 * Wait for the snapshot navigation hook to publish a successful navigation result.
 *
 * This does not perform navigation by itself. It only watches the
 * `navigation.json` artifact emitted by `chrome_navigate` and optionally waits
 * a bit longer for late network work that should remain within the same
 * snapshot lifecycle.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @param {number} [timeoutMs=120000] - Timeout in milliseconds
 * @param {number} [postLoadDelayMs=0] - Additional delay after successful navigation
 * @returns {Promise<Object>} - Parsed navigation state
 * @throws {Error} - If timeout waiting for navigation or navigation.json reports an error
 */
async function waitForNavigationComplete(
  chromeSessionDir,
  timeoutMs = 120000,
  postLoadDelayMs = 0
) {
  const { navigationFile } = getChromeSessionPaths(chromeSessionDir);
  const pollInterval = 100;
  const deadline = Date.now() + timeoutMs;
  let lastParseError = null;

  while (Date.now() < deadline) {
    if (!fs.existsSync(navigationFile)) {
      await new Promise((resolve) => setTimeout(resolve, pollInterval));
      continue;
    }

    try {
      const rawNavigationState = fs.readFileSync(navigationFile, "utf8");
      if (!rawNavigationState.trim()) {
        throw new SyntaxError("navigation.json is empty");
      }
      const navigationState = JSON.parse(rawNavigationState);
      if (navigationState?.error) {
        throw new Error(navigationState.error);
      }

      if (postLoadDelayMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, postLoadDelayMs));
      }

      return navigationState;
    } catch (error) {
      if (error instanceof SyntaxError) {
        lastParseError = error;
        await new Promise((resolve) => setTimeout(resolve, pollInterval));
        continue;
      }
      throw error;
    }
  }

  if (lastParseError) {
    throw new Error(
      `Timeout waiting for navigation (invalid navigation.json: ${lastParseError.message})`
    );
  }
  throw new Error(
    "Timeout waiting for navigation (chrome_navigate did not complete)"
  );
}

/**
 * Read all browser cookies from a running Chrome CDP debug port.
 * Uses existing CDP bootstrap helpers and puppeteer connection logic.
 *
 * @param {number} port - Chrome remote debugging port
 * @param {Object} [options={}] - Optional settings
 * @param {number} [options.timeoutMs=10000] - Timeout waiting for debug port
 * @returns {Promise<Array<Object>>} - Array of cookie objects
 */
async function getCookiesViaCdp(port, options = {}) {
  const timeoutMs =
    options.timeoutMs || getEnvInt("CDP_COOKIE_TIMEOUT_MS", 10000);
  const versionInfo = await waitForDebugPort(port, timeoutMs);
  const browserWSEndpoint = versionInfo?.webSocketDebuggerUrl;
  if (!browserWSEndpoint) {
    throw new Error(`No webSocketDebuggerUrl from Chrome debug port ${port}`);
  }
  const puppeteerModule = resolvePuppeteerModule();

  return withConnectedBrowser(
    {
      puppeteer: puppeteerModule,
      browserWSEndpoint,
    },
    async (browser) => {
      const session = await browser.target().createCDPSession();
      const result = await session.send("Storage.getCookies");
      return result?.cookies || [];
    }
  );
}

// Export all functions
module.exports = {
  // Environment helpers
  getEnv,
  getEnvBool,
  getEnvInt,
  getEnvArray,
  parseResolution,
  cleanupChromeProfileLockFiles,
  // PID file management
  writePidWithMtime,
  writeCmdScript,
  acquireSessionLock,
  // Port management
  findFreePort,
  waitForDebugPort,
  // Zombie cleanup
  killZombieChrome,
  // Chrome launching
  resolveChromeLaunchOptions,
  getChromeSessionOptionsFromConfig,
  getNodeModulesDir,
  launchChromium,
  killChrome,
  // Chromium binary finding
  findChromium,
  findAnyChromiumBinary,
  parseChromiumVersion,
  isSupportedChromiumVersionOutput,
  isSupportedChromiumBinary,
  parseChromiumUserAgentVersion,
  replaceChromeUserAgentVersion,
  // Extension utilities
  loadExtensionManifest,
  isTargetExtension,
  loadExtensionFromTarget,
  loadUnpackedExtensionsIntoBrowser,
  waitForExtensionTargetHandle,
  // New puppeteer best-practices helpers
  resolvePuppeteerModule,
  connectToBrowserEndpoint,
  withConnectedBrowser,
  closeExistingTabs,
  getExtensionPaths,
  waitForExtensionTarget,
  getExtensionTargets,
  findExtensionMetadataByName,
  readBrowserMetadata,
  writeBrowserMetadata,
  loadInstalledExtensionsFromCache,
  importCookiesFromFile,
  ensureChromeSession,
  // Chrome/browser path utilities
  getExtensionsDir,
  // Snapshot hook utilities (for CDP-based plugins)
  parseArgs,
  inspectChromeSessionArtifacts,
  cleanupStaleChromeSessionArtifacts,
  waitForChromeSessionState,
  waitForChromeLaunchPrerequisites,
  getBrowserCdpUrl,
  openTabInChromeSession,
  closeTabInChromeSession,
  closeBrowserInChromeSession,
  getTargetIdFromTarget,
  getTargetIdFromPage,
  connectToPage,
  waitForNavigationComplete,
  setBrowserDownloadBehavior,
  getCookiesViaCdp,
};

// CLI usage
if (require.main === module) {
  const args = process.argv.slice(2);

  if (args.length === 0) {
    console.log("Usage: chrome_utils.js <command> [args...]");
    console.log("");
    console.log("Commands:");
    console.log("  findChromium              Find Chromium binary");
    console.log(
      "  isSupportedChromiumBinary <path>  Check Chromium >=149.0.0 support"
    );
    console.log("  launchChromium            Launch Chrome with CDP debugging");
    console.log("  getCookiesViaCdp <port>  Read browser cookies via CDP port");
    console.log(
      "  getBrowserCdpUrl      Resolve browser-level CDP endpoint from session dir"
    );
    console.log("  killChrome <pid>          Kill Chrome process by PID");
    console.log("  killZombieChrome          Clean up zombie Chrome processes");
    console.log("");
    console.log("  getExtensionsDir          Get Chrome extensions directory");
    console.log("");
    console.log("  loadExtensionManifest     Load extension manifest.json");
    console.log(
      "  readBrowserMetadata       Read published browser setup metadata"
    );
    console.log("");
    console.log("Environment variables:");
    console.log("  SNAP_DIR                  Base snapshot directory");
    console.log("  CRAWL_DIR                 Base crawl directory");
    console.log("  PERSONAS_DIR              Personas directory");
    console.log(
      "  ABXPKG_LIB_DIR                   Library directory (computed if not set)"
    );
    console.log("  MACHINE_TYPE              Machine type override");
    console.log("  NODE_MODULES_DIR          Node modules directory");
    console.log("  CHROME_BINARY             Chrome binary path");
    process.exit(1);
  }

  const [command, ...commandArgs] = args;

  (async () => {
    try {
      switch (command) {
        case "findChromium": {
          const binary = findChromium();
          if (binary) {
            console.log(binary);
          } else {
            console.error("Chromium binary not found");
            process.exit(1);
          }
          break;
        }

        case "isSupportedChromiumBinary": {
          const [binaryPath] = commandArgs;
          console.log(
            Boolean(binaryPath && isSupportedChromiumBinary(binaryPath))
          );
          break;
        }

        case "launchChromium": {
          const [outputDir] = commandArgs;
          const result = await launchChromium({
            outputDir: outputDir || "chrome",
          });
          if (result.success) {
            console.log(
              JSON.stringify({
                cdpUrl: result.cdpUrl,
                pid: result.pid,
                port: result.port,
              })
            );
          } else {
            console.error(result.error);
            process.exit(1);
          }
          break;
        }

        case "getCookiesViaCdp": {
          const [portStr] = commandArgs;
          const port = parseInt(portStr, 10);
          if (isNaN(port) || port <= 0) {
            console.error("Invalid port");
            process.exit(1);
          }
          const cookies = await getCookiesViaCdp(port);
          console.log(JSON.stringify(cookies));
          break;
        }

        case "getBrowserCdpUrl": {
          const [
            chromeSessionDir = "../chrome",
            timeoutMsStr = "60000",
            requireTargetIdStr = "true",
          ] = commandArgs;
          const timeoutMs = parseInt(timeoutMsStr, 10);
          if (isNaN(timeoutMs) || timeoutMs <= 0) {
            console.error("Invalid timeoutMs");
            process.exit(1);
          }
          const requireTargetId = !["0", "false", "no"].includes(
            String(requireTargetIdStr).toLowerCase()
          );
          const browserCdpUrl = await getBrowserCdpUrl(chromeSessionDir, {
            timeoutMs,
            requireTargetId,
          });
          console.log(browserCdpUrl);
          break;
        }

        case "killChrome": {
          const [pidStr, outputDir] = commandArgs;
          const pid = parseInt(pidStr, 10);
          if (isNaN(pid)) {
            console.error("Invalid PID");
            process.exit(1);
          }
          await killChrome(pid, outputDir);
          break;
        }

        case "killZombieChrome": {
          const [snapDir] = commandArgs;
          const killed = await killZombieChrome(snapDir);
          console.log(killed);
          break;
        }

        case "loadExtensionManifest": {
          const [unpacked_path] = commandArgs;
          const manifest = loadExtensionManifest(unpacked_path);
          console.log(JSON.stringify(manifest));
          break;
        }

        case "readBrowserMetadata": {
          const [chromeSessionDir = ".", timeoutMsStr = "10000"] = commandArgs;
          const timeoutMs = parseInt(timeoutMsStr, 10);
          if (isNaN(timeoutMs) || timeoutMs <= 0) {
            console.error("Invalid timeoutMs");
            process.exit(1);
          }
          const deadline = Date.now() + timeoutMs;
          let metadata = readBrowserMetadata(chromeSessionDir);
          while (metadata === null && Date.now() < deadline) {
            await sleep(250);
            metadata = readBrowserMetadata(chromeSessionDir);
          }
          if (metadata === null) {
            console.error(
              `Timeout waiting for browser metadata in ${chromeSessionDir}`
            );
            process.exit(1);
          }
          console.log(JSON.stringify(metadata));
          break;
        }

        case "getExtensionsDir": {
          console.log(getExtensionsDir());
          break;
        }

        case "getNodeModulesDir": {
          console.log(getNodeModulesDir());
          break;
        }

        default:
          console.error(`Unknown command: ${command}`);
          process.exit(1);
      }
    } catch (error) {
      console.error(`Error: ${error.message}`);
      process.exit(1);
    }
  })();
}
