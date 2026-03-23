#!/usr/bin/env node
/**
 * Extract SSL/TLS certificate details from a URL.
 *
 * This hook sets up CDP listeners BEFORE chrome_navigate loads the page,
 * then waits for navigation to complete. The listener captures SSL details
 * during the navigation request.
 *
 * Usage: on_Snapshot__23_sslcerts.daemon.bg.js --url=<url>
 * Output: Writes sslcerts.jsonl
 */

const fs = require("fs");
const path = require("path");
const tls = require("tls");
const crypto = require("crypto");

// Import generic helpers from base/utils.js
const {
  ensureNodeModuleResolution,
  getEnvBool,
  getEnvInt,
  loadConfig,
  parseArgs,
  emitArchiveResultRecord,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);
const puppeteer = require("puppeteer-core");

// Import chrome-specific utilities from chrome_utils.js
const {
  connectToPage,
  waitForNavigationComplete,
} = require("../chrome/chrome_utils.js");

const PLUGIN_NAME = "sslcerts";
const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || ".").trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const OUTPUT_FILE = "sslcerts.jsonl";
const CHROME_SESSION_DIR = "../chrome";

let browser = null;
let page = null;
let sslCaptured = false;
let shuttingDown = false;
let sslIssuer = null;
const seenCertificates = new Set();
let certCount = 0;

function readSecurityDetail(details, key) {
  const value = details?.[key];
  return typeof value === "function" ? value.call(details) : value;
}

function truncateIssuerName(value, maxLen = 40) {
  const text = String(value || "").trim();
  if (!text) return "unknown issuer";
  if (text.length <= maxLen) return text;
  return `${text.slice(0, maxLen - 3)}...`;
}

function normalizeFingerprint(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  return text.replace(/:/g, "").toLowerCase();
}

function sha256Hex(buffer) {
  return crypto.createHash("sha256").update(buffer).digest("hex");
}

function derToPem(rawBuffer) {
  const body = rawBuffer.toString("base64").match(/.{1,64}/g)?.join("\n") || "";
  return `-----BEGIN CERTIFICATE-----\n${body}\n-----END CERTIFICATE-----\n`;
}

function formatDn(value) {
  if (!value || typeof value !== "object") return "";
  return Object.entries(value)
    .filter(([, part]) => part !== undefined && part !== null && String(part).trim())
    .map(([key, part]) => `${key}=${String(part).trim()}`)
    .join(", ");
}

function tlsDateToEpochSeconds(value) {
  const timestamp = Date.parse(String(value || ""));
  return Number.isFinite(timestamp) ? Math.floor(timestamp / 1000) : null;
}

function buildCtSearchUrl(fingerprint256) {
  const normalized = normalizeFingerprint(fingerprint256);
  return normalized ? `https://crt.sh/?q=${encodeURIComponent(normalized)}` : null;
}

function getPositionLabel(index, total) {
  if (index === 0) return "leaf";
  if (index === total - 1) return "root";
  return "intermediate";
}

function writePemFile(filename, pemText) {
  const outputPath = path.join(OUTPUT_DIR, filename);
  fs.writeFileSync(outputPath, pemText);
  return `${PLUGIN_DIR}/${filename}`;
}

async function fetchCertificateChain(originUrl, timeoutMs) {
  const origin = new URL(originUrl);
  const host = origin.hostname;
  const port = origin.port ? Number(origin.port) : 443;

  return await new Promise((resolve, reject) => {
    const socket = tls.connect({
      host,
      port,
      servername: host,
      rejectUnauthorized: false,
    });

    const cleanup = () => {
      socket.removeAllListeners("secureConnect");
      socket.removeAllListeners("error");
      socket.removeAllListeners("timeout");
    };

    socket.setTimeout(timeoutMs);

    socket.once("secureConnect", () => {
      try {
        let current = socket.getPeerCertificate(true);
        const chain = [];
        const seenFingerprints = new Set();

        while (current && current.raw && current.raw.length > 0) {
          const fingerprint256 =
            normalizeFingerprint(current.fingerprint256) || sha256Hex(current.raw);
          if (seenFingerprints.has(fingerprint256)) break;
          seenFingerprints.add(fingerprint256);

          chain.push({
            raw: current.raw,
            subject: current.subject || null,
            issuer: current.issuer || null,
            subjectText: formatDn(current.subject),
            issuerText: formatDn(current.issuer),
            commonName: current.subject?.CN || "",
            issuerCommonName: current.issuer?.CN || "",
            subjectAltName: current.subjectaltname || "",
            serialNumber: current.serialNumber || "",
            validFrom: tlsDateToEpochSeconds(current.valid_from),
            validTo: tlsDateToEpochSeconds(current.valid_to),
            fingerprint256,
            fingerprint512: normalizeFingerprint(current.fingerprint512),
            ctSearchUrl: buildCtSearchUrl(fingerprint256),
          });

          if (!current.issuerCertificate || current.issuerCertificate === current) {
            break;
          }
          current = current.issuerCertificate;
        }

        cleanup();
        socket.end();
        resolve(chain);
      } catch (error) {
        cleanup();
        socket.destroy();
        reject(error);
      }
    });

    socket.once("timeout", () => {
      cleanup();
      socket.destroy(new Error(`Timed out fetching certificate chain for ${origin.origin}`));
    });

    socket.once("error", (error) => {
      cleanup();
      socket.destroy();
      reject(error);
    });
  });
}

