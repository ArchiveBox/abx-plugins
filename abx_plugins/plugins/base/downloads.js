const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { writeFileAtomic } = require("./utils.js");

// Validate the ZIP boundary without decompressing or extracting untrusted paths.
function validateZip(filename) {
  const fd = fs.openSync(filename, "r");
  try {
    const size = fs.fstatSync(fd).size;
    if (size < 22)
      throw new Error("Provider did not return a complete ZIP archive");
    const start = Buffer.alloc(4);
    fs.readSync(fd, start, 0, 4, 0);
    const tail = Buffer.alloc(Math.min(size, 65557));
    fs.readSync(fd, tail, 0, tail.length, size - tail.length);
    if (![0x04034b50, 0x06054b50].includes(start.readUInt32LE(0)))
      throw new Error("Provider did not return a ZIP archive");
    for (let i = tail.length - 22; i >= 0; i--) {
      if (
        tail.readUInt32LE(i) === 0x06054b50 &&
        i + 22 + tail.readUInt16LE(i + 20) === tail.length
      )
        return;
    }
    throw new Error("Provider returned a truncated ZIP archive");
  } finally {
    fs.closeSync(fd);
  }
}

async function saveDownloads(
  outputDir,
  title,
  downloads,
  { requireZip = false } = {}
) {
  fs.mkdirSync(outputDir, { recursive: true });
  const manifest = { title, downloads: [] };
  const pending = [];
  try {
    for (const [index, item] of downloads.entries()) {
      const filename = path.basename(
        item.suggestedFilename.replace(/\\/g, "/")
      );
      const isZip = /\.zip$/i.test(filename);
      if (requireZip || isZip) validateZip(item.filePath);
      if (requireZip && !isZip)
        throw new Error("Expected a folder ZIP download");
      const extension = path
        .extname(filename)
        .replace(/[^.a-zA-Z0-9]/g, "")
        .slice(0, 16);
      const outputName = `download-${index + 1}${extension}`;
      const temporary = path.join(
        outputDir,
        `.${outputName}.${process.pid}.tmp`
      );
      pending.push({
        temporary,
        destination: path.join(outputDir, outputName),
      });
      await fs.promises.copyFile(item.filePath, temporary);
      const hash = crypto.createHash("sha256");
      for await (const chunk of fs.createReadStream(temporary))
        hash.update(chunk);
      manifest.downloads.push({
        path: outputName,
        filename,
        format: isZip ? "zip" : extension.slice(1) || "file",
        size: fs.statSync(temporary).size,
        sha256: hash.digest("hex"),
      });
    }
    if (!pending.length) throw new Error("Provider returned no downloads");
    for (const item of pending) fs.renameSync(item.temporary, item.destination);
    writeFileAtomic(
      path.join(outputDir, "downloads.json"),
      JSON.stringify(manifest, null, 2) + "\n"
    );
    return manifest;
  } finally {
    for (const item of pending) fs.rmSync(item.temporary, { force: true });
  }
}

module.exports = { validateZip, saveDownloads };
