#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Record all DNS traffic (hostname -> IP resolutions) during page load.
 *
 * This hook sets up CDP listeners BEFORE chrome_navigate loads the page,
 * then waits for navigation to complete. The listeners capture all DNS
 * resolutions by extracting hostname/IP pairs from network responses.
 *
 * Usage: on_Snapshot__22_dns.daemon.bg.js --url=<url>
 * Output: Writes dns.jsonl with one line per DNS resolution record, including
 * the configured nameservers available to Chrome's resolver on this system
 */


// Cleanup can SIGTERM the process immediately after spawn; remember early
// signals and replay them to the hook-specific cleanup handler once it exists.
let __abxEarlyShutdownSignal = null;
function __abxRememberEarlyShutdown(signal) {
  if (__abxEarlyShutdownSignal === null) {
    __abxEarlyShutdownSignal = signal;
  }
}
function __abxInstallShutdownHandler(handler) {
  process.removeAllListeners("SIGTERM");
  process.removeAllListeners("SIGINT");
  process.on("SIGTERM", () => handler("SIGTERM"));
  process.on("SIGINT", () => handler("SIGINT"));
  if (__abxEarlyShutdownSignal !== null) {
    const signal = __abxEarlyShutdownSignal;
    __abxEarlyShutdownSignal = null;
    setImmediate(() => handler(signal));
  }
}
process.on("SIGTERM", () => __abxRememberEarlyShutdown("SIGTERM"));
process.on("SIGINT", () => __abxRememberEarlyShutdown("SIGINT"));

const fs = require("fs");
const path = require("path");
const dns = require("dns");

// Import generic helpers from base/utils.js
const {
  ensureNodeModuleResolution,
  getEnvBool,
  getEnvInt,
  loadConfig,
  parseArgs,
  emitArchiveResultRecord,
  emitProcessReadyRecord,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);

// Import chrome-specific utilities from chrome_utils.js
const {
  connectToPage,
  resolvePuppeteerModule,
  waitForNavigationComplete,
} = require("../chrome/chrome_utils.js");
const puppeteer = resolvePuppeteerModule();

const PLUGIN_NAME = "dns";
const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || ".").trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const OUTPUT_FILE = "dns.jsonl";
const CHROME_SESSION_DIR = "../chrome";

let browser = null;
let page = null;
let recordCount = 0;
let shuttingDown = false;
let primaryHostname = "";
let primaryIp = "";
let firstResolvedIp = "";
let keepAliveTimer = null;
let configuredNameservers = [];
let lastProgressLine = "";

function emitProgress(line) {
  if (line && line !== lastProgressLine) {
    lastProgressLine = line;
    console.log(line);
  }
}

function extractHostname(url) {
  try {
    const urlObj = new URL(url);
    return urlObj.hostname;
  } catch (e) {
    return null;
  }
}

function getConfiguredNameservers() {
  try {
    return dns.getServers().filter(Boolean);
  } catch (e) {
    return [];
  }
}

function writeDnsRecord({ hostname, ip, port = null, url = "", requestId = null, seenResolutions, source = "cdp" }) {
  if (!hostname || !ip || hostname === ip) {
    return false;
  }

  const resolutionKey = `${hostname}:${ip}`;
  if (seenResolutions.has(resolutionKey)) {
    return false;
  }
  seenResolutions.set(resolutionKey, true);

  const isIPv6 = ip.includes(":");
  if (!firstResolvedIp) {
    firstResolvedIp = ip;
  }
  if (!primaryIp && hostname === primaryHostname) {
    primaryIp = ip;
  }

  const dnsRecord = {
    ts: new Date().toISOString(),
    hostname: hostname,
    ip: ip,
    port: port || null,
    type: isIPv6 ? "AAAA" : "A",
    protocol: url.startsWith("https://") ? "https" : "http",
    url: url,
    requestId: requestId,
    source: source,
    nameservers: [...configuredNameservers],
  };

  fs.appendFileSync(path.join(OUTPUT_DIR, OUTPUT_FILE), JSON.stringify(dnsRecord) + "\n");
  recordCount += 1;
  emitProgress(`${recordCount} DNS record${recordCount === 1 ? "" : "s"}`);
  return true;
}

async function lookupTargetHost(targetUrl, seenResolutions) {
  const hostname = extractHostname(targetUrl);
  if (!hostname) {
    return;
  }

  // Newer Chromium builds can complete navigation without populating
  // response.remoteIPAddress in CDP. Use Node's real resolver as a fallback so
  // the DNS hook still records the target host instead of producing an empty file.
  for (const resolver of [dns.promises.resolve4, dns.promises.resolve6]) {
    try {
      const ips = await resolver(hostname);
      for (const ip of ips) {
        writeDnsRecord({
          hostname,
          ip,
          url: targetUrl,
          seenResolutions,
          source: "node-dns",
        });
      }
    } catch (e) {
      if (!["ENODATA", "ENOTFOUND", "ENOTIMP", "ETIMEOUT"].includes(e.code)) {
        console.error(`WARN: DNS fallback failed for ${hostname}: ${e.message}`);
      }
    }
  }
}

