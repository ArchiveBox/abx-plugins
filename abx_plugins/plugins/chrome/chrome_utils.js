#!/usr/bin/env node
/**
 * Chrome Extension Management Utilities
 *
 * Handles downloading, installing, and managing Chrome extensions for browser automation.
 * Ported from the TypeScript implementation in archivebox.ts
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const http = require('http');
const os = require('os');
const net = require('net');
const { exec, spawn } = require('child_process');
const { promisify } = require('util');
const { Readable } = require('stream');
const { finished } = require('stream/promises');

const execAsync = promisify(exec);

// Import generic helpers from base plugin
const { getEnv, getEnvBool, getEnvInt, getEnvArray, parseArgs } = require('../base/utils.js');

const CHROME_SESSION_REQUIRED_ERROR = 'No Chrome session found (chrome plugin must run first)';

/**
 * Get the current snapshot directory.
 * Priority: SNAP_DIR, or cwd.
 *
 * @returns {string} - Absolute path to snapshot directory
 */
function getSnapDir() {
    const snapDir = getEnv('SNAP_DIR');
    if (snapDir) return path.resolve(snapDir);
    return path.resolve(process.cwd());
}

/**
 * Get the current crawl directory.
 * Priority: CRAWL_DIR, or cwd.
 *
 * @returns {string} - Absolute path to crawl directory
 */
function getCrawlDir() {
    const crawlDir = getEnv('CRAWL_DIR');
    if (crawlDir) return path.resolve(crawlDir);
    return path.resolve(process.cwd());
}

/**
 * Get the personas directory.
 * Priority: PERSONAS_DIR, or ~/.config/abx/personas
 *
 * @returns {string} - Absolute path to personas directory
 */
function getPersonasDir() {
    const personasDir = getEnv('PERSONAS_DIR');
    if (personasDir) return path.resolve(personasDir);
    return path.resolve(path.join(os.homedir(), '.config', 'abx', 'personas'));
}

/**
 * Parse resolution string into width/height.
 * @param {string} resolution - Resolution string like "1440,2000"
 * @returns {{width: number, height: number}} - Parsed dimensions
 */
