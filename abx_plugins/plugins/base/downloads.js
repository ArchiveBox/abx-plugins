const path = require("path");
const { spawn } = require("child_process");

// Reuse the Python runtime already supplied by abxpkg, with stdlib ZIP64/CRC
// support and streaming extraction. No additional package or ZIP parser needed.
function saveDownloads(
  outputDir,
  title,
  downloads,
  { requireZip = false, timeoutMs = 120000 } = {},
) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      path.join(__dirname, "unpack_downloads.py"),
      [outputDir],
      {
        timeout: timeoutMs,
        killSignal: "SIGKILL",
        stdio: ["pipe", "pipe", "pipe"],
      },
    );
    let stdout = "",
      stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0)
        return reject(new Error(stderr.trim() || "Download extraction failed"));
      try {
        resolve(JSON.parse(stdout));
      } catch (error) {
        reject(error);
      }
    });
    child.stdin.on("error", reject);
    child.stdin.end(JSON.stringify({ title, downloads, requireZip }));
  });
}
module.exports = { saveDownloads };

// Build a file listing only when a browser task saved content, not just logs.
const fs = require("node:fs");
const escape = (value) =>
  value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
function writeDownloadsIndex(outputDir) {
  const files = [];
  function walk(directory, prefix = "") {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (entry.name === ".hooks" || entry.name === ".DS_Store") continue;
      const relative = prefix + entry.name;
      if (entry.isDirectory())
        walk(path.join(directory, entry.name), relative + "/");
      else if (entry.isFile() && !/\.(log|pid|sh)$/.test(entry.name))
        files.push(relative);
    }
  }
  walk(outputDir);
  if (!files.length) return null;
  if (!files.includes("index.html")) {
    const rows = files
      .sort()
      .map(
        (file) =>
          `<li><a href="${file.split("/").map(encodeURIComponent).join("/")}">${escape(file)}</a></li>`,
      )
      .join("\n");
    fs.writeFileSync(
      path.join(outputDir, "index.html"),
      `<!doctype html><html><head><meta charset="utf-8"><title>Saved files</title><style>body{font:14px/1.6 system-ui,sans-serif;margin:16px;color:#243443}ul{padding-left:20px}a{overflow-wrap:anywhere}</style></head><body><strong>Saved files</strong><ul>${rows}</ul></body></html>\n`,
    );
  }
  return "index.html";
}
module.exports.writeDownloadsIndex = writeDownloadsIndex;
if (require.main === module)
  console.log(writeDownloadsIndex(path.resolve(process.argv[2])) || "");