async function setupListener(url) {
  const outputPath = path.join(OUTPUT_DIR, OUTPUT_FILE);
  const timeout = getEnvInt("SSLCERTS_TIMEOUT", 30) * 1000;
  let targetHost = null;

  fs.writeFileSync(outputPath, "");

  // Only extract SSL for HTTPS URLs
  if (!url.startsWith("https://")) {
    throw new Error("URL is not HTTPS");
  }

  try {
    targetHost = new URL(url).host;
  } catch (e) {
    targetHost = null;
  }

  // Connect to Chrome page using shared utility
  const { browser, page } = await connectToPage({
    chromeSessionDir: CHROME_SESSION_DIR,
    timeoutMs: timeout,
    puppeteer,
  });

  page.on("response", async (response) => {
    try {
      if (sslCaptured) return;
      const request = response.request();
      if (
        !request.isNavigationRequest() ||
        request.frame() !== page.mainFrame()
      ) {
        return;
      }

      const responseUrl = response.url() || "";
      if (!responseUrl.startsWith("http")) return;

      if (targetHost) {
        try {
          const responseHost = new URL(responseUrl).host;
          if (responseHost !== targetHost) return;
        } catch (e) {
          // Ignore URL parse errors, fall through
        }
      }

      const securityDetails = response.securityDetails?.() || null;
      let sslInfo = { url: responseUrl };

      if (securityDetails) {
        sslCaptured = true;
        const protocol = readSecurityDetail(securityDetails, "protocol") || "";
        const keyExchange =
          readSecurityDetail(securityDetails, "keyExchange") || "";
        const keyExchangeGroup =
          readSecurityDetail(securityDetails, "keyExchangeGroup") || "";
        const cipher = readSecurityDetail(securityDetails, "cipher") || "";
        const mac = readSecurityDetail(securityDetails, "mac") || "";
        const subjectName =
          readSecurityDetail(securityDetails, "subjectName") || "";
        const issuer = readSecurityDetail(securityDetails, "issuer") || "";
        const validFrom =
          readSecurityDetail(securityDetails, "validFrom") || "";
        const validTo = readSecurityDetail(securityDetails, "validTo") || "";
        const sanList =
          readSecurityDetail(securityDetails, "subjectAlternativeNames") ||
          readSecurityDetail(securityDetails, "sanList") ||
          [];
        const certificateId =
          readSecurityDetail(securityDetails, "certificateId") || null;
        const signedCertificateTimestampList =
          readSecurityDetail(securityDetails, "signedCertificateTimestampList") ||
          [];
        const certificateTransparencyCompliance =
          readSecurityDetail(
            securityDetails,
            "certificateTransparencyCompliance",
          ) || "";
        const serverSignatureAlgorithm =
          readSecurityDetail(securityDetails, "serverSignatureAlgorithm") || null;
        const encryptedClientHello =
          readSecurityDetail(securityDetails, "encryptedClientHello");
        const certKey = JSON.stringify([
          responseHostFromUrl(responseUrl),
          protocol,
          subjectName,
          issuer,
          validFrom,
          validTo,
          ...sanList,
        ]);
        if (seenCertificates.has(certKey)) {
          return;
        }
        seenCertificates.add(certKey);
        certCount += 1;
        sslInfo.protocol = protocol;
        if (keyExchange) sslInfo.keyExchange = keyExchange;
        if (keyExchangeGroup) sslInfo.keyExchangeGroup = keyExchangeGroup;
        if (cipher) sslInfo.cipher = cipher;
        if (mac) sslInfo.mac = mac;
        sslInfo.subjectName = subjectName;
        sslInfo.issuer = issuer;
        sslIssuer = issuer || subjectName || null;
        sslInfo.validFrom = validFrom;
        sslInfo.validTo = validTo;
        sslInfo.certificateId = certificateId;
        sslInfo.securityState = "secure";
        sslInfo.schemeIsCryptographic = true;
        if (sanList && sanList.length > 0) {
          sslInfo.subjectAlternativeNames = sanList;
        }
        if (signedCertificateTimestampList.length > 0) {
          sslInfo.signedCertificateTimestampList = signedCertificateTimestampList;
        }
        if (certificateTransparencyCompliance) {
          sslInfo.certificateTransparencyCompliance =
            certificateTransparencyCompliance;
        }
        if (serverSignatureAlgorithm !== null) {
          sslInfo.serverSignatureAlgorithm = serverSignatureAlgorithm;
        }
        if (typeof encryptedClientHello === "boolean") {
          sslInfo.encryptedClientHello = encryptedClientHello;
        }

        try {
          const chain = await fetchCertificateChain(responseUrl, timeout);
          if (chain.length > 0) {
            const chainPem = chain.map((cert) => derToPem(cert.raw)).join("");
            const leafPemPath = writePemFile("leaf.pem", derToPem(chain[0].raw));
            const rootPemPath = writePemFile(
              "root.pem",
              derToPem(chain[chain.length - 1].raw),
            );
            const chainPemPath = writePemFile("chain.pem", chainPem);

            sslInfo.leafPemPath = leafPemPath;
            sslInfo.rootPemPath = rootPemPath;
            sslInfo.chainPemPath = chainPemPath;
            sslInfo.leafFingerprint256 = chain[0].fingerprint256;
            sslInfo.leafCtSearchUrl = chain[0].ctSearchUrl;
            sslInfo.certificateChain = chain.map((cert, index) => ({
              position: getPositionLabel(index, chain.length),
              subject: cert.subject,
              issuer: cert.issuer,
              subjectText: cert.subjectText,
              issuerText: cert.issuerText,
              commonName: cert.commonName,
              issuerCommonName: cert.issuerCommonName,
              subjectAltName: cert.subjectAltName,
              serialNumber: cert.serialNumber,
              validFrom: cert.validFrom,
              validTo: cert.validTo,
              fingerprint256: cert.fingerprint256,
              fingerprint512: cert.fingerprint512 || null,
              pemPath:
                index === 0
                  ? leafPemPath
                  : index === chain.length - 1
                    ? rootPemPath
                    : null,
              ctSearchUrl: cert.ctSearchUrl,
            }));
          }
        } catch (error) {
          sslInfo.chainError = `${error.name}: ${error.message}`;
        }
      } else if (responseUrl.startsWith("https://")) {
        sslCaptured = true;
        sslInfo.securityState = "unknown";
        sslInfo.schemeIsCryptographic = true;
        sslInfo.error = "No security details available";
      } else {
        sslCaptured = true;
        sslInfo.securityState = "insecure";
        sslInfo.schemeIsCryptographic = false;
      }

      fs.appendFileSync(outputPath, JSON.stringify(sslInfo) + "\n");
    } catch (e) {
      // Ignore errors
    }
  });

  return { browser, page };
}

