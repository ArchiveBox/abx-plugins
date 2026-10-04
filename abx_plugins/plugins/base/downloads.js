const path = require("path");
const { spawn } = require("child_process");

// Reuse the Python runtime already supplied by abxpkg, with stdlib ZIP64/CRC
// support and streaming extraction. No additional package or ZIP parser needed.
function saveDownloads(
  outputDir,
  title,
  downloads,
  { requireZip = false, timeoutMs = 120000 } = {}
) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      path.join(__dirname, "unpack_downloads.py"),
      [outputDir],
      {
        timeout: timeoutMs,
        killSignal: "SIGKILL",
        stdio: ["pipe", "pipe", "pipe"],
      }
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
