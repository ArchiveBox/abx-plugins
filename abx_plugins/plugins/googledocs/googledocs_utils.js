const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const readline = require("readline");

const FORMATS = {
  document: ["docx", "pdf", "odt", "rtf", "txt", "md", "zip", "epub"],
  spreadsheets: ["xlsx", "csv", "pdf", "ods", "tsv", "zip"],
  presentation: ["pptx", "pdf", "odp", "txt"],
  drawings: ["svg", "pdf", "png", "jpg"],
};

function parseDocumentUrl(value) {
  try {
    const url = new URL(value);
    if (
      url.protocol !== "https:" ||
      url.host !== "docs.google.com" ||
      url.username ||
      url.password
    )
      return null;
    const match = url.pathname.match(
      /^\/(?:a\/[^/]+\/)?(document|spreadsheets|presentation|drawings)(\/u\/\d+)?\/d\/([\w-]+)(?:\/|$)/
    );
    if (!match || match[3] === "e") return null; // Published IDs are not file IDs.
    return {
      kind: match[1],
      id: match[3],
      base: `https://docs.google.com${match[0].replace(/\/$/, "")}`,
      authuser: url.searchParams.get("authuser") || match[2]?.split("/").pop(),
      resourcekey: url.searchParams.get("resourcekey"),
      gid:
        url.searchParams.get("gid") ||
        new URLSearchParams(url.hash.slice(1)).get("gid"),
    };
  } catch {
    return null;
  }
}

function exportUrl(doc, format) {
  if (!FORMATS[doc.kind]?.includes(format))
    throw new Error(`Unsupported export format: ${format}`);
  const url = new URL(`${doc.base}/export`);
  if (["presentation", "drawings"].includes(doc.kind))
    url.pathname += `/${format === "jpg" ? "jpeg" : format}`;
  else url.searchParams.set("format", format);
  for (const key of ["authuser", "resourcekey"]) {
    if (doc[key]) url.searchParams.set(key, doc[key]);
  }
  if (["csv", "tsv"].includes(format) && /^\d+$/.test(doc.gid || ""))
    url.searchParams.set("gid", doc.gid);
  return url.href;
}

function normalizeUrl(value) {
  const url = new URL(value);
  url.hash = "";
  url.searchParams.sort();
  return url.href;
}

// responses is optional. Ignore missing/malformed records, never trust a path
// outside its output directory, and never reuse a different export/revision.
async function findSavedResponses(snapshotDir, urls) {
  const found = new Map();
  const root = path.join(snapshotDir, "responses");
  const index = path.join(root, "index.jsonl");
  if (!fs.existsSync(index)) return found;
  const wanted = new Set(urls.map(normalizeUrl));
  const lines = readline.createInterface({
    input: fs.createReadStream(index),
    crlfDelay: Infinity,
  });
  for await (const line of lines) {
    try {
      const entry = JSON.parse(line);
      const requestUrl = normalizeUrl(entry.requestUrl || entry.url);
      if (
        entry.method !== "GET" ||
        (entry.requestMethod || entry.method) !== "GET" ||
        entry.status !== 200 ||
        !wanted.has(requestUrl)
      )
        continue;
      const file = fs.realpathSync(path.resolve(root, entry.path));
      if (!file.startsWith(`${fs.realpathSync(root)}${path.sep}`)) continue;
      found.set(requestUrl, { ...entry, file });
    } catch {
      /* An optional artifact may be incomplete while its hook runs. */
    }
  }
  return found;
}

async function fileHash(file) {
  const hash = crypto.createHash("sha256");
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
  return hash.digest("hex");
}

function validateExport(file, format, mimeType) {
  const fd = fs.openSync(file, "r");
  const header = Buffer.alloc(1024);
  let length;
  try {
    length = fs.readSync(fd, header);
  } finally {
    fs.closeSync(fd);
  }
  const bytes = header.subarray(0, length);
  const text = bytes.toString("utf8").trimStart();
  const mime = (mimeType || "").split(";")[0].trim().toLowerCase();
  if (
    !length ||
    /html|json/.test(mime) ||
    /^(?:<!doctype\s+html|<html\b)/i.test(text)
  )
    throw new Error("Export returned an empty file or login/error page");
  let valid = false;
  if (
    ["docx", "xlsx", "pptx", "odt", "ods", "odp", "zip", "epub"].includes(
      format
    )
  )
    valid = bytes.subarray(0, 4).equals(Buffer.from([80, 75, 3, 4]));
  else if (format === "pdf") valid = text.startsWith("%PDF-");
  else if (format === "png")
    valid = bytes
      .subarray(0, 8)
      .equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
  else if (format === "jpg")
    valid = bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255;
  else if (format === "svg")
    valid =
      mime === "image/svg+xml" && /^(?:<\?xml[^>]*>\s*)?<svg\b/.test(text);
  else if (format === "rtf") valid = text.startsWith("{\\rtf");
  else
    valid = [
      "text/plain",
      "text/csv",
      "text/tab-separated-values",
      "text/markdown",
      "text/x-markdown",
    ].includes(mime);
  if (!valid)
    throw new Error(
      `Invalid ${format.toUpperCase()} export (${
        mime || "missing Content-Type"
      })`
    );
}

module.exports = {
  FORMATS,
  parseDocumentUrl,
  exportUrl,
  normalizeUrl,
  findSavedResponses,
  fileHash,
  validateExport,
};