function emitResult(
  status = "succeeded",
  outputStr = truncateIssuerName(sslIssuer),
) {
  if (shuttingDown) return Promise.resolve();
  shuttingDown = true;
  emitArchiveResultRecord(status, outputStr);
  return Promise.resolve();
}

function responseHostFromUrl(url) {
  try {
    return new URL(url).host;
  } catch (e) {
    return url || "";
  }
}

async function handleShutdown(signal) {
  console.error(`\nReceived ${signal}, emitting final results...`);
  await emitResult("succeeded");
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
    console.error(
      "Usage: on_Snapshot__23_sslcerts.daemon.bg.js --url=<url>",
    );
    process.exit(1);
  }

  if (!getEnvBool("SSLCERTS_ENABLED", true)) {
    console.error("Skipping (SSLCERTS_ENABLED=False)");
    emitArchiveResultRecord("skipped", "SSLCERTS_ENABLED=False");
    process.exit(0);
  }

  try {
    // Set up listener BEFORE navigation
    const connection = await setupListener(url);
    browser = connection.browser;
    page = connection.page;

    // Register signal handlers for graceful shutdown
    process.on("SIGTERM", () => handleShutdown("SIGTERM"));
    process.on("SIGINT", () => handleShutdown("SIGINT"));

    // Wait for chrome_navigate to complete (non-fatal)
    try {
      const timeout = getEnvInt("SSLCERTS_TIMEOUT", 30) * 1000;
      await waitForNavigationComplete(CHROME_SESSION_DIR, timeout * 4);
    } catch (e) {
      console.error(`WARN: ${e.message}`);
    }

    // console.error('SSL listener active, waiting for cleanup signal...');
    await new Promise(() => {}); // Keep alive until SIGTERM
    return;
  } catch (e) {
    const error = `${e.name}: ${e.message}`;
    console.error(`ERROR: ${error}`);

    await emitResult("failed", error);
    process.exit(1);
  }
}

main().catch(async (e) => {
  console.error(`Fatal error: ${e.message}`);
  await emitResult("failed", `${e.name}: ${e.message}`);
  process.exit(1);
});