async function setupListener(targetUrl) {
  const outputPath = path.join(OUTPUT_DIR, OUTPUT_FILE);
  const timeout = getEnvInt("DNS_TIMEOUT", 30) * 1000;
  primaryHostname = extractHostname(targetUrl) || "";
  configuredNameservers = getConfiguredNameservers();

  // Initialize output file
  fs.writeFileSync(outputPath, "");

  // Track seen hostname -> IP mappings to avoid duplicates per request
  const seenResolutions = new Map();
  // Track request IDs to their URLs for correlation
  const requestUrls = new Map();

  // Connect to Chrome page using shared utility
  const { browser, page, cdpSession: client } = await connectToPage({
    chromeSessionDir: CHROME_SESSION_DIR,
    timeoutMs: timeout,
    puppeteer,
  });

  // Enable network domain to receive events
  await client.send("Network.enable");

  // Listen for request events to track URLs
  client.on("Network.requestWillBeSent", (params) => {
    requestUrls.set(params.requestId, params.request.url);
  });

  // Listen for response events which contain remoteIPAddress (the resolved IP)
  client.on("Network.responseReceived", (params) => {
    try {
      const response = params.response;
      const url = response.url;
      const remoteIPAddress = response.remoteIPAddress;
      const remotePort = response.remotePort;

      if (!url || !remoteIPAddress) {
        return;
      }

      const hostname = extractHostname(url);
      if (!hostname) {
        return;
      }

      // Skip if IP address is same as hostname (already an IP)
      if (hostname === remoteIPAddress) {
        return;
      }

      writeDnsRecord({
        hostname,
        ip: remoteIPAddress,
        port: remotePort,
        url,
        requestId: params.requestId,
        seenResolutions,
      });
    } catch (e) {
      // Ignore errors
    }
  });

  // Listen for failed requests too - they still involve DNS
  client.on("Network.loadingFailed", (params) => {
    try {
      const requestId = params.requestId;
      const url = requestUrls.get(requestId);

      if (!url) {
        return;
      }

      const hostname = extractHostname(url);
      if (!hostname) {
        return;
      }

      // Check if this is a DNS-related failure
      const errorText = params.errorText || "";
      if (
        errorText.includes("net::ERR_NAME_NOT_RESOLVED") ||
        errorText.includes("net::ERR_NAME_RESOLUTION_FAILED")
      ) {
        // Create a unique key for this failed resolution
        const resolutionKey = `${hostname}:NXDOMAIN`;

        // Skip if we've already recorded this NXDOMAIN
        if (seenResolutions.has(resolutionKey)) {
          return;
        }
        seenResolutions.set(resolutionKey, true);

        const timestamp = new Date().toISOString();
        const dnsRecord = {
          ts: timestamp,
          hostname: hostname,
          ip: null,
          port: null,
          type: "NXDOMAIN",
          protocol: url.startsWith("https://") ? "https" : "http",
          url: url,
          requestId: requestId,
          error: errorText,
          nameservers: [...configuredNameservers],
        };

        fs.appendFileSync(outputPath, JSON.stringify(dnsRecord) + "\n");
        recordCount += 1;
        emitProgress(
          `${recordCount} DNS record${recordCount === 1 ? "" : "s"}`
        );
      }
    } catch (e) {
      // Ignore errors
    }
  });

  return { browser, page, client, seenResolutions };
}

function emitResult(status = "succeeded") {
  if (shuttingDown) return;
  shuttingDown = true;
  const outputPath = path.join(OUTPUT_DIR, OUTPUT_FILE);
  let resolvedIp = primaryIp || firstResolvedIp || "";

  if (!resolvedIp && fs.existsSync(outputPath)) {
    for (const line of fs.readFileSync(outputPath, "utf8").split("\n")) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("{")) continue;
      try {
        const record = JSON.parse(trimmed);
        const ip = record.ip || "";
        const hostname = record.hostname || "";
        if (!ip) continue;
        if (!resolvedIp) resolvedIp = ip;
        if (primaryHostname && hostname === primaryHostname) {
          resolvedIp = ip;
          break;
        }
      } catch (e) {}
    }
  }

  emitArchiveResultRecord(status, resolvedIp);
}

async function handleShutdown(signal) {
  console.error(`\nReceived ${signal}, emitting final results...`);
  if (keepAliveTimer) {
    clearInterval(keepAliveTimer);
    keepAliveTimer = null;
  }
  emitResult("succeeded");
  if (browser) {
    try {
      browser.disconnect();
    } catch (e) {}
  }
  process.exit(0);
}

async function main() {
  const args = parseArgs();
  const url = args.url;

  if (!url) {
    console.error("Usage: on_Snapshot__22_dns.daemon.bg.js --url=<url>");
    process.exit(1);
  }

  if (!getEnvBool("DNS_ENABLED", true)) {
    console.error("Skipping (DNS_ENABLED=False)");
    emitArchiveResultRecord("skipped", "DNS_ENABLED=False");
    process.exit(0);
  }

  try {
    // Set up listener BEFORE navigation
    const connection = await setupListener(url);
    browser = connection.browser;
    page = connection.page;
    emitProgress("0 DNS records");
    emitProcessReadyRecord({ plugin: PLUGIN_DIR, output: OUTPUT_FILE });

    // Register signal handlers for graceful shutdown
    __abxInstallShutdownHandler(handleShutdown);

    // Wait for chrome_navigate to complete (non-fatal)
    try {
      const timeout = getEnvInt("DNS_TIMEOUT", 30) * 1000;
      await waitForNavigationComplete(CHROME_SESSION_DIR, timeout * 4, 500);
    } catch (e) {
      console.error(`WARN: ${e.message}`);
    }
    if (recordCount === 0) {
      await lookupTargetHost(url, connection.seenResolutions);
    }

    // console.error('DNS listener active, waiting for cleanup signal...');
    keepAliveTimer = setInterval(() => {}, 1000);
    await new Promise(() => {}); // Keep alive until SIGTERM
    return;
  } catch (e) {
    const error = `${e.name}: ${e.message}`;
    console.error(`ERROR: ${error}`);

    emitArchiveResultRecord("failed", error);
    process.exit(1);
  }
}

main().catch((e) => {
  console.error(`Fatal error: ${e.message}`);
  process.exit(1);
});
