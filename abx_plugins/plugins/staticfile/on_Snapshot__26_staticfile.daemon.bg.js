#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Detect static-file main responses using CDP during initial request.
 *
 * This hook sets up CDP listeners BEFORE chrome_navigate to capture the
 * Content-Type from the initial response. If it's a static file (PDF, image, etc.),
 * it prefers the artifact saved by the responses hook and only falls back to
 * saving its own copy when that artifact is unavailable.
 *
 * Usage: on_Snapshot__26_staticfile.daemon.bg.js --url=<url>
 * Output: Emits the saved main-response path
 */


const installShutdownHandler = require("../base/daemon_lifecycle.js").captureShutdownSignals();

const fs = require("fs");
const path = require("path");
const {
  getExtensionFromMimeType,
  getExtensionFromUrl,
} = require("../responses/filename_utils.js");

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

// Import chrome-specific utilities from chrome_utils.js
const {
  connectToPage,
  getBrowserConnection,
  resolvePuppeteerModule,
  resolveChromeLaunchOptions,
  sendBrowserCommand,
} = require("../chrome/chrome_utils.js");
const puppeteer = resolvePuppeteerModule();

const PLUGIN_NAME = "staticfile";
const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || ".").trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
const RESPONSES_INDEX_PATH = path.join(SNAP_DIR, "responses", "index.jsonl");
const PRENAV_MARKER_PATH = path.join(OUTPUT_DIR, "prenav.json");
if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);
const CHROME_SESSION_DIR = "../chrome";

// Content-Types that indicate static files
const STATIC_CONTENT_TYPES = new Set([
  // Documents
  "application/pdf",
  "application/msword",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.ms-excel",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.ms-powerpoint",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "application/rtf",
  "application/epub+zip",
  // Images
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/svg+xml",
  "image/x-icon",
  "image/bmp",
  "image/tiff",
  "image/avif",
  "image/heic",
  "image/heif",
  // Audio
  "audio/mpeg",
  "audio/mp3",
  "audio/wav",
  "audio/flac",
  "audio/aac",
  "audio/ogg",
  "audio/webm",
  "audio/m4a",
  "audio/opus",
  // Video
  "video/mp4",
  "video/webm",
  "video/x-matroska",
  "video/avi",
  "video/quicktime",
  "video/x-ms-wmv",
  "video/x-flv",
  // Archives
  "application/zip",
  "application/x-tar",
  "application/gzip",
  "application/x-bzip2",
  "application/x-xz",
  "application/x-7z-compressed",
  "application/x-rar-compressed",
  "application/vnd.rar",
  // Data
  "application/json",
  "application/xml",
  "text/csv",
  "text/xml",
  "application/x-yaml",
  // Executables/Binaries
  "application/octet-stream",
  "application/x-executable",
  "application/x-msdos-program",
  "application/x-apple-diskimage",
  "application/vnd.debian.binary-package",
  "application/x-rpm",
  // Other
  "application/x-bittorrent",
  "application/wasm",
]);

const STATIC_CONTENT_TYPE_PREFIXES = [
  "image/",
  "audio/",
  "video/",
  "application/zip",
  "application/x-",
];

// Global state
let originalUrl = "";
let detectedContentType = null;
let isStaticFile = false;
let savedOutputPath = null;
let downloadError = null;
let page = null;
let browser = null;
let finalized = false;

function isStaticContentType(contentType) {
  if (!contentType) return false;

  const ct = contentType.split(";")[0].trim().toLowerCase();

  // Check exact match
  if (STATIC_CONTENT_TYPES.has(ct)) return true;

  // Check prefixes
  for (const prefix of STATIC_CONTENT_TYPE_PREFIXES) {
    if (ct.startsWith(prefix)) return true;
  }

  return false;
}

function sanitizeFilename(str, maxLen = 200) {
  return str.replace(/[^a-zA-Z0-9._-]/g, "_").slice(0, maxLen);
}

function getFilenameFromUrl(url) {
  try {
    const pathname = new URL(url).pathname;
    const filename = path.basename(pathname) || "downloaded_file";
    return sanitizeFilename(filename);
  } catch (e) {
    return "downloaded_file";
  }
}

function normalizeUrl(url) {
  try {
    const parsed = new URL(url);
    let path = parsed.pathname || "";
    if (path === "/") path = "";
    return `${parsed.origin}${path}`;
  } catch (e) {
    return url;
  }
}

function isTopLevelNavigationRequest(request) {
  try {
    if (!request || request.isNavigationRequest?.() !== true) return false;
    const url = request.url?.() || "";
    if (!url.startsWith("http")) return false;
    const frame = request.frame?.() || null;
    return (
      !frame || frame.parentFrame?.() === null || frame === page?.mainFrame?.()
    );
  } catch (error) {
    return false;
  }
}