function parseResolution(resolution) {
    const [width, height] = resolution.split(',').map(x => parseInt(x.trim(), 10));
    return { width: width || 1440, height: height || 2000 };
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
    const escape = (arg) =>
        arg.includes(' ') || arg.includes('"') || arg.includes('$')
            ? `"${arg.replace(/"/g, '\\"')}"`
            : arg;
    fs.writeFileSync(
        filePath,
        `#!/bin/bash\n${binary} ${args.map(escape).join(' ')}\n`
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
        server.on('error', reject);
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
    let lastFailure = 'no response yet';
    const host = '127.0.0.1';

    const normalizeWsUrl = (rawWsUrl) => {
        try {
            const parsed = new URL(rawWsUrl);
            if (!parsed.port) parsed.port = String(port);
            return parsed.toString();
        } catch (e) {
            return rawWsUrl;
        }
    };

    const probeDebugPort = () => new Promise((resolve, reject) => {
        const req = http.request(
            {
                host,
                port,
                path: '/json/version',
                method: 'GET',
                headers: {
                    Host: `${host}:${port}`,
                    Connection: 'close',
                },
                timeout: 5000,
            },
            (res) => {
                let data = '';
                res.on('data', (chunk) => (data += chunk));
                res.on('end', () => {
                    if ((res.statusCode || 0) >= 400) {
                        reject(new Error(`HTTP ${res.statusCode}`));
                        return;
                    }
                    try {
                        const info = JSON.parse(data);
                        if (!info?.webSocketDebuggerUrl) {
                            reject(new Error('missing webSocketDebuggerUrl in /json/version response'));
                            return;
                        }
                        info.webSocketDebuggerUrl = normalizeWsUrl(info.webSocketDebuggerUrl);
                        resolve(info);
                    } catch (error) {
                        reject(new Error(`invalid /json/version payload: ${error.message}`));
                    }
                });
            }
        );
        req.on('error', reject);
        req.on('timeout', () => {
            req.destroy(new Error('request timeout'));
        });
        req.end();
    });

    return new Promise((resolve, reject) => {
        const tryConnect = async () => {
            if (Date.now() - startTime > timeout) {
                reject(new Error(`Timeout waiting for Chrome debug port ${port} (${lastFailure})`));
                return;
            }

            try {
                const info = await probeDebugPort();
                resolve(info);
                return;
            } catch (error) {
                lastFailure = `${host}: ${error.message}`;
            }

            setTimeout(tryConnect, 100);
        };

        tryConnect();
    });
}

// ============================================================================
// Zombie process cleanup
// ============================================================================

/**
 * Kill zombie Chrome processes from stale crawls.
 * Recursively scans SNAP_DIR for any .../chrome/...pid files from stale crawls.
 * Does not assume specific directory structure - works with nested paths.
 * @param {string} [snapDir] - Snapshot directory (defaults to SNAP_DIR env or cwd)
 * @param {Object} [options={}] - Cleanup options
 * @param {string[]} [options.excludeCrawlDirs=[]] - Crawl directories to never treat as stale
 * @returns {number} - Number of zombies killed
 */
function killZombieChrome(snapDir = null, options = {}) {
    snapDir = snapDir || getSnapDir();
    const now = Date.now();
    const fiveMinutesAgo = now - 300000;
    let killed = 0;
    const excludeCrawlDirs = new Set(
        (options.excludeCrawlDirs || []).map(dir => path.resolve(dir))
    );

    console.error('[*] Checking for zombie Chrome processes...');

    if (!fs.existsSync(snapDir)) {
        console.error('[+] No snapshot directory found');
        return 0;
    }

    /**
     * Recursively find all chrome/.pid files in directory tree
     * @param {string} dir - Directory to search
     * @param {number} depth - Current recursion depth (limit to 10)
     * @returns {Array<{pidFile: string, crawlDir: string}>} - Array of PID file info
     */
    function findChromePidFiles(dir, depth = 0) {
        if (depth > 10) return [];  // Prevent infinite recursion

        const results = [];
        try {
            const entries = fs.readdirSync(dir, { withFileTypes: true });

            for (const entry of entries) {
                if (!entry.isDirectory()) continue;

                const fullPath = path.join(dir, entry.name);

                // Found a chrome directory - check for .pid files
                if (entry.name === 'chrome') {
                    try {
                        const pidFiles = fs.readdirSync(fullPath).filter(f => f.endsWith('.pid'));
                        const crawlDir = dir;  // Parent of chrome/ is the crawl dir

                        for (const pidFileName of pidFiles) {
                            results.push({
                                pidFile: path.join(fullPath, pidFileName),
                                crawlDir: crawlDir,
                            });
                        }
                    } catch (e) {
                        // Skip if can't read chrome dir
                    }
                } else {
                    // Recurse into subdirectory (skip hidden dirs and node_modules)
                    if (!entry.name.startsWith('.') && entry.name !== 'node_modules') {
                        results.push(...findChromePidFiles(fullPath, depth + 1));
                    }
                }
            }
        } catch (e) {
            // Skip if can't read directory
        }
        return results;
    }

    try {
        const chromePids = findChromePidFiles(snapDir);

        for (const {pidFile, crawlDir} of chromePids) {
            const resolvedCrawlDir = path.resolve(crawlDir);

            if (excludeCrawlDirs.has(resolvedCrawlDir)) {
                continue;
            }

            // Check if crawl was modified recently (still active)
            try {
                const crawlStats = fs.statSync(resolvedCrawlDir);
                if (crawlStats.mtimeMs > fiveMinutesAgo) {
                    continue;  // Crawl is active, skip
                }
            } catch (e) {
                continue;
            }

            // Crawl is stale, check PID
            try {
                const pid = parseInt(fs.readFileSync(pidFile, 'utf8').trim(), 10);
                if (isNaN(pid) || pid <= 0) continue;

                // Check if process exists
                try {
                    process.kill(pid, 0);
                } catch (e) {
                    // Process dead, remove stale PID file
                    try { fs.unlinkSync(pidFile); } catch (e) {}
                    continue;
                }

                // Process alive and crawl is stale - zombie!
                console.error(`[!] Found zombie (PID ${pid}) from stale crawl ${path.basename(resolvedCrawlDir)}`);

                try {
                    try { process.kill(-pid, 'SIGKILL'); } catch (e) { process.kill(pid, 'SIGKILL'); }
                    killed++;
                    console.error(`[+] Killed zombie (PID ${pid})`);
                    try { fs.unlinkSync(pidFile); } catch (e) {}
                } catch (e) {
                    console.error(`[!] Failed to kill PID ${pid}: ${e.message}`);
                }
            } catch (e) {
                // Skip invalid PID files
            }
        }
    } catch (e) {
        console.error(`[!] Error scanning for Chrome processes: ${e.message}`);
    }

    if (killed > 0) {
        console.error(`[+] Killed ${killed} zombie process(es)`);
    } else {
        console.error('[+] No zombies found');
    }

    // Clean up stale SingletonLock files from persona chrome_user_data directories
    const personasDir = getPersonasDir();
    if (fs.existsSync(personasDir)) {
        try {
            const personas = fs.readdirSync(personasDir, { withFileTypes: true });
            for (const persona of personas) {
                if (!persona.isDirectory()) continue;

                const userDataDir = path.join(personasDir, persona.name, 'chrome_user_data');
                const singletonLock = path.join(userDataDir, 'SingletonLock');

                if (fs.existsSync(singletonLock)) {
                    try {
                        fs.unlinkSync(singletonLock);
                        console.error(`[+] Removed stale SingletonLock: ${singletonLock}`);
                    } catch (e) {
                        // Ignore - may be in use by active Chrome
                    }
                }
            }
        } catch (e) {
            // Ignore errors scanning personas directory
        }
    }

    return killed;
}

// ============================================================================
// Chrome launching
// ============================================================================

/**
 * Launch Chromium with extensions and return connection info.
 *
 * @param {Object} options - Launch options
 * @param {string} [options.binary] - Chrome binary path (auto-detected if not provided)
 * @param {string} [options.outputDir='chrome'] - Directory for output files
 * @param {string} [options.userDataDir] - Chrome user data directory for persistent sessions
 * @param {string} [options.resolution='1440,2000'] - Window resolution
 * @param {boolean} [options.headless=true] - Run in headless mode
 * @param {boolean} [options.sandbox=true] - Enable Chrome sandbox
 * @param {boolean} [options.checkSsl=true] - Check SSL certificates
 * @param {string[]} [options.extensionPaths=[]] - Paths to unpacked extensions
 * @param {boolean} [options.killZombies=true] - Kill zombie processes first
 * @returns {Promise<Object>} - {success, cdpUrl, pid, port, process, error}
 */
async function launchChromium(options = {}) {
    const {
        binary = findChromium(),
        outputDir = 'chrome',
        userDataDir = getEnv('CHROME_USER_DATA_DIR'),
        resolution = getEnv('CHROME_RESOLUTION') || getEnv('RESOLUTION', '1440,2000'),
        userAgent = getEnv('CHROME_USER_AGENT') || getEnv('USER_AGENT', ''),
        headless = getEnvBool('CHROME_HEADLESS', true),
        sandbox = getEnvBool('CHROME_SANDBOX', true),
        checkSsl = getEnvBool('CHROME_CHECK_SSL_VALIDITY', getEnvBool('CHECK_SSL_VALIDITY', true)),
        extensionPaths = [],
        killZombies = true,
    } = options;

    if (!binary) {
        return { success: false, error: 'Chrome binary not found' };
    }

    const downloadsDir = getEnv('CHROME_DOWNLOADS_DIR');

    // Kill zombies first
    if (killZombies) {
        killZombieChrome(getSnapDir(), {
            excludeCrawlDirs: [getCrawlDir()],
        });
    }

    const { width, height } = parseResolution(resolution);

    // Create output directory
    if (!fs.existsSync(outputDir)) {
        fs.mkdirSync(outputDir, { recursive: true });
    }

    // Create user data directory if specified and doesn't exist
    if (userDataDir) {
        if (!fs.existsSync(userDataDir)) {
            fs.mkdirSync(userDataDir, { recursive: true });
            console.error(`[*] Created user data directory: ${userDataDir}`);
        }
        // Clean up any stale SingletonLock file from previous crashed sessions
        const singletonLock = path.join(userDataDir, 'SingletonLock');
        if (fs.existsSync(singletonLock)) {
            try {
                fs.unlinkSync(singletonLock);
                console.error(`[*] Removed stale SingletonLock: ${singletonLock}`);
            } catch (e) {
                console.error(`[!] Failed to remove SingletonLock: ${e.message}`);
            }
        }
        if (downloadsDir) {
            try {
                const defaultProfileDir = path.join(userDataDir, 'Default');
                const prefsPath = path.join(defaultProfileDir, 'Preferences');
                fs.mkdirSync(defaultProfileDir, { recursive: true });
                let prefs = {};
                if (fs.existsSync(prefsPath)) {
                    try {
                        prefs = JSON.parse(fs.readFileSync(prefsPath, 'utf-8'));
                    } catch (e) {
                        prefs = {};
                    }
                }
                prefs.download = prefs.download || {};
                prefs.download.default_directory = downloadsDir;
                prefs.download.prompt_for_download = false;
                fs.writeFileSync(prefsPath, JSON.stringify(prefs));
                console.error(`[*] Set Chrome download directory: ${downloadsDir}`);
            } catch (e) {
                console.error(`[!] Failed to set Chrome download directory: ${e.message}`);
            }
        }
    }

    // Find a free port
    const debugPort = await findFreePort();
    console.error(`[*] Using debug port: ${debugPort}`);

    // Get base Chrome args from config (static flags from CHROME_ARGS env var)
    // These come from config.json defaults, merged by get_config() in Python
    const baseArgs = getEnvArray('CHROME_ARGS', []);

    // Get extra user-provided args
    const extraArgs = getEnvArray('CHROME_ARGS_EXTRA', []);

    // Build dynamic Chrome arguments (these must be computed at runtime)
    const dynamicArgs = [
        // Remote debugging setup
        `--remote-debugging-port=${debugPort}`,
        '--remote-debugging-address=127.0.0.1',

        // Sandbox settings
        ...(sandbox ? [] : ['--no-sandbox', '--disable-setuid-sandbox']),

        // Docker-specific workarounds
        '--disable-dev-shm-usage',

        // Window size
        `--window-size=${width},${height}`,

        // User data directory (for persistent sessions with persona)
        ...(userDataDir ? [`--user-data-dir=${userDataDir}`] : []),

        // User agent
        ...(userAgent ? [`--user-agent=${userAgent}`] : []),

        // Headless mode
        ...(headless ? ['--headless=new'] : []),

        // SSL certificate checking
        ...(checkSsl ? [] : ['--ignore-certificate-errors']),
    ];

    // Combine all args: base (from config) + dynamic (runtime) + extra (user overrides)
    // Dynamic args come after base so they can override if needed
    const chromiumArgs = [...baseArgs, ...dynamicArgs, ...extraArgs];

    // Ensure keychain prompts are disabled on macOS
    if (!chromiumArgs.includes('--use-mock-keychain')) {
        chromiumArgs.push('--use-mock-keychain');
    }

    // Add extension loading flags
    if (extensionPaths.length > 0) {
        const extPathsArg = extensionPaths.join(',');
        chromiumArgs.push(`--load-extension=${extPathsArg}`);
        chromiumArgs.push('--enable-unsafe-extension-debugging');
        chromiumArgs.push('--disable-features=DisableLoadExtensionCommandLineSwitch,ExtensionManifestV2Unsupported,ExtensionManifestV2Disabled');
        console.error(`[*] Loading ${extensionPaths.length} extension(s) via --load-extension`);
    }

    chromiumArgs.push('about:blank');

    // Write command script for debugging
    writeCmdScript(path.join(outputDir, 'cmd.sh'), binary, chromiumArgs);

    try {
        console.error(`[*] Spawning Chromium (headless=${headless})...`);
        const chromiumProcess = spawn(binary, chromiumArgs, {
            stdio: ['ignore', 'pipe', 'pipe'],
            detached: true,
        });

        const chromePid = chromiumProcess.pid;
        const chromeStartTime = Date.now() / 1000;

        if (chromePid) {
            console.error(`[*] Chromium spawned (PID: ${chromePid})`);
            writePidWithMtime(path.join(outputDir, 'chrome.pid'), chromePid, chromeStartTime);
        }

        // Pipe Chrome output to stderr
        chromiumProcess.stdout.on('data', (data) => {
            process.stderr.write(`[chromium:stdout] ${data}`);
        });
        chromiumProcess.stderr.on('data', (data) => {
            process.stderr.write(`[chromium:stderr] ${data}`);
        });

        // Wait for debug port
        console.error(`[*] Waiting for debug port ${debugPort}...`);
        const debugProbeTimeoutMs = getEnvInt('CHROME_DEBUG_PORT_TIMEOUT_MS', 30000);
        const versionInfo = await waitForDebugPort(debugPort, debugProbeTimeoutMs);
        const wsUrl = versionInfo.webSocketDebuggerUrl;

        console.error(`[+] Chromium ready: ${wsUrl}`);

        fs.writeFileSync(path.join(outputDir, 'cdp_url.txt'), wsUrl);
        fs.writeFileSync(path.join(outputDir, 'port.txt'), String(debugPort));

        return {
            success: true,
            cdpUrl: wsUrl,
            pid: chromePid,
            port: debugPort,
            process: chromiumProcess,
        };
    } catch (e) {
        return { success: false, error: `${e.name}: ${e.message}` };
    }
}

/**
 * Check if a process is still running.
 * @param {number} pid - Process ID to check
 * @returns {boolean} - True if process exists
 */
function isProcessAlive(pid) {
    try {
        process.kill(pid, 0);  // Signal 0 checks existence without killing
        return true;
    } catch (e) {
        return false;
    }
}

async function acquireSessionLock(lockFile, timeoutMs = 10000, intervalMs = 100) {
    const startedAt = Date.now();
    const token = `${process.pid}:${startedAt}:${Math.random().toString(16).slice(2)}`;
    const staleLockMs = Math.max(2000, intervalMs * 10);

    while (Date.now() - startedAt < timeoutMs) {
        try {
            const fd = fs.openSync(lockFile, 'wx');
            fs.writeFileSync(fd, JSON.stringify({ pid: process.pid, token, createdAt: new Date().toISOString() }));
            fs.closeSync(fd);
            return () => {
                try {
                    const current = JSON.parse(fs.readFileSync(lockFile, 'utf-8'));
                    if (current?.token === token) {
                        fs.unlinkSync(lockFile);
                    }
                } catch (error) {}
            };
        } catch (error) {
            if (error?.code !== 'EEXIST') throw error;
            try {
                const current = JSON.parse(fs.readFileSync(lockFile, 'utf-8'));
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
    const { execSync } = require('child_process');
    const pids = [];

    try {
        // Find all Chrome processes using this debug port
        const output = execSync(
            `ps aux | grep -i "chrome.*--remote-debugging-port=${port}" | grep -v grep | awk '{print $2}'`,
            { encoding: 'utf8', timeout: 5000 }
        );

        for (const line of output.split('\n')) {
            const pid = parseInt(line.trim(), 10);
            if (!isNaN(pid) && pid > 0) {
                pids.push(pid);
            }
        }
    } catch (e) {
        // Command failed or no processes found
    }

    return pids;
}

/**
 * Kill a Chrome process by PID.
 * Always sends SIGTERM before SIGKILL, then verifies death.
 *
 * @param {number} pid - Process ID to kill
 * @param {string} [outputDir] - Directory containing PID files to clean up
 */
async function killChrome(pid, outputDir = null) {
    if (!pid) return;

    console.error(`[*] Killing Chrome process tree (PID ${pid})...`);

    // Get debug port for finding child processes
    let debugPort = null;
    if (outputDir) {
        try {
            const portFile = path.join(outputDir, 'port.txt');
            if (fs.existsSync(portFile)) {
                debugPort = parseInt(fs.readFileSync(portFile, 'utf8').trim(), 10);
            }
        } catch (e) {}
    }

    // Step 1: SIGTERM to process group (graceful shutdown)
    console.error(`[*] Sending SIGTERM to process group -${pid}...`);
    try {
        process.kill(-pid, 'SIGTERM');
    } catch (e) {
        try {
            console.error(`[*] Process group kill failed, trying single process...`);
            process.kill(pid, 'SIGTERM');
        } catch (e2) {
            console.error(`[!] SIGTERM failed: ${e2.message}`);
        }
    }

    // Step 2: Wait for graceful shutdown
    await new Promise(resolve => setTimeout(resolve, 2000));

    // Step 3: Check if still alive
    if (!isProcessAlive(pid)) {
        console.error('[+] Chrome process terminated gracefully');
    } else {
        // Step 4: Force kill ENTIRE process group with SIGKILL
        console.error(`[*] Process still alive, sending SIGKILL to process group -${pid}...`);
        try {
            process.kill(-pid, 'SIGKILL');  // Kill entire process group
        } catch (e) {
            console.error(`[!] Process group SIGKILL failed, trying single process: ${e.message}`);
            try {
                process.kill(pid, 'SIGKILL');
            } catch (e2) {
                console.error(`[!] SIGKILL failed: ${e2.message}`);
            }
        }

        // Step 5: Wait briefly and verify death
        await new Promise(resolve => setTimeout(resolve, 1000));

        if (isProcessAlive(pid)) {
            console.error(`[!] WARNING: Process ${pid} is unkillable (likely in UNE state)`);
            console.error(`[!] This typically happens when Chrome crashes in kernel syscall`);
            console.error(`[!] Process will remain as zombie until system reboot`);
            console.error(`[!] macOS IOSurface crash creates unkillable processes in UNE state`);

            // Try one more time to kill the entire process group
            if (debugPort) {
                const relatedPids = findChromeProcessesByPort(debugPort);
                if (relatedPids.length > 1) {
                    console.error(`[*] Found ${relatedPids.length} Chrome processes still running on port ${debugPort}`);
                    console.error(`[*] Attempting final process group SIGKILL...`);

                    // Try to kill each unique process group we find
                    const processGroups = new Set();
                    for (const relatedPid of relatedPids) {
                        if (relatedPid !== pid) {
                            processGroups.add(relatedPid);
                        }
                    }

                    for (const groupPid of processGroups) {
                        try {
                            process.kill(-groupPid, 'SIGKILL');
                        } catch (e) {}
                    }
                }
            }
        } else {
            console.error('[+] Chrome process group killed successfully');
        }
    }

    // Step 8: Clean up PID files
    // Note: hook-specific .pid files are cleaned up by run_hook() and Snapshot.cleanup()
    if (outputDir) {
        try { fs.unlinkSync(path.join(outputDir, 'chrome.pid')); } catch (e) {}
        try { fs.unlinkSync(path.join(outputDir, 'port.txt')); } catch (e) {}
    }

    console.error('[*] Chrome cleanup completed');
}

/**
 * Install Chromium using @puppeteer/browsers programmatic API.
 * Uses puppeteer's default cache location, returns the binary path.
 *
 * @param {Object} options - Install options
 * @returns {Promise<Object>} - {success, binary, version, error}
 */
async function installChromium(options = {}) {
    // Check if CHROME_BINARY is already set and valid
    const configuredBinary = getEnv('CHROME_BINARY');
    if (configuredBinary && fs.existsSync(configuredBinary)) {
        console.error(`[+] Using configured CHROME_BINARY: ${configuredBinary}`);
        return { success: true, binary: configuredBinary, version: null };
    }

    // Try to load @puppeteer/browsers from NODE_MODULES_DIR or system
    let puppeteerBrowsers;
    try {
        if (process.env.NODE_MODULES_DIR) {
            module.paths.unshift(process.env.NODE_MODULES_DIR);
        }
        puppeteerBrowsers = require('@puppeteer/browsers');
    } catch (e) {
        console.error(`[!] @puppeteer/browsers not found. Install it first with installPuppeteerCore.`);
        return { success: false, error: '@puppeteer/browsers not installed' };
    }

    console.error(`[*] Installing Chromium via @puppeteer/browsers...`);

    try {
        const result = await puppeteerBrowsers.install({
            browser: 'chromium',
            buildId: 'latest',
        });

        const binary = result.executablePath;
        const version = result.buildId;

        if (!binary || !fs.existsSync(binary)) {
            console.error(`[!] Chromium binary not found at: ${binary}`);
            return { success: false, error: `Chromium binary not found at: ${binary}` };
        }

        console.error(`[+] Chromium installed: ${binary}`);
        return { success: true, binary, version };
    } catch (e) {
        console.error(`[!] Failed to install Chromium: ${e.message}`);
        return { success: false, error: e.message };
    }
}

/**
 * Install puppeteer-core npm package.
 *
 * @param {Object} options - Install options
 * @param {string} [options.npmPrefix] - npm prefix directory (default: LIB_DIR/npm)
 * @param {number} [options.timeout=60000] - Timeout in milliseconds
 * @returns {Promise<Object>} - {success, path, error}
 */
async function installPuppeteerCore(options = {}) {
    const arch = `${process.arch}-${process.platform}`;
    const defaultPrefix = path.join(getLibDir(), 'npm');
    const {
        npmPrefix = defaultPrefix,
        timeout = 60000,
    } = options;

    const nodeModulesDir = path.join(npmPrefix, 'node_modules');
    const puppeteerPath = path.join(nodeModulesDir, 'puppeteer-core');

    // Check if already installed
    if (fs.existsSync(puppeteerPath)) {
        console.error(`[+] puppeteer-core already installed: ${puppeteerPath}`);
        return { success: true, path: puppeteerPath };
    }

    console.error(`[*] Installing puppeteer-core to ${npmPrefix}...`);

    // Create directory
    if (!fs.existsSync(npmPrefix)) {
        fs.mkdirSync(npmPrefix, { recursive: true });
    }

    try {
        const { execSync } = require('child_process');
        execSync(
            `npm install --prefix "${npmPrefix}" puppeteer-core`,
            { encoding: 'utf8', timeout, stdio: ['pipe', 'pipe', 'pipe'] }
        );
        console.error(`[+] puppeteer-core installed successfully`);
        return { success: true, path: puppeteerPath };
    } catch (e) {
        console.error(`[!] Failed to install puppeteer-core: ${e.message}`);
        return { success: false, error: e.message };
    }
}

// Try to import unzipper, fallback to system unzip if not available
let unzip = null;
try {
    const unzipper = require('unzipper');
    unzip = async (sourcePath, destPath) => {
        const stream = fs.createReadStream(sourcePath).pipe(unzipper.Extract({ path: destPath }));
        return stream.promise();
    };
} catch (err) {
    // Will use system unzip command as fallback
}

/**
 * Compute the extension ID from the unpacked path.
 * Chrome uses a SHA256 hash of the unpacked extension directory path to compute a dynamic id.
 *
 * @param {string} unpacked_path - Path to the unpacked extension directory
 * @returns {string} - 32-character extension ID
 */
function getExtensionId(unpacked_path) {
    let resolved_path = unpacked_path;
    try {
        resolved_path = fs.realpathSync(unpacked_path);
    } catch (err) {
        // Use the provided path if realpath fails
        resolved_path = unpacked_path;
    }
    // Chrome uses a SHA256 hash of the unpacked extension directory path
    const hash = crypto.createHash('sha256');
    hash.update(Buffer.from(resolved_path, 'utf-8'));

    // Convert first 32 hex chars to characters in the range 'a'-'p'
    const detected_extension_id = Array.from(hash.digest('hex'))
        .slice(0, 32)
        .map(i => String.fromCharCode(parseInt(i, 16) + 'a'.charCodeAt(0)))
        .join('');

    return detected_extension_id;
}

/**
 * Download and install a Chrome extension from the Chrome Web Store.
 *
 * @param {Object} extension - Extension metadata object
 * @param {string} extension.webstore_id - Chrome Web Store extension ID
 * @param {string} extension.name - Human-readable extension name
 * @param {string} extension.crx_url - URL to download the CRX file
 * @param {string} extension.crx_path - Local path to save the CRX file
 * @param {string} extension.unpacked_path - Path to extract the extension
 * @returns {Promise<boolean>} - True if installation succeeded
 */
async function installExtension(extension) {
    const manifest_path = path.join(extension.unpacked_path, 'manifest.json');

    // Download CRX file if not already downloaded
    if (!fs.existsSync(manifest_path) && !fs.existsSync(extension.crx_path)) {
        console.log(`[🛠️] Downloading missing extension ${extension.name} ${extension.webstore_id} -> ${extension.crx_path}`);

        try {
            // Ensure parent directory exists
            const crxDir = path.dirname(extension.crx_path);
            if (!fs.existsSync(crxDir)) {
                fs.mkdirSync(crxDir, { recursive: true });
            }

            // Download CRX file from Chrome Web Store
            let downloaded = false;
            try {
                const response = await fetch(extension.crx_url);
                if (response.ok && response.body) {
                    const crx_file = fs.createWriteStream(extension.crx_path);
                    const crx_stream = Readable.fromWeb(response.body);
                    await finished(crx_stream.pipe(crx_file));
                    downloaded = true;
                } else {
                    console.warn(`[⚠️] fetch failed for ${extension.name}: HTTP ${response.status}`);
                }
            } catch (fetchErr) {
                console.warn(`[⚠️] fetch failed for ${extension.name}, trying curl: ${fetchErr.message}`);
            }

            // Fallback to curl when fetch (Node undici) fails (e.g. DNS/proxy issues)
            if (!downloaded) {
                try {
                    await execAsync(`curl -sL -o "${extension.crx_path}" "${extension.crx_url}" --connect-timeout 30`);
                    downloaded = fs.existsSync(extension.crx_path) && fs.statSync(extension.crx_path).size > 0;
                } catch (curlErr) {
                    console.error(`[❌] curl fallback also failed for ${extension.name}: ${curlErr.message}`);
                }
            }

            if (!downloaded) {
                console.warn(`[⚠️] Failed to download extension ${extension.name}`);
                return false;
            }
        } catch (err) {
            console.error(`[❌] Failed to download extension ${extension.name}:`, err);
            return false;
        }
    }

    // Unzip CRX file to unpacked_path (CRX files have extra header bytes but unzip handles it)
    await fs.promises.mkdir(extension.unpacked_path, { recursive: true });

    try {
        // Use -q to suppress warnings about extra bytes in CRX header
        await execAsync(`/usr/bin/unzip -q -o "${extension.crx_path}" -d "${extension.unpacked_path}"`);
    } catch (err1) {
        // unzip may return non-zero even on success due to CRX header warning, check if manifest exists
        if (!fs.existsSync(manifest_path)) {
            if (unzip) {
                // Fallback to unzipper library
                try {
                    await unzip(extension.crx_path, extension.unpacked_path);
                } catch (err2) {
                    console.error(`[❌] Failed to unzip ${extension.crx_path}:`, err2.message);
                    return false;
                }
            } else {
                console.error(`[❌] Failed to unzip ${extension.crx_path}:`, err1.message);
                return false;
            }
        }
    }

    if (!fs.existsSync(manifest_path)) {
        console.error(`[❌] Failed to install ${extension.crx_path}: could not find manifest.json in unpacked_path`);
        return false;
    }

    return true;
}

/**
 * Load or install a Chrome extension, computing all metadata.
 *
 * @param {Object} ext - Partial extension metadata (at minimum: webstore_id or unpacked_path)
 * @param {string} [ext.webstore_id] - Chrome Web Store extension ID
 * @param {string} [ext.name] - Human-readable extension name
 * @param {string} [ext.unpacked_path] - Path to unpacked extension
 * @param {string} [extensions_dir] - Directory to store extensions
 * @returns {Promise<Object>} - Complete extension metadata object
 */
async function loadOrInstallExtension(ext, extensions_dir = null) {
    if (!(ext.webstore_id || ext.unpacked_path)) {
        throw new Error('Extension must have either {webstore_id} or {unpacked_path}');
    }

    // Determine extensions directory
    // Use provided dir, or fall back to getExtensionsDir() which handles env vars and defaults
    const EXTENSIONS_DIR = extensions_dir || getExtensionsDir();

    // Set statically computable extension metadata
    ext.webstore_id = ext.webstore_id || ext.id;
    ext.name = ext.name || ext.webstore_id;
    ext.webstore_url = ext.webstore_url || `https://chromewebstore.google.com/detail/${ext.webstore_id}`;
    ext.crx_url = ext.crx_url || `https://clients2.google.com/service/update2/crx?response=redirect&prodversion=1230&acceptformat=crx3&x=id%3D${ext.webstore_id}%26uc`;
    ext.crx_path = ext.crx_path || path.join(EXTENSIONS_DIR, `${ext.webstore_id}__${ext.name}.crx`);
    ext.unpacked_path = ext.unpacked_path || path.join(EXTENSIONS_DIR, `${ext.webstore_id}__${ext.name}`);

    const manifest_path = path.join(ext.unpacked_path, 'manifest.json');
    ext.read_manifest = () => JSON.parse(fs.readFileSync(manifest_path, 'utf-8'));
    ext.read_version = () => fs.existsSync(manifest_path) && ext.read_manifest()?.version || null;

    // If extension is not installed, download and unpack it
    if (!ext.read_version()) {
        await installExtension(ext);
    }

    // Autodetect ID from filesystem path (unpacked extensions don't have stable IDs)
    ext.id = getExtensionId(ext.unpacked_path);
    ext.version = ext.read_version();

    if (!ext.version) {
        console.warn(`[❌] Unable to detect ID and version of installed extension ${ext.unpacked_path}`);
    } else {
        console.log(`[➕] Installed extension ${ext.name} (${ext.version})... ${ext.unpacked_path}`);
    }

    return ext;
}

/**
 * Check if a Puppeteer target is an extension background page/service worker.
 *
 * @param {Object} target - Puppeteer target object
 * @returns {Promise<Object>} - Object with target_is_bg, extension_id, manifest_version, etc.
 */
const CHROME_EXTENSION_URL_PREFIX = 'chrome-extension://';
const EXTENSION_BACKGROUND_TARGET_TYPES = new Set(['service_worker', 'background_page']);

/**
 * Parse extension ID from a target URL.
 *
 * @param {string|null|undefined} targetUrl - URL from Puppeteer target
 * @returns {string|null} - Extension ID if URL is a chrome-extension URL
 */
function getExtensionIdFromUrl(targetUrl) {
    if (!targetUrl || !targetUrl.startsWith(CHROME_EXTENSION_URL_PREFIX)) return null;
    return targetUrl.slice(CHROME_EXTENSION_URL_PREFIX.length).split('/')[0] || null;
}

/**
 * Filter extension list to entries with unpacked paths.
 *
 * @param {Array} extensions - Extension metadata list
 * @returns {Array} - Extensions with unpacked_path
 */
function getValidInstalledExtensions(extensions) {
    if (!Array.isArray(extensions) || extensions.length === 0) return [];
    return extensions.filter(ext => ext?.unpacked_path);
}

async function tryGetExtensionContext(target, targetType) {
    if (targetType === 'service_worker') return await target.worker();
    return await target.page();
}

async function waitForExtensionTargetType(browser, extensionId, targetType, timeout) {
    const target = await browser.waitForTarget(
        candidate => candidate.type() === targetType &&
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
 * @returns {Promise<Object>} - Puppeteer target
 */
async function waitForExtensionTargetHandle(browser, extensionId, timeout = 30000) {
    return await browser.waitForTarget(
        target =>
            getExtensionIdFromUrl(target.url()) === extensionId &&
            (EXTENSION_BACKGROUND_TARGET_TYPES.has(target.type()) ||
                target.url().startsWith(CHROME_EXTENSION_URL_PREFIX)),
        { timeout }
    );
}

async function isTargetExtension(target) {
    let target_type;
    let target_ctx;
    let target_url;

    try {
        target_type = target.type();
        target_ctx = (await target.worker()) || (await target.page()) || null;
        target_url = target.url() || target_ctx?.url() || null;
    } catch (err) {
        if (String(err).includes('No target with given id found')) {
            // Target closed during check, ignore harmless race condition
            target_type = 'closed';
            target_ctx = null;
            target_url = 'about:closed';
        } else {
            throw err;
        }
    }

    // Check if this is an extension background page or service worker
    const extension_id = getExtensionIdFromUrl(target_url);
    const is_chrome_extension = Boolean(extension_id);
    const is_background_page = target_type === 'background_page';
    const is_service_worker = target_type === 'service_worker';
    const target_is_bg = is_chrome_extension && (is_background_page || is_service_worker);

    let manifest_version = null;
    let manifest = null;
    let manifest_name = null;
    const target_is_extension = is_chrome_extension || target_is_bg;

    if (target_is_extension) {
        try {
            if (target_ctx) {
                manifest = await target_ctx.evaluate(() => chrome.runtime.getManifest());
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
async function loadExtensionFromTarget(extensions, target) {
    const {
        target_is_bg,
        target_is_extension,
        target_type,
        target_ctx,
        target_url,
        extension_id,
        manifest_version,
        manifest,
    } = await isTargetExtension(target);

    if (!(target_is_bg && extension_id && target_ctx)) {
        return null;
    }

    // Find matching extension in our list
    const extension = extensions.find(ext => ext.id === extension_id);
    if (!extension) {
        console.warn(`[⚠️] Found loaded extension ${extension_id} that's not in CHROME_EXTENSIONS list`);
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
                tab = tab || (await new Promise((resolve) =>
                    chrome.tabs.query({ currentWindow: true, active: true }, ([tab]) => resolve(tab))
                ));

                // Manifest V3: chrome.action
                if (chrome.action?.onClicked?.dispatch) {
                    return await chrome.action.onClicked.dispatch(tab);
                }

                // Manifest V2: chrome.browserAction
                if (chrome.browserAction?.onClicked?.dispatch) {
                    return await chrome.browserAction.onClicked.dispatch(tab);
                }

                throw new Error('Extension action dispatch not available');
            }, tab || null);
        },

        // Send message to extension
        dispatchMessage: async (message, options = {}) => {
            return await target_ctx.evaluate((msg, opts) => {
                return new Promise((resolve) => {
                    chrome.runtime.sendMessage(msg, opts, (response) => {
                        resolve(response);
                    });
                });
            }, message, options);
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

    console.log(`[🔌] Connected to extension ${extension.name} (${extension.version})`);

    return new_extension;
}

/**
 * Install all extensions in the list if not already installed.
 *
 * @param {Array} extensions - Array of extension metadata objects
 * @param {string} [extensions_dir] - Directory to store extensions
 * @returns {Promise<Array>} - Array of installed extension objects
 */
async function installAllExtensions(extensions, extensions_dir = null) {
    console.log(`[⚙️] Installing ${extensions.length} chrome extensions...`);

    for (const extension of extensions) {
        await loadOrInstallExtension(extension, extensions_dir);
    }

    return extensions;
}

/**
 * Load and connect to all extensions from a running browser.
 *
 * @param {Object} browser - Puppeteer browser instance
 * @param {Array} extensions - Array of extension metadata objects
 * @returns {Promise<Array>} - Array of loaded extension objects with connection handlers
 */
async function loadAllExtensionsFromBrowser(browser, extensions, timeout = 30000) {
    console.log(`[⚙️] Loading ${extensions.length} chrome extensions from browser...`);

    for (const extension of getValidInstalledExtensions(extensions)) {
        if (!extension.id) {
            throw new Error(`Extension ${extension.name || extension.unpacked_path} missing id`);
        }
        const target = await waitForExtensionTargetHandle(browser, extension.id, timeout);
        await loadExtensionFromTarget(extensions, target);
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
    const manifest_path = path.join(unpacked_path, 'manifest.json');

    if (!fs.existsSync(manifest_path)) {
        return null;
    }

    try {
        const manifest_content = fs.readFileSync(manifest_path, 'utf-8');
        return JSON.parse(manifest_content);
    } catch (error) {
        // Invalid JSON or read error
        return null;
    }
}

/**
 * @deprecated Use puppeteer's enableExtensions option instead.
 *
 * Generate Chrome launch arguments for loading extensions.
 * NOTE: This is deprecated. Use puppeteer.launch({ pipe: true, enableExtensions: [paths] }) instead.
 *
 * @param {Array} extensions - Array of extension metadata objects
 * @returns {Array<string>} - Chrome CLI arguments for loading extensions
 */
function getExtensionLaunchArgs(extensions) {
    console.warn('[DEPRECATED] getExtensionLaunchArgs is deprecated. Use puppeteer enableExtensions option instead.');
    const validExtensions = getValidInstalledExtensions(extensions);
    if (validExtensions.length === 0) return [];

    const unpacked_paths = validExtensions.map(ext => ext.unpacked_path);
    // Use computed id (from path hash) for allowlisting, as that's what Chrome uses for unpacked extensions
    // Fall back to webstore_id if computed id not available
    const extension_ids = validExtensions.map(ext => ext.id || getExtensionId(ext.unpacked_path));

    return [
        `--load-extension=${unpacked_paths.join(',')}`,
        `--allowlisted-extension-id=${extension_ids.join(',')}`,
        '--allow-legacy-extension-manifests',
        '--disable-extensions-auto-update',
    ];
}

/**
 * Get extension paths for use with puppeteer's enableExtensions option.
 * Following puppeteer best practices: https://pptr.dev/guides/chrome-extensions
 *
 * @param {Array} extensions - Array of extension metadata objects
 * @returns {Array<string>} - Array of extension unpacked paths
 */
function getExtensionPaths(extensions) {
    return getValidInstalledExtensions(extensions).map(ext => ext.unpacked_path);
}

/**
 * Wait for an extension target to be available in the browser.
 * Following puppeteer best practices for accessing extension contexts.
 *
 * For Manifest V3 extensions (service workers):
 *   const worker = await waitForExtensionTarget(browser, extensionId);
 *   // worker is a WebWorker context
 *
 * For Manifest V2 extensions (background pages):
 *   const page = await waitForExtensionTarget(browser, extensionId);
 *   // page is a Page context
 *
 * @param {Object} browser - Puppeteer browser instance
 * @param {string} extensionId - Extension ID to wait for (computed from path hash)
 * @param {number} [timeout=30000] - Timeout in milliseconds
 * @returns {Promise<Object>} - Worker or Page context for the extension
 */
async function waitForExtensionTarget(browser, extensionId, timeout = 30000) {
    for (const targetType of EXTENSION_BACKGROUND_TARGET_TYPES) {
        try {
            const context = await waitForExtensionTargetType(browser, extensionId, targetType, timeout);
            if (context) return context;
        } catch (err) {
            // Continue to next extension target type
        }
    }

    // Try any extension page as fallback
    const extTarget = await waitForExtensionTargetHandle(browser, extensionId, timeout);

    // Return worker or page depending on target type
    return await tryGetExtensionContext(extTarget, extTarget.type());
}

/**
 * Read extensions metadata from chrome session directory.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @returns {Array<Object>|null} - Parsed extensions metadata list or null if unavailable
 */
function readExtensionsMetadata(chromeSessionDir) {
    const extensionsFile = path.join(path.resolve(chromeSessionDir), 'extensions.json');
    if (!fs.existsSync(extensionsFile)) return null;
    try {
        const parsed = JSON.parse(fs.readFileSync(extensionsFile, 'utf8'));
        return Array.isArray(parsed) ? parsed : null;
    } catch (e) {
        return null;
    }
}

/**
 * Wait for extensions metadata to be written by chrome launch hook.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @param {number} [timeoutMs=10000] - Timeout in milliseconds
 * @param {number} [intervalMs=250] - Poll interval in milliseconds
 * @returns {Promise<Array<Object>>} - Parsed extensions metadata list
 * @throws {Error} - If metadata file is not available in time
 */
async function waitForExtensionsMetadata(chromeSessionDir, timeoutMs = 10000, intervalMs = 250) {
    const startTime = Date.now();
    while (Date.now() - startTime < timeoutMs) {
        const metadata = readExtensionsMetadata(chromeSessionDir);
        if (metadata && metadata.length > 0) return metadata;
        await new Promise(resolve => setTimeout(resolve, intervalMs));
    }
    throw new Error(`Timeout waiting for extensions metadata in ${chromeSessionDir}`);
}

/**
 * Find extension metadata entry by name.
 *
 * @param {Array<Object>} extensions - Parsed extensions metadata list
 * @param {string} extensionName - Extension name to match
 * @returns {Object|null} - Matching extension metadata entry
 */
function findExtensionMetadataByName(extensions, extensionName) {
    const wanted = (extensionName || '').toLowerCase();
    return extensions.find(ext => (ext?.name || '').toLowerCase() === wanted) || null;
}

/**
 * Get all loaded extension targets from a browser.
 *
 * @param {Object} browser - Puppeteer browser instance
 * @returns {Array<Object>} - Array of extension target info objects
 */
function getExtensionTargets(browser) {
    return browser.targets()
        .filter(target =>
            getExtensionIdFromUrl(target.url()) ||
            EXTENSION_BACKGROUND_TARGET_TYPES.has(target.type())
        )
        .map(target => ({
            type: target.type(),
            url: target.url(),
            extensionId: getExtensionIdFromUrl(target.url()),
        }));
}

/**
 * Find Chromium binary path.
 * Checks CHROME_BINARY env var first, then falls back to system locations.
 *
 * @returns {string|null} - Absolute path to browser binary or null if not found
 */
function findChromium() {
    const { execSync } = require('child_process');

    // Helper to validate a binary by running --version
    const validateBinary = (binaryPath) => {
        if (!binaryPath || !fs.existsSync(binaryPath)) return false;
        try {
            execSync(`"${binaryPath}" --version`, { encoding: 'utf8', timeout: 5000, stdio: 'pipe' });
            return true;
        } catch (e) {
            return false;
        }
    };

    // 1. Check CHROME_BINARY env var first
    const chromeBinary = getEnv('CHROME_BINARY');
    if (chromeBinary) {
        const absPath = path.resolve(chromeBinary);
        if (absPath.includes('Google Chrome') || absPath.includes('google-chrome')) {
            console.error('[!] Warning: CHROME_BINARY points to Chrome. Chromium is required for extension support.');
        } else if (validateBinary(absPath)) {
            return absPath;
        }
        console.error(`[!] Warning: CHROME_BINARY="${chromeBinary}" is not valid`);
    }

    // 2. Warn that no CHROME_BINARY is configured, searching fallbacks
    if (!chromeBinary) {
        console.error('[!] Warning: CHROME_BINARY not set, searching system locations...');
    }

    // Helper to find Chromium in @puppeteer/browsers directory structure
    const findInPuppeteerDir = (baseDir) => {
        if (!fs.existsSync(baseDir)) return null;
        try {
            const versions = fs.readdirSync(baseDir);
            for (const version of versions.sort().reverse()) {
                const versionDir = path.join(baseDir, version);
                const candidates = [
                    path.join(versionDir, 'chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium'),
                    path.join(versionDir, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium'),
                    path.join(versionDir, 'chrome-mac-x64/Chromium.app/Contents/MacOS/Chromium'),
                    path.join(versionDir, 'chrome-linux64/chrome'),
                    path.join(versionDir, 'chrome-linux/chrome'),
                ];
                for (const c of candidates) {
                    if (fs.existsSync(c)) return c;
                }
            }
        } catch (e) {}
        return null;
    };

    // 3. Search fallback locations (Chromium only)
    const fallbackLocations = [
        // System Chromium
        '/Applications/Chromium.app/Contents/MacOS/Chromium',
        '/usr/bin/chromium',
        '/usr/bin/chromium-browser',
        // Puppeteer cache
        path.join(process.env.HOME || '', '.cache/puppeteer/chromium'),
        path.join(process.env.HOME || '', '.cache/puppeteer'),
    ];

    for (const loc of fallbackLocations) {
        // Check if it's a puppeteer cache dir
        if (loc.includes('.cache/puppeteer')) {
            const binary = findInPuppeteerDir(loc);
            if (binary && validateBinary(binary)) {
                return binary;
            }
        } else if (validateBinary(loc)) {
            return loc;
        }
    }

    return null;
}

/**
 * Find Chromium binary path only (never Chrome/Brave/Edge).
 * Prefers CHROME_BINARY if set, then Chromium.
 *
 * @returns {string|null} - Absolute path or command name to browser binary
 */
function findAnyChromiumBinary() {
    const chromiumBinary = findChromium();
    if (chromiumBinary) return chromiumBinary;
    return null;
}

// ============================================================================
// Shared Extension Installer Utilities
// ============================================================================

/**
 * Get the extensions directory path.
 * Centralized path calculation used by extension installers and chrome launch.
 *
 * Path is derived from environment variables in this priority:
 * 1. CHROME_EXTENSIONS_DIR (explicit override)
 * 2. PERSONAS_DIR/ACTIVE_PERSONA/chrome_extensions (default)
 *
 * @returns {string} - Absolute path to extensions directory
 */
function getExtensionsDir() {
    const personasDir = getPersonasDir();
    const persona = getEnv('ACTIVE_PERSONA', 'Default');
    return getEnv('CHROME_EXTENSIONS_DIR') ||
        path.join(personasDir, persona, 'chrome_extensions');
}

/**
 * Get machine type string for platform-specific paths.
 * Matches Python's archivebox.config.paths.get_machine_type()
 *
 * @returns {string} - Machine type (e.g., 'x86_64-linux', 'arm64-darwin')
 */
function getMachineType() {
    if (process.env.MACHINE_TYPE) {
        return process.env.MACHINE_TYPE;
    }

    let machine = process.arch;
    const system = process.platform;

    // Normalize machine type to match Python's convention
    if (machine === 'arm64' || machine === 'aarch64') {
        machine = 'arm64';
    } else if (machine === 'x64' || machine === 'x86_64' || machine === 'amd64') {
        machine = 'x86_64';
    } else if (machine === 'ia32' || machine === 'x86') {
        machine = 'x86';
    }

    return `${machine}-${system}`;
}

/**
 * Get LIB_DIR path for shared binaries and caches.
 * Returns ~/.config/abx/lib by default.
 *
 * @returns {string} - Absolute path to lib directory
 */
function getLibDir() {
    if (process.env.LIB_DIR) {
        return path.resolve(process.env.LIB_DIR);
    }
    const defaultRoot = path.join(os.homedir(), '.config', 'abx', 'lib');
    return path.resolve(defaultRoot);
}

/**
 * Get NODE_MODULES_DIR path for npm packages.
 * Returns LIB_DIR/npm/node_modules/
 *
 * @returns {string} - Absolute path to node_modules directory
 */
function getNodeModulesDir() {
    if (process.env.NODE_MODULES_DIR) {
        return path.resolve(process.env.NODE_MODULES_DIR);
    }
    return path.resolve(path.join(getLibDir(), 'npm', 'node_modules'));
}

/**
 * Get all test environment paths as a JSON object.
 * This is the single source of truth for path calculations - Python calls this
 * to avoid duplicating path logic.
 *
 * @returns {Object} - Object with all test environment paths
 */
function getTestEnv() {
    const snapDir = getSnapDir();
    const crawlDir = getCrawlDir();
    const machineType = getMachineType();
    const libDir = getLibDir();
    const nodeModulesDir = getNodeModulesDir();

    return {
        SNAP_DIR: snapDir,
        CRAWL_DIR: crawlDir,
        PERSONAS_DIR: getPersonasDir(),
        MACHINE_TYPE: machineType,
        LIB_DIR: libDir,
        NODE_MODULES_DIR: nodeModulesDir,
        NODE_PATH: nodeModulesDir,  // Node.js uses NODE_PATH for module resolution
        NPM_BIN_DIR: path.join(libDir, 'npm', '.bin'),
        CHROME_EXTENSIONS_DIR: getExtensionsDir(),
    };
}

/**
 * Install a Chrome extension with caching support.
 *
 * This is the main entry point for extension installer hooks. It handles:
 * - Checking for cached extension metadata
 * - Installing the extension if not cached
 * - Writing cache file for future runs
 *
 * @param {Object} extension - Extension metadata object
 * @param {string} extension.webstore_id - Chrome Web Store extension ID
 * @param {string} extension.name - Human-readable extension name (used for cache file)
 * @param {Object} [options] - Options
 * @param {string} [options.extensionsDir] - Override extensions directory
 * @param {boolean} [options.quiet=false] - Suppress info logging
 * @returns {Promise<Object|null>} - Installed extension metadata or null on failure
 */
async function installExtensionWithCache(extension, options = {}) {
    const {
        extensionsDir = getExtensionsDir(),
        quiet = false,
    } = options;

    const cacheFile = path.join(extensionsDir, `${extension.name}.extension.json`);

    // Check if extension is already cached and valid
    if (fs.existsSync(cacheFile)) {
        try {
            const cached = JSON.parse(fs.readFileSync(cacheFile, 'utf-8'));
            const manifestPath = path.join(cached.unpacked_path, 'manifest.json');

            if (fs.existsSync(manifestPath)) {
                if (!quiet) {
                    console.log(`[*] ${extension.name} extension already installed (using cache)`);
                }
                return cached;
            }
        } catch (e) {
            // Cache file corrupted, re-install
            console.warn(`[⚠️] Extension cache corrupted for ${extension.name}, re-installing...`);
        }
    }

    // Install extension
    if (!quiet) {
        console.log(`[*] Installing ${extension.name} extension...`);
    }

    const installedExt = await loadOrInstallExtension(extension, extensionsDir);

    if (!installedExt?.version) {
        console.error(`[❌] Failed to install ${extension.name} extension`);
        return null;
    }

    // Write cache file
    try {
        await fs.promises.mkdir(extensionsDir, { recursive: true });
        await fs.promises.writeFile(cacheFile, JSON.stringify(installedExt, null, 2));
        if (!quiet) {
            console.log(`[+] Extension metadata written to ${cacheFile}`);
        }
    } catch (e) {
        console.warn(`[⚠️] Failed to write cache file: ${e.message}`);
    }

    if (!quiet) {
        console.log(`[+] ${extension.name} extension installed`);
    }

    return installedExt;
}

// ============================================================================
// Snapshot Hook Utilities (for CDP-based plugins like ssl, responses, dns)
// ============================================================================

const CHROME_SESSION_FILES = Object.freeze({
    cdpUrl: 'cdp_url.txt',
    targetId: 'target_id.txt',
    chromePid: 'chrome.pid',
    pageLoaded: 'page_loaded.txt',
});

/**
 * Parse command line arguments into an object.
 * Handles --key=value and --flag formats.
 *
 * @returns {Object} - Parsed arguments object
 */
/**
 * Resolve all session marker file paths for a chrome session directory.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @returns {{sessionDir: string, cdpFile: string, targetIdFile: string, chromePidFile: string, pageLoadedFile: string}}
 */
function getChromeSessionPaths(chromeSessionDir) {
    const sessionDir = path.resolve(chromeSessionDir);
    return {
        sessionDir,
        cdpFile: path.join(sessionDir, CHROME_SESSION_FILES.cdpUrl),
        targetIdFile: path.join(sessionDir, CHROME_SESSION_FILES.targetId),
        chromePidFile: path.join(sessionDir, CHROME_SESSION_FILES.chromePid),
        pageLoadedFile: path.join(sessionDir, CHROME_SESSION_FILES.pageLoaded),
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
    const value = fs.readFileSync(filePath, 'utf8').trim();
    return value || null;
}

/**
 * Read the current chrome session state from marker files.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @returns {{sessionDir: string, cdpUrl: string|null, targetId: string|null, pid: number|null}}
 */
function readChromeSessionState(chromeSessionDir) {
    const sessionPaths = getChromeSessionPaths(chromeSessionDir);
    const cdpUrl = readSessionTextFile(sessionPaths.cdpFile);
    const targetId = readSessionTextFile(sessionPaths.targetIdFile);
    const rawPid = readSessionTextFile(sessionPaths.chromePidFile);
    const parsedPid = rawPid ? parseInt(rawPid, 10) : NaN;
    const pid = Number.isFinite(parsedPid) && parsedPid > 0 ? parsedPid : null;

    return {
        sessionDir: sessionPaths.sessionDir,
        cdpUrl,
        targetId,
        pid,
    };
}

/**
 * Return the session-related artifact files that may need cleanup when stale.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @returns {string[]} - Absolute file paths
 */
function getChromeSessionArtifactPaths(chromeSessionDir) {
    const { sessionDir, cdpFile, targetIdFile, chromePidFile, pageLoadedFile } = getChromeSessionPaths(chromeSessionDir);
    return [
        cdpFile,
        targetIdFile,
        chromePidFile,
        pageLoadedFile,
        path.join(sessionDir, 'port.txt'),
        path.join(sessionDir, 'url.txt'),
        path.join(sessionDir, 'final_url.txt'),
        path.join(sessionDir, 'navigation.json'),
        path.join(sessionDir, 'extensions.json'),
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
 * Inspect whether session marker files refer to a still-live Chrome session.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @param {Object} [options={}] - Validation options
 * @param {boolean} [options.requireTargetId=false] - Require target ID marker to consider the session healthy
 * @param {number} [options.probeTimeoutMs=1500] - Timeout for probing the CDP endpoint
 * @returns {Promise<{hasArtifacts: boolean, stale: boolean, state: Object, reason: string|null}>}
 */
async function inspectChromeSessionArtifacts(chromeSessionDir, options = {}) {
    const {
        requireTargetId = false,
        probeTimeoutMs = 1500,
    } = options;

    const artifactPaths = getChromeSessionArtifactPaths(chromeSessionDir);
    const hasArtifacts = artifactPaths.some(filePath => fs.existsSync(filePath));
    const state = readChromeSessionState(chromeSessionDir);

    if (!hasArtifacts) {
        return { hasArtifacts: false, stale: false, state, reason: null };
    }

    if (!state.cdpUrl) {
        return { hasArtifacts: true, stale: true, state, reason: 'missing cdp_url.txt' };
    }

    if (requireTargetId && !state.targetId) {
        return { hasArtifacts: true, stale: true, state, reason: 'missing target_id.txt' };
    }

    if (state.pid && !isProcessAlive(state.pid)) {
        return { hasArtifacts: true, stale: true, state, reason: `chrome pid ${state.pid} is not running` };
    }

    if (fs.existsSync(getChromeSessionPaths(chromeSessionDir).chromePidFile) && !state.pid) {
        return { hasArtifacts: true, stale: true, state, reason: 'invalid chrome.pid' };
    }

    const debugPort = getChromeDebugPortFromCdpUrl(state.cdpUrl);
    if (!debugPort) {
        return { hasArtifacts: true, stale: true, state, reason: `invalid cdp url: ${state.cdpUrl}` };
    }

    try {
        await waitForDebugPort(debugPort, probeTimeoutMs);
        return { hasArtifacts: true, stale: false, state, reason: null };
    } catch (error) {
        return {
            hasArtifacts: true,
            stale: true,
            state,
            reason: `cdp unreachable on port ${debugPort}: ${error.message}`,
        };
    }
}

/**
 * Delete stale Chrome session marker files, but leave healthy sessions untouched.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @param {Object} [options={}] - Validation options
 * @param {boolean} [options.requireTargetId=false] - Require target ID marker to consider the session healthy
 * @param {number} [options.probeTimeoutMs=1500] - Timeout for probing the CDP endpoint
 * @returns {Promise<{hasArtifacts: boolean, stale: boolean, state: Object, reason: string|null, cleanedFiles: string[]}>}
 */
async function cleanupStaleChromeSessionArtifacts(chromeSessionDir, options = {}) {
    const inspection = await inspectChromeSessionArtifacts(chromeSessionDir, options);
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
 * Check if a chrome session state satisfies required fields.
 *
 * @param {{cdpUrl: string|null, targetId: string|null, pid: number|null}} state - Session state
 * @param {Object} [options={}] - Validation options
 * @param {boolean} [options.requireTargetId=false] - Require target ID marker
 * @param {boolean} [options.requirePid=false] - Require PID marker
 * @param {boolean} [options.requireAlivePid=false] - Require PID to be alive
 * @returns {boolean} - True if state is valid
 */
function isValidChromeSessionState(state, options = {}) {
    const {
        requireTargetId = false,
        requirePid = false,
        requireAlivePid = false,
    } = options;

    if (!state?.cdpUrl) return false;
    if (requireTargetId && !state.targetId) return false;
    if ((requirePid || requireAlivePid) && !state.pid) return false;
    if (requireAlivePid) {
        try {
            process.kill(state.pid, 0);
        } catch (e) {
            return false;
        }
    }
    return true;
}

/**
 * Wait for a chrome session state to satisfy required fields.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @param {Object} [options={}] - Wait/validation options
 * @param {number} [options.timeoutMs=60000] - Timeout in milliseconds
 * @param {number} [options.intervalMs=100] - Poll interval in milliseconds
 * @param {boolean} [options.requireTargetId=false] - Require target ID marker
 * @param {boolean} [options.requirePid=false] - Require PID marker
 * @param {boolean} [options.requireAlivePid=false] - Require PID to be alive
 * @returns {Promise<{sessionDir: string, cdpUrl: string|null, targetId: string|null, pid: number|null}|null>}
 */
async function waitForChromeSessionState(chromeSessionDir, options = {}) {
    const {
        timeoutMs = 60000,
        intervalMs = 100,
        requireTargetId = false,
        requirePid = false,
        requireAlivePid = false,
    } = options;
    const startTime = Date.now();

    while (Date.now() - startTime < timeoutMs) {
        const state = readChromeSessionState(chromeSessionDir);
        if (isValidChromeSessionState(state, { requireTargetId, requirePid, requireAlivePid })) {
            return state;
        }
        await new Promise(resolve => setTimeout(resolve, intervalMs));
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
    for (const moduleName of ['puppeteer-core', 'puppeteer']) {
        try {
            return require(moduleName);
        } catch (e) {}
    }
    throw new Error('Missing puppeteer dependency (need puppeteer-core or puppeteer)');
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
        connectOptions = {},
    } = options;

    const browser = await puppeteer.connect({
        browserWSEndpoint,
        ...connectOptions,
    });
    try {
        return await operation(browser);
    } finally {
        await browser.disconnect();
    }
}

function fetchDevtoolsTargets(cdpUrl) {
    const port = getChromeDebugPortFromCdpUrl(cdpUrl);
    if (!port) {
        return Promise.resolve([]);
    }

    return new Promise((resolve, reject) => {
        const req = http.get(
            { hostname: '127.0.0.1', port, path: '/json/list' },
            (res) => {
                let data = '';
                res.on('data', (chunk) => (data += chunk));
                res.on('end', () => {
                    try {
                        const targets = JSON.parse(data);
                        resolve(Array.isArray(targets) ? targets : []);
                    } catch (error) {
                        reject(error);
                    }
                });
            }
        );
        req.on('error', reject);
    });
}

function devtoolsHttpRequest(cdpUrl, requestPath, method = 'GET') {
    const port = getChromeDebugPortFromCdpUrl(cdpUrl);
    if (!port) {
        return Promise.reject(new Error(`Invalid CDP URL: ${cdpUrl}`));
    }

    return new Promise((resolve, reject) => {
        const req = http.request(
            { hostname: '127.0.0.1', port, path: requestPath, method },
            (res) => {
                let data = '';
                res.on('data', (chunk) => (data += chunk));
                res.on('end', () => {
                    if (res.statusCode && res.statusCode >= 400) {
                        reject(new Error(`DevTools HTTP ${res.statusCode}: ${data || requestPath}`));
                        return;
                    }
                    resolve(data);
                });
            }
        );
        req.on('error', reject);
        req.end();
    });
}

async function createDevtoolsPageTarget(cdpUrl, initialUrl = 'about:blank') {
    const encodedUrl = encodeURIComponent(initialUrl);
    const response = await devtoolsHttpRequest(cdpUrl, `/json/new?${encodedUrl}`, 'PUT');
    const target = JSON.parse(response || '{}');
    if (!target?.id) {
        throw new Error('Failed to create DevTools page target');
    }
    return target;
}

async function getDevtoolsTargetById(cdpUrl, targetId) {
    if (!targetId) return null;
    const targets = await fetchDevtoolsTargets(cdpUrl);
    return targets.find((target) => target?.id === targetId) || null;
}

async function closeDevtoolsPageTarget(cdpUrl, targetId) {
    if (!cdpUrl || !targetId) {
        return false;
    }

    for (const method of ['PUT', 'GET']) {
        try {
            await devtoolsHttpRequest(cdpUrl, `/json/close/${targetId}`, method);
            return true;
        } catch (error) {}
    }

    return false;
}

function getTargetIdFromTarget(target) {
    if (!target) return null;
    return target._targetId || target._targetInfo?.targetId || null;
}

function getTargetIdFromPage(page) {
    if (!page || typeof page.target !== 'function') return null;
    try {
        return getTargetIdFromTarget(page.target());
    } catch (error) {
        return null;
    }
}

async function waitForNewPageTarget(browser, previousTargetIds = new Set(), timeoutMs = 5000) {
    const deadline = Date.now() + Math.max(timeoutMs, 0);

    while (Date.now() <= deadline) {
        const pages = await browser.pages();
        for (const page of pages) {
            const targetId = getTargetIdFromPage(page);
            if (targetId && !previousTargetIds.has(targetId)) {
                return { page, targetId };
            }
        }
        await sleep(100);
    }

    return { page: null, targetId: null };
}

async function waitForNewDevtoolsPageTarget(cdpUrl, previousTargetIds = new Set(), timeoutMs = 5000) {
    const deadline = Date.now() + Math.max(timeoutMs, 0);

    while (Date.now() <= deadline) {
        const targets = await fetchDevtoolsTargets(cdpUrl);
        const target = targets.find((candidate) => {
            if (candidate?.type !== 'page') return false;
            if (!candidate.id) return false;
            return !previousTargetIds.has(candidate.id);
        });
        if (target?.id) {
            return target;
        }
        await sleep(100);
    }

    return null;
}

async function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function resolvePageByTargetId(browser, targetId, timeoutMs = 0) {
    const deadline = Date.now() + Math.max(timeoutMs, 0);

    while (true) {
        const targets = browser.targets();
        const target = targets.find(candidate => getTargetIdFromTarget(candidate) === targetId);
        if (target) {
            try {
                const page = await target.page();
                if (page) {
                    return page;
                }
            } catch (error) {}
        }

        const pages = await browser.pages();
        const pageMatch = pages.find(page => getTargetIdFromPage(page) === targetId);
        if (pageMatch) {
            return pageMatch;
        }

        if (Date.now() >= deadline) {
            return null;
        }

        await sleep(100);
    }
}

/**
 * Wait for Chrome session files to be ready.
 * Polls for cdp_url.txt and optionally target_id.txt in the chrome session directory.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory (e.g., '../chrome')
 * @param {number} [timeoutMs=60000] - Timeout in milliseconds
 * @param {boolean} [requireTargetId=true] - Whether target_id.txt must exist
 * @returns {Promise<boolean>} - True if files are ready, false if timeout
 */
async function waitForChromeSession(chromeSessionDir, timeoutMs = 60000, requireTargetId = true) {
    const state = await waitForChromeSessionState(chromeSessionDir, { timeoutMs, requireTargetId });
    return Boolean(state);
}

/**
 * Read CDP WebSocket URL from chrome session directory.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @returns {string|null} - CDP URL or null if not found
 */
function readCdpUrl(chromeSessionDir) {
    const { cdpFile } = getChromeSessionPaths(chromeSessionDir);
    return readSessionTextFile(cdpFile);
}

/**
 * Read target ID from chrome session directory.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @returns {string|null} - Target ID or null if not found
 */
function readTargetId(chromeSessionDir) {
    const { targetIdFile } = getChromeSessionPaths(chromeSessionDir);
    return readSessionTextFile(targetIdFile);
}

/**
 * Read Chrome PID from chrome session directory.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @returns {number|null} - PID or null if invalid/missing
 */
function readChromePid(chromeSessionDir) {
    return readChromeSessionState(chromeSessionDir).pid;
}

/**
 * Resolve the active crawl-level Chrome session.
 *
 * @param {string} [crawlBaseDir='.'] - Crawl root directory
 * @returns {{cdpUrl: string, pid: number, crawlChromeDir: string}}
 * @throws {Error} - If session files are missing/invalid or process is dead
 */
function getCrawlChromeSession(crawlBaseDir = '.') {
    const crawlChromeDir = path.join(path.resolve(crawlBaseDir), 'chrome');
    const state = readChromeSessionState(crawlChromeDir);
    if (!isValidChromeSessionState(state, { requirePid: true, requireAlivePid: true })) {
        throw new Error(CHROME_SESSION_REQUIRED_ERROR);
    }
    return { cdpUrl: state.cdpUrl, pid: state.pid, crawlChromeDir };
}

/**
 * Wait for an active crawl-level Chrome session.
 *
 * @param {number} timeoutMs - Timeout in milliseconds
 * @param {Object} [options={}] - Optional settings
 * @param {number} [options.intervalMs=250] - Poll interval in ms
 * @param {string} [options.crawlBaseDir='.'] - Crawl root directory
 * @returns {Promise<{cdpUrl: string, pid: number, crawlChromeDir: string}>}
 * @throws {Error} - If timeout reached
 */
async function waitForCrawlChromeSession(timeoutMs, options = {}) {
    const intervalMs = options.intervalMs || 250;
    const crawlBaseDir = options.crawlBaseDir || '.';
    const crawlChromeDir = path.join(path.resolve(crawlBaseDir), 'chrome');
    const state = await waitForChromeSessionState(crawlChromeDir, {
        timeoutMs,
        intervalMs,
        requirePid: true,
        requireAlivePid: true,
    });
    if (!state) throw new Error(CHROME_SESSION_REQUIRED_ERROR);
    return { cdpUrl: state.cdpUrl, pid: state.pid, crawlChromeDir };
}

/**
 * Open a new tab in an existing Chrome session.
 *
 * @param {Object} options - Tab open options
 * @param {string} options.cdpUrl - Browser CDP websocket URL
 * @param {Object} options.puppeteer - Puppeteer module
 * @returns {Promise<{targetId: string}>}
 */
async function openTabInChromeSession(options = {}) {
    const { cdpUrl, puppeteer } = options;
    if (!cdpUrl) {
        throw new Error(CHROME_SESSION_REQUIRED_ERROR);
    }
    if (puppeteer) {
        requirePuppeteerModule(puppeteer, 'openTabInChromeSession');
    }
    const target = await createDevtoolsPageTarget(cdpUrl, 'about:blank');
    return { targetId: target.id };
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
    if (await closeDevtoolsPageTarget(cdpUrl, targetId)) {
        return true;
    }
    const puppeteerModule = requirePuppeteerModule(puppeteer, 'closeTabInChromeSession');

    return withConnectedBrowser(
        {
            puppeteer: puppeteerModule,
            browserWSEndpoint: cdpUrl,
        },
        async (browser) => {
        const pages = await browser.pages();
        const page = pages.find(p => getTargetIdFromPage(p) === targetId);
        if (!page) {
            return false;
        }
        await page.close();
        return true;
        }
    );
}

/**
 * Connect to Chrome browser and find the target page.
 * This is a high-level utility that handles all the connection logic:
 * 1. Wait for chrome session files
 * 2. Connect to browser via CDP
 * 3. Find the target page by ID
 *
 * @param {Object} options - Connection options
 * @param {string} [options.chromeSessionDir='../chrome'] - Path to chrome session directory
 * @param {number} [options.timeoutMs=60000] - Timeout for waiting
 * @param {boolean} [options.requireTargetId=true] - Require target_id.txt in session dir
 * @param {Object} [options.puppeteer] - Puppeteer module (preferred explicit form)
 * @param {Object} [options.puppeteerModule] - Backward-compatible puppeteer module key
 * @returns {Promise<Object>} - { browser, page, targetId, cdpUrl }
 * @throws {Error} - If connection fails or page not found
 */
async function connectToPage(options = {}) {
    const {
        chromeSessionDir = '../chrome',
        timeoutMs = 60000,
        requireTargetId = true,
        puppeteer,
        puppeteerModule,
    } = options;

    // Support both key names and fall back to local resolution for compatibility
    // with older callers that may omit explicit module injection.
    const resolvedPuppeteer = puppeteer || puppeteerModule || resolvePuppeteerModule();
    const state = await waitForChromeSessionState(chromeSessionDir, { timeoutMs, requireTargetId });
    if (!state) {
        throw new Error(CHROME_SESSION_REQUIRED_ERROR);
    }
    let targetId = state.targetId;

    let devtoolsTarget = null;
    if (targetId) {
        devtoolsTarget = await getDevtoolsTargetById(state.cdpUrl, targetId);
        if (!devtoolsTarget && requireTargetId) {
            throw new Error(`Target ${targetId} not found in Chrome session`);
        }
    }

    // Connect to browser
    const browser = await resolvedPuppeteer.connect({ browserWSEndpoint: state.cdpUrl });

    try {
        // Find the target page
        let page = null;

        if (targetId) {
            page = await resolvePageByTargetId(browser, targetId, Math.min(timeoutMs, 5000));
            if (!page && requireTargetId) {
                const pages = await browser.pages();
                if (devtoolsTarget?.url) {
                    page = pages.find((candidate) => candidate.url() === devtoolsTarget.url) || null;
                }
            }
            if (!page && requireTargetId) {
                throw new Error(`Target ${targetId} not found in Chrome session`);
            }
        }

        const pages = await browser.pages();
        if (!page) {
            page = pages[pages.length - 1];
        }

        if (!page) {
            throw new Error('No page found in browser');
        }

        return { browser, page, targetId, cdpUrl: state.cdpUrl };
    } catch (error) {
        // connectToPage hands ownership of browser to callers on success;
        // disconnect here only for failures that happen before handoff.
        try {
            await browser.disconnect();
        } catch (disconnectError) {}
        throw error;
    }
}

/**
 * Wait for page navigation to complete.
 * Polls for page_loaded.txt marker file written by chrome_navigate.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @param {number} [timeoutMs=120000] - Timeout in milliseconds
 * @param {number} [postLoadDelayMs=0] - Additional delay after page load marker
 * @returns {Promise<void>}
 * @throws {Error} - If timeout waiting for navigation
 */
async function waitForPageLoaded(chromeSessionDir, timeoutMs = 120000, postLoadDelayMs = 0) {
    const { pageLoadedFile } = getChromeSessionPaths(chromeSessionDir);
    const pollInterval = 100;
    let waitTime = 0;

    while (!fs.existsSync(pageLoadedFile) && waitTime < timeoutMs) {
        await new Promise(resolve => setTimeout(resolve, pollInterval));
        waitTime += pollInterval;
    }

    if (!fs.existsSync(pageLoadedFile)) {
        throw new Error('Timeout waiting for navigation (chrome_navigate did not complete)');
    }

    // Optional post-load delay for late responses
    if (postLoadDelayMs > 0) {
        await new Promise(resolve => setTimeout(resolve, postLoadDelayMs));
    }
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
    const timeoutMs = options.timeoutMs || getEnvInt('CDP_COOKIE_TIMEOUT_MS', 10000);
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
            const result = await session.send('Storage.getCookies');
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
    launchChromium,
    killChrome,
    // Chromium install
    installChromium,
    installPuppeteerCore,
    // Chromium binary finding
    findChromium,
    findAnyChromiumBinary,
    // Extension utilities
    getExtensionId,
    loadExtensionManifest,
    installExtension,
    loadOrInstallExtension,
    isTargetExtension,
    loadExtensionFromTarget,
    installAllExtensions,
    loadAllExtensionsFromBrowser,
    waitForExtensionTargetHandle,
    // New puppeteer best-practices helpers
    getExtensionPaths,
    waitForExtensionTarget,
    getExtensionTargets,
    readExtensionsMetadata,
    waitForExtensionsMetadata,
    findExtensionMetadataByName,
    // Shared path utilities (single source of truth for Python/JS)
    getMachineType,
    getLibDir,
    getNodeModulesDir,
    getExtensionsDir,
    getTestEnv,
    // Shared extension installer utilities
    installExtensionWithCache,
    // Deprecated - use enableExtensions option instead
    getExtensionLaunchArgs,
    // Snapshot hook utilities (for CDP-based plugins)
    parseArgs,
    inspectChromeSessionArtifacts,
    cleanupStaleChromeSessionArtifacts,
    waitForChromeSessionState,
    waitForChromeSession,
    readCdpUrl,
    readTargetId,
    readChromePid,
    getCrawlChromeSession,
    waitForCrawlChromeSession,
    openTabInChromeSession,
    closeTabInChromeSession,
    getTargetIdFromTarget,
    getTargetIdFromPage,
    fetchDevtoolsTargets,
    createDevtoolsPageTarget,
    getDevtoolsTargetById,
    closeDevtoolsPageTarget,
    waitForNewPageTarget,
    waitForNewDevtoolsPageTarget,
    connectToPage,
    waitForPageLoaded,
    getCookiesViaCdp,
};

// CLI usage
if (require.main === module) {
    const args = process.argv.slice(2);

    if (args.length === 0) {
        console.log('Usage: chrome_utils.js <command> [args...]');
        console.log('');
        console.log('Commands:');
        console.log('  findChromium              Find Chromium binary');
        console.log('  installChromium           Install Chromium via @puppeteer/browsers');
        console.log('  installPuppeteerCore      Install puppeteer-core npm package');
        console.log('  launchChromium            Launch Chrome with CDP debugging');
        console.log('  getCookiesViaCdp <port>  Read browser cookies via CDP port');
        console.log('  getCrawlChromeSession    Resolve active crawl chrome session');
        console.log('  killChrome <pid>          Kill Chrome process by PID');
        console.log('  killZombieChrome          Clean up zombie Chrome processes');
        console.log('');
        console.log('  getMachineType            Get machine type (e.g., x86_64-linux)');
        console.log('  getLibDir                 Get LIB_DIR path');
        console.log('  getNodeModulesDir         Get NODE_MODULES_DIR path');
        console.log('  getExtensionsDir          Get Chrome extensions directory');
        console.log('  getTestEnv                Get all paths as JSON (for tests)');
        console.log('');
        console.log('  getExtensionId <path>     Get extension ID from unpacked path');
        console.log('  loadExtensionManifest     Load extension manifest.json');
        console.log('  loadOrInstallExtension    Load or install an extension');
        console.log('  installExtensionWithCache Install extension with caching');
        console.log('');
        console.log('Environment variables:');
        console.log('  SNAP_DIR                  Base snapshot directory');
        console.log('  CRAWL_DIR                 Base crawl directory');
        console.log('  PERSONAS_DIR              Personas directory');
        console.log('  LIB_DIR                   Library directory (computed if not set)');
        console.log('  MACHINE_TYPE              Machine type override');
        console.log('  NODE_MODULES_DIR          Node modules directory');
        console.log('  CHROME_BINARY             Chrome binary path');
        console.log('  CHROME_EXTENSIONS_DIR     Extensions directory');
        process.exit(1);
    }

    const [command, ...commandArgs] = args;

    (async () => {
        try {
            switch (command) {
                case 'findChromium': {
                    const binary = findChromium();
                    if (binary) {
                        console.log(binary);
                    } else {
                        console.error('Chromium binary not found');
                        process.exit(1);
                    }
                    break;
                }

                case 'installChromium': {
                    const result = await installChromium();
                    if (result.success) {
                        console.log(JSON.stringify({
                            binary: result.binary,
                            version: result.version,
                        }));
                    } else {
                        console.error(result.error);
                        process.exit(1);
                    }
                    break;
                }

                case 'installPuppeteerCore': {
                    const [npmPrefix] = commandArgs;
                    const result = await installPuppeteerCore({ npmPrefix: npmPrefix || undefined });
                    if (result.success) {
                        console.log(JSON.stringify({ path: result.path }));
                    } else {
                        console.error(result.error);
                        process.exit(1);
                    }
                    break;
                }

                case 'launchChromium': {
                    const [outputDir, extensionPathsJson] = commandArgs;
                    const extensionPaths = extensionPathsJson ? JSON.parse(extensionPathsJson) : [];
                    const result = await launchChromium({
                        outputDir: outputDir || 'chrome',
                        extensionPaths,
                    });
                    if (result.success) {
                        console.log(JSON.stringify({
                            cdpUrl: result.cdpUrl,
                            pid: result.pid,
                            port: result.port,
                        }));
                    } else {
                        console.error(result.error);
                        process.exit(1);
                    }
                    break;
                }

                case 'getCookiesViaCdp': {
                    const [portStr] = commandArgs;
                    const port = parseInt(portStr, 10);
                    if (isNaN(port) || port <= 0) {
                        console.error('Invalid port');
                        process.exit(1);
                    }
                    const cookies = await getCookiesViaCdp(port);
                    console.log(JSON.stringify(cookies));
                    break;
                }

                case 'getCrawlChromeSession': {
                    const [crawlBaseDir] = commandArgs;
                    const session = getCrawlChromeSession(crawlBaseDir || getEnv('CRAWL_DIR', '.'));
                    console.log(JSON.stringify(session));
                    break;
                }

                case 'killChrome': {
                    const [pidStr, outputDir] = commandArgs;
                    const pid = parseInt(pidStr, 10);
                    if (isNaN(pid)) {
                        console.error('Invalid PID');
                        process.exit(1);
                    }
                    await killChrome(pid, outputDir);
                    break;
                }

                case 'killZombieChrome': {
                    const [snapDir] = commandArgs;
                    const killed = killZombieChrome(snapDir);
                    console.log(killed);
                    break;
                }

                case 'getExtensionId': {
                    const [unpacked_path] = commandArgs;
                    const id = getExtensionId(unpacked_path);
                    console.log(id);
                    break;
                }

                case 'loadExtensionManifest': {
                    const [unpacked_path] = commandArgs;
                    const manifest = loadExtensionManifest(unpacked_path);
                    console.log(JSON.stringify(manifest));
                    break;
                }

                case 'getExtensionLaunchArgs': {
                    const [extensions_json] = commandArgs;
                    const extensions = JSON.parse(extensions_json);
                    const launchArgs = getExtensionLaunchArgs(extensions);
                    console.log(JSON.stringify(launchArgs));
                    break;
                }

                case 'loadOrInstallExtension': {
                    const [webstore_id, name, extensions_dir] = commandArgs;
                    const ext = await loadOrInstallExtension({ webstore_id, name }, extensions_dir);
                    console.log(JSON.stringify(ext, null, 2));
                    break;
                }

                case 'waitForExtensionsMetadata': {
                    const [chromeSessionDir = '.', timeoutMsStr = '10000'] = commandArgs;
                    const timeoutMs = parseInt(timeoutMsStr, 10);
                    if (isNaN(timeoutMs) || timeoutMs <= 0) {
                        console.error('Invalid timeoutMs');
                        process.exit(1);
                    }
                    const metadata = await waitForExtensionsMetadata(chromeSessionDir, timeoutMs);
                    console.log(JSON.stringify(metadata));
                    break;
                }

                case 'getMachineType': {
                    console.log(getMachineType());
                    break;
                }

                case 'getLibDir': {
                    console.log(getLibDir());
                    break;
                }

                case 'getNodeModulesDir': {
                    console.log(getNodeModulesDir());
                    break;
                }

                case 'getExtensionsDir': {
                    console.log(getExtensionsDir());
                    break;
                }

                case 'getTestEnv': {
                    console.log(JSON.stringify(getTestEnv(), null, 2));
                    break;
                }

                case 'installExtensionWithCache': {
                    const [webstore_id, name] = commandArgs;
                    if (!webstore_id || !name) {
                        console.error('Usage: installExtensionWithCache <webstore_id> <name>');
                        process.exit(1);
                    }
                    const ext = await installExtensionWithCache({ webstore_id, name });
                    if (ext) {
                        console.log(JSON.stringify(ext, null, 2));
                    } else {
                        process.exit(1);
                    }
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
