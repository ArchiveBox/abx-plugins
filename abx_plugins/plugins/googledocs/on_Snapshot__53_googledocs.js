#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
const fs = require("fs");
const path = require("path");
const {
  loadConfig,
  getEnvBool,
  getEnvInt,
  getEnvArray,
  parseArgs,
  emitArchiveResultRecord,
  writeFileAtomic,
} = require("../base/utils.js");
const {
  connectToPage,
  downloadBrowserResource,
} = require("../chrome/chrome_utils.js");
const {
  FORMATS,
  parseDocumentUrl,
  exportUrl,
  normalizeUrl,
  findSavedResponses,
  fileHash,
  validateExport,
} = require("./googledocs_utils.js");

async function main() {
  const config = loadConfig();
  const { url } = parseArgs();
  if (!url) throw new Error("Missing --url");
  if (!getEnvBool("GOOGLEDOCS_ENABLED", true))
    return emitArchiveResultRecord("skipped", "GOOGLEDOCS_ENABLED=False");
  const source = new URL(url);
  const originalDoc = parseDocumentUrl(url);
  const driveLink =
    source.protocol === "https:" &&
    source.host === "drive.google.com" &&
    (/^\/file\/d\/[\w-]+/.test(source.pathname) || source.pathname === "/open");
  if (!originalDoc && !driveLink)
    return emitArchiveResultRecord(
      "noresults",
      "Not a supported Google document URL"
    );
  const snapshotDir = path.resolve(config.SNAP_DIR || ".");
  const timeoutMs = getEnvInt("GOOGLEDOCS_TIMEOUT", 120) * 1000;
  const deadline = Date.now() + timeoutMs;
  const { browser, page, cdpSession } = await connectToPage({
    chromeSessionDir: path.join(snapshotDir, "chrome"),
    timeoutMs,
    waitForNavigationComplete: true,
  });
  try {
    const currentDoc = parseDocumentUrl(page.url());
    if (
      originalDoc &&
      currentDoc &&
      (originalDoc.id !== currentDoc.id || originalDoc.kind !== currentDoc.kind)
    )
      throw new Error("Chrome tab navigated to a different Google document");
    const doc = currentDoc || originalDoc;
    if (!doc)
      return emitArchiveResultRecord(
        "noresults",
        "Drive URL did not resolve to Docs, Sheets, Slides or Drawings"
      );
    // Preserve link access and selected-sheet context through Google's redirects.
    for (const key of ["resourcekey", "authuser", "gid"])
      doc[key] = originalDoc?.[key] || source.searchParams.get(key) || doc[key];
    const requested = [
      ...new Set(getEnvArray("GOOGLEDOCS_FORMATS", config.GOOGLEDOCS_FORMATS)),
    ];
    const unknown = requested.filter(
      (format) => !Object.values(FORMATS).flat().includes(format)
    );
    if (unknown.length)
      throw new Error(`Unknown GOOGLEDOCS_FORMATS: ${unknown.join(", ")}`);
    const formats = requested.filter((format) =>
      FORMATS[doc.kind].includes(format)
    );
    if (!formats.length)
      return emitArchiveResultRecord(
        "skipped",
        "GOOGLEDOCS_FORMATS has no formats for this document type"
      );
    const outputDir = path.join(snapshotDir, "googledocs");
    fs.mkdirSync(outputDir, { recursive: true });
    const saved = await findSavedResponses(
      snapshotDir,
      formats.map((format) => exportUrl(doc, format))
    );
    const manifest = {
      title: await page.title(),
      document_type: doc.kind,
      document_id: doc.id,
      exports: [],
      errors: [],
    };
    for (const format of formats) {
      const exportURL = exportUrl(doc, format);
      const filename = `document.${format}`;
      const temporary = path.join(outputDir, `.${filename}.${process.pid}.tmp`);
      try {
        const cached = saved.get(normalizeUrl(exportURL));
        let mimeType;
        let reused = false;
        if (cached) {
          try {
            validateExport(cached.file, format, cached.mimeType);
            if ((await fileHash(cached.file)) === cached.responseSha256) {
              fs.copyFileSync(cached.file, temporary);
              mimeType = cached.mimeType;
              reused = true;
            }
          } catch {
            /* Unusable optional captures must not prevent export. */
          }
        }
        if (!reused) {
          const response = await downloadBrowserResource({
            cdpSession,
            url: exportURL,
            outputPath: temporary,
            timeoutMs: deadline - Date.now(),
          });
          mimeType =
            Object.entries(response.headers).find(
              ([key]) => key.toLowerCase() === "content-type"
            )?.[1] || "";
        }
        validateExport(temporary, format, mimeType);
        const sha256 = await fileHash(temporary);
        const size = fs.statSync(temporary).size;
        fs.renameSync(temporary, path.join(outputDir, filename));
        manifest.exports.push({
          format,
          path: filename,
          size,
          sha256,
          reused_response: reused,
        });
        console.error(
          `${format.toUpperCase()}: ${size} bytes${
            reused ? " (reused captured response)" : ""
          }`
        );
      } catch (error) {
        manifest.errors.push({ format, error: error.message });
        console.error(`${format.toUpperCase()}: ${error.message}`);
      } finally {
        fs.rmSync(temporary, { force: true });
      }
    }
    writeFileAtomic(
      path.join(outputDir, "exports.json"),
      JSON.stringify(manifest, null, 2) + "\n"
    );
    if (manifest.errors.length) {
      emitArchiveResultRecord(
        "failed",
        `${manifest.exports.length}/${
          formats.length
        } exports saved; ${manifest.errors
          .map((item) => `${item.format}: ${item.error}`)
          .join("; ")}`
      );
      process.exitCode = 1;
    } else emitArchiveResultRecord("succeeded", "googledocs/exports.json");
  } finally {
    await browser.disconnect();
  }
}

main().catch((error) => {
  console.error(error.message);
  emitArchiveResultRecord("failed", error.message);
  process.exitCode = 1;
});