function getOutputPathRelativeToSnapshot(filePath) {
  if (!filePath) return null;
  return path.posix.join(
    PLUGIN_DIR,
    String(filePath).split(path.sep).join("/")
  );
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function writePrenavMarker(status) {
  fs.writeFileSync(
    PRENAV_MARKER_PATH,
    JSON.stringify(
      {
        status,
        timestamp: new Date().toISOString(),
      },
      null,
      2
    )
  );
}

function removePrenavMarker() {
  try {
    fs.unlinkSync(PRENAV_MARKER_PATH);
  } catch (error) {}
}

function getResponsesOutputInfo(url, mimeType) {
  try {
    const urlObj = new URL(url);
    const hostname = urlObj.hostname;
    const pathname = urlObj.pathname || "/";
    const extension =
      getExtensionFromMimeType(mimeType) || getExtensionFromUrl(url);
    const filename =
      path.basename(pathname) || `index${extension ? `.${extension}` : ""}`;
    const dirPathRaw = path.dirname(pathname);
    const dirPath = dirPathRaw === "." ? "" : dirPathRaw.replace(/^\/+/, "");
    const relativePath = path.posix.join(
      "responses",
      hostname,
      dirPath.split(path.sep).join("/"),
      filename
    );
    return {
      relativePath,
      absolutePath: path.join(SNAP_DIR, ...relativePath.split("/")),
    };
  } catch (error) {
    return null;
  }
}

function isSavedResponseReady(filePath) {
  if (!filePath || !fs.existsSync(filePath)) return false;

  try {
    const stats = fs.statSync(filePath);
    return stats.isFile() && stats.size > 0;
  } catch (error) {
    return false;
  }
}

async function waitForResponsesOutput(outputInfo, timeoutMs) {
  if (!outputInfo) return null;

  const startupDeadline = Date.now() + Math.min(timeoutMs, 2000);
  while (Date.now() < startupDeadline) {
    if (fs.existsSync(RESPONSES_INDEX_PATH)) break;
    await sleep(100);
  }

  if (!fs.existsSync(RESPONSES_INDEX_PATH)) {
    return null;
  }

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (isSavedResponseReady(outputInfo.absolutePath)) {
      return outputInfo.relativePath;
    }
    await sleep(100);
  }

  if (isSavedResponseReady(outputInfo.absolutePath)) {
    return outputInfo.relativePath;
  }

  return null;
}

function buildArchiveResult() {
  const outputMimeType = detectedContentType || "unknown";

  if (!detectedContentType) {
    return {
      type: "ArchiveResult",
      status: "failed",
      output_str: "No main response captured",
      plugin: PLUGIN_NAME,
    };
  }

  if (!isStaticFile) {
    return {
      type: "ArchiveResult",
      status: "noresults",
      output_str: detectedContentType.startsWith("text/html")
        ? "Page is HTML (not staticfile)"
        : outputMimeType,
      plugin: PLUGIN_NAME,
      content_type: detectedContentType,
    };
  }

  if (downloadError) {
    return {
      type: "ArchiveResult",
      status: "failed",
      output_str: downloadError,
      plugin: PLUGIN_NAME,
      content_type: detectedContentType,
    };
  }

  if (savedOutputPath) {
    return {
      type: "ArchiveResult",
      status: "succeeded",
      output_str: savedOutputPath,
      plugin: PLUGIN_NAME,
      content_type: detectedContentType,
    };
  }

  return {
    type: "ArchiveResult",
    status: "failed",
    output_str: outputMimeType,
    plugin: PLUGIN_NAME,
    content_type: detectedContentType,
  };
}

async function setupStaticFileListener() {
  const timeout = getEnvInt("STATICFILE_TIMEOUT", 30) * 1000;

  // Connect to Chrome page using shared utility
  const connection = await connectToPage({
    chromeSessionDir: CHROME_SESSION_DIR,
    timeoutMs: timeout,
    puppeteer,
  });
  browser = connection.browser;
  page = connection.page;

  // Chrome handles Content-Disposition attachments as downloads, so their
  // response bodies are unavailable to response.buffer(). Keep the existing
  // persona-wide download directory shared with ArchiveWeb.page exports; only
  // claim a completed download from this snapshot's main frame.
  const downloadDir = path.resolve(
    resolveChromeLaunchOptions(hookConfig).CHROME_DOWNLOADS_DIR
  );
  fs.mkdirSync(downloadDir, { recursive: true });
  const pageSession = await page.target().createCDPSession();
  const frameTree = await pageSession.send("Page.getFrameTree");
  const mainFrameId = frameTree.frameTree.frame.id;
  await pageSession.detach();
  const browserConnection = getBrowserConnection(browser);
  let downloadGuid = null;
  let downloadFilename = null;
  let downloadTerminal = null;
  let resolveDownload;
  const browserDownload = new Promise((resolve) => {
    resolveDownload = resolve;
  });
  const onDownloadWillBegin = (event) => {
    if (event.frameId !== mainFrameId || downloadGuid) return;
    downloadGuid = event.guid;
    downloadFilename = event.suggestedFilename;
  };
  const onDownloadProgress = (event) => {
    if (event.guid !== downloadGuid) return;
    if (["completed", "canceled", "interrupted"].includes(event.state)) {
      downloadTerminal = { event, filename: downloadFilename };
      resolveDownload(downloadTerminal);
    }
  };
  browserConnection.on("Browser.downloadWillBegin", onDownloadWillBegin);
  browserConnection.on("Browser.downloadProgress", onDownloadProgress);
  await sendBrowserCommand(browser, "Browser.setDownloadBehavior", {
    behavior: "allow",
    downloadPath: downloadDir,
    eventsEnabled: true,
  });

  async function waitForDownloadUntil(deadline) {
    if (downloadTerminal) return downloadTerminal;
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new Error("Browser download did not complete");
    let timer;
    try {
      return await Promise.race([
        browserDownload,
        new Promise((_, reject) => {
          timer = setTimeout(
            () => reject(new Error("Browser download did not complete")),
            remaining
          );
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  }

  async function saveBrowserDownload(download, deadline) {
    const { event, filename } = download;
    if (event.state !== "completed" || !event.filePath) {
      throw new Error(`Browser download ${event.state} or missing file path`);
    }
    const sourcePath = path.resolve(event.filePath);
    const relativeSource = path.relative(downloadDir, sourcePath);
    if (relativeSource.startsWith("..") || path.isAbsolute(relativeSource)) {
      throw new Error("Browser download escaped shared directory");
    }
    const maxSize = getEnvInt("STATICFILE_MAX_SIZE", 1024 * 1024 * 1024);
    do {
      const size = fs.existsSync(sourcePath) ? fs.statSync(sourcePath).size : 0;
      if (size === event.receivedBytes && size > 0) {
        if (size > maxSize) throw new Error(`File too large: ${size} bytes`);
        const outputName = sanitizeFilename(filename || path.basename(sourcePath));
        fs.copyFileSync(sourcePath, path.join(OUTPUT_DIR, outputName));
        return getOutputPathRelativeToSnapshot(outputName);
      }
      await sleep(50);
    } while (Date.now() < deadline);
    throw new Error("Completed browser download was not published");
  }

  let resolveMainResponse;
  let rejectMainResponse;
  const mainResponseHandled = new Promise((resolve, reject) => {
    resolveMainResponse = resolve;
    rejectMainResponse = reject;
  });

  const failTimer = setTimeout(() => {
    rejectMainResponse(
      new Error(
        `Timed out waiting for main response after ${
          (timeout * 4) / 1000
        } seconds`
      )
    );
  }, timeout * 4);

  const finish = () => {
    clearTimeout(failTimer);
    browserConnection.off("Browser.downloadWillBegin", onDownloadWillBegin);
    browserConnection.off("Browser.downloadProgress", onDownloadProgress);
    resolveMainResponse(buildArchiveResult());
  };

  let firstResponseHandled = false;

  page.on("response", async (response) => {
    if (firstResponseHandled) return;

    try {
      const request = response.request();
      const url = response.url();
      const headers = response.headers();
      const contentType = headers["content-type"] || "";
      const status = response.status();

      // Only process the main document response
      if (status < 200 || status >= 300) return;
      if (!isTopLevelNavigationRequest(request)) return;

      firstResponseHandled = true;
      detectedContentType = contentType.split(";")[0].trim();

      console.error(`Detected Content-Type: ${detectedContentType}`);

      // Check if it's a static file
      if (!isStaticContentType(detectedContentType)) {
        console.error("Not a static file, skipping download");
        finish();
        return;
      }

      isStaticFile = true;
      console.error("Static file detected, waiting for saved output...");

      const responsesEnabled = getEnvBool("RESPONSES_ENABLED", true);
      const isAttachment = /\battachment\b/i.test(
        headers["content-disposition"] || ""
      );
      const deadline = Date.now() + timeout;
      if (isAttachment) {
        savedOutputPath = await saveBrowserDownload(
          await waitForDownloadUntil(deadline),
          deadline
        );
        finish();
        return;
      }
      if (responsesEnabled) {
        const responsesOutputInfo = getResponsesOutputInfo(url, detectedContentType);
        const responsesOutputPath = await waitForResponsesOutput(
          responsesOutputInfo,
          timeout
        );
        if (responsesOutputPath) {
          savedOutputPath = responsesOutputPath;
          finish();
          return;
        }
      }

      console.error("Saving static file fallback locally...");

      // Download the file
      const maxSize = getEnvInt("STATICFILE_MAX_SIZE", 1024 * 1024 * 1024); // 1GB default
      let buffer;
      try {
        buffer = await response.buffer();
      } catch (bodyError) {
        try {
          savedOutputPath = await saveBrowserDownload(
            await waitForDownloadUntil(deadline),
            deadline
          );
          finish();
          return;
        } catch (browserError) {
          throw new Error(
            `Response body unavailable: ${bodyError.message}; ${browserError.message}`
          );
        }
      }

      if (buffer.length > maxSize) {
        downloadError = `File too large: ${buffer.length} bytes > ${maxSize} max`;
        finish();
        return;
      }

      // Determine filename
      let filename = getFilenameFromUrl(url);

      // Check content-disposition header for better filename
      const contentDisp = headers["content-disposition"] || "";
      if (contentDisp.includes("filename=")) {
        const match = contentDisp.match(/filename[*]?=["']?([^"';\n]+)/);
        if (match) {
          filename = sanitizeFilename(match[1].trim());
        }
      }

      const outputPath = path.join(OUTPUT_DIR, filename);
      fs.writeFileSync(outputPath, buffer);

      savedOutputPath = getOutputPathRelativeToSnapshot(filename);
      console.error(
        `Static file downloaded (${buffer.length} bytes): ${filename}`
      );
      finish();
    } catch (e) {
      downloadError = `${e.name}: ${e.message}`;
      console.error(`Error downloading static file: ${downloadError}`);
      firstResponseHandled = true;
      finish();
    }
  });

  page.on("requestfailed", (request) => {
    if (firstResponseHandled) return;
    try {
      if (!isTopLevelNavigationRequest(request)) return;
      firstResponseHandled = true;
      const failure = request.failure();
      downloadError = failure ? failure.errorText : "Request failed";
      rejectMainResponse(new Error(downloadError));
    } catch (e) {
      rejectMainResponse(e);
    }
  });

  writePrenavMarker("ready");
  return { browser, page, mainResponseHandled };
}

function emitResult(result) {
  emitArchiveResultRecord(result.status, result.output_str, {
    plugin: result.plugin,
    content_type: result.content_type,
  });
  return Promise.resolve();
}

async function handleShutdown(signal) {
  console.error(`\nReceived ${signal}, emitting final results...`);
  if (finalized) {
    process.exit(0);
  }
  finalized = true;
  removePrenavMarker();
  await emitResult(buildArchiveResult());
  process.exit(0);
}

async function main() {
  const args = parseArgs();
  const url = args.url;

  if (!url) {
    console.error("Usage: on_Snapshot__26_staticfile.daemon.bg.js --url=<url>");
    process.exit(1);
  }

  originalUrl = url;
  writePrenavMarker("starting");

  if (!getEnvBool("STATICFILE_ENABLED", true)) {
    console.error("Skipping (STATICFILE_ENABLED=False)");
    writePrenavMarker("skipped");
    emitArchiveResultRecord("skipped", "STATICFILE_ENABLED=False");
    process.exit(0);
  }

  const timeout = getEnvInt("STATICFILE_TIMEOUT", 30) * 1000;

  // Register signal handlers for graceful shutdown
  installShutdownHandler(handleShutdown);

  try {
    // Set up static file listener BEFORE navigation and finish on the
    // first successful main-document response.
    const connection = await setupStaticFileListener();
    console.log("staticfile listener attached");
    console.error("waiting for initial response...");
    const result = await connection.mainResponseHandled;
    finalized = true;
    removePrenavMarker();
    await emitResult(result);
    if (browser) {
      try {
        browser.disconnect();
      } catch (e) {}
    }
    process.exit(result.status === "failed" ? 1 : 0);
  } catch (e) {
    const error = `${e.name}: ${e.message}`;
    console.error(`ERROR: ${error}`);
    removePrenavMarker();

    await emitResult({
      type: "ArchiveResult",
      status: "failed",
      output_str: error,
    });
    process.exit(1);
  }
}

main().catch(async (e) => {
  console.error(`Fatal error: ${e.message}`);
  removePrenavMarker();
  const error = `${e.name}: ${e.message}`;
  await emitResult({
    type: "ArchiveResult",
    status: "failed",
    output_str: error,
  });
  process.exit(1);
});
