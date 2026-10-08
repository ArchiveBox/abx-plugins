#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
const fs = require("fs");
const path = require("path");
const {
  loadConfig,
  getEnvBool,
  getEnvInt,
  parseArgs,
  emitArchiveResultRecord,
} = require("../base/utils.js");
const {
  connectToPage,
  waitForNavigationComplete,
  downloadBrowserResource,
} = require("../chrome/chrome_utils.js");
const { saveDownloads } = require("../base/downloads.js");

async function main() {
  const config = loadConfig();
  const { url } = parseArgs();
  if (!url) throw new Error("Missing --url");
  if (!getEnvBool("ONEDRIVE_ENABLED", true)) {
    console.error("ONEDRIVE_ENABLED=False");
    return emitArchiveResultRecord("skipped", "ONEDRIVE_ENABLED=False");
  }
  const timeoutMs = getEnvInt("ONEDRIVE_TIMEOUT", 120) * 1000;
  const deadline = Date.now() + timeoutMs;
  const remaining = () => {
    const ms = deadline - Date.now();
    if (ms <= 0) throw new Error("Provider download deadline exceeded");
    return ms;
  };
  const snapshotDir = path.resolve(config.SNAP_DIR || ".");
  const chromeSessionDir = path.join(snapshotDir, "chrome");
  const { browser, page, cdpSession } = await connectToPage({
    chromeSessionDir,
    timeoutMs,
  });
  let temporary;
  try {
    const candidate = (value) =>
      value.protocol === "https:" &&
      ((value.hostname === "1drv.ms" &&
        /^\/[a-z]\/[\w!-]+/i.test(value.pathname)) ||
        (value.hostname === "onedrive.live.com" &&
          (value.searchParams.has("redeem") ||
            value.searchParams.has("authkey") ||
            /\/:\w:\//.test(value.pathname))));
    const source = new URL(url);
    if (!candidate(source) && !candidate(new URL(page.url()))) {
      console.error("Not a shared OneDrive item");
      return emitArchiveResultRecord("noresults", "Not a shared OneDrive item");
    }
    await waitForNavigationComplete(chromeSessionDir, remaining());
    const current = new URL(page.url());
    if (
      candidate(source) &&
      ["login.live.com", "login.microsoftonline.com"].includes(current.hostname)
    ) {
      console.error("Persona must be logged in to onedrive.live.com");
      return emitArchiveResultRecord(
        "skipped",
        "Persona must be logged in to onedrive.live.com",
      );
    }
    if (
      current.protocol !== "https:" ||
      current.hostname !== "onedrive.live.com"
    ) {
      console.error("Not a OneDrive share");
      return emitArchiveResultRecord("noresults", "Not a OneDrive share");
    }
    if (
      !current.searchParams.has("redeem") &&
      !current.searchParams.has("authkey") &&
      !/\/:\w:\//.test(current.pathname)
    ) {
      console.error("Not a shared OneDrive item");
      return emitArchiveResultRecord("noresults", "Not a shared OneDrive item");
    }
    const shareURL = candidate(source) ? url : current.href;
    const token = "u!" + Buffer.from(shareURL).toString("base64url");
    const exportURL = `https://onedrive.live.com/_api/v2.0/shares/${token}/driveItem/content`;
    const output = path.join(snapshotDir, "onedrive");
    await fs.promises.mkdir(output, { recursive: true });
    temporary = path.join(output, `.download-${process.pid}.tmp`);
    const response = await downloadBrowserResource({
      cdpSession,
      url: exportURL,
      outputPath: temporary,
      timeoutMs: remaining(),
    });
    const headers = Object.fromEntries(
      Object.entries(response.headers).map(([key, value]) => [
        key.toLowerCase(),
        value,
      ]),
    );
    const disposition = headers["content-disposition"] || "";
    const encoded = disposition.match(/filename\*=utf-8''([^;]+)/i)?.[1];
    const filename = encoded
      ? decodeURIComponent(encoded)
      : disposition.match(/filename="([^"]+)"/i)?.[1];
    if (!filename || !/attachment/i.test(disposition))
      throw new Error("OneDrive did not return an original file attachment");
    if (!fs.statSync(temporary).size)
      throw new Error("OneDrive returned an empty download");
    if (filename.toLowerCase().endsWith(".pdf")) {
      const file = await fs.promises.open(temporary, "r");
      try {
        const header = Buffer.alloc(5);
        await file.read(header, 0, 5, 0);
        if (header.toString() !== "%PDF-")
          throw new Error("OneDrive returned an invalid PDF");
      } finally {
        await file.close();
      }
    }
    await saveDownloads(
      output,
      filename,
      [{ filePath: temporary, suggestedFilename: filename }],
      { timeoutMs: remaining() },
    );
    emitArchiveResultRecord("succeeded", "onedrive/downloads.json");
  } finally {
    if (temporary)
      await fs.promises.unlink(temporary).catch((error) => {
        if (error.code !== "ENOENT") console.error(error.message);
      });
    await browser.disconnect();
  }
}
main().catch((error) => {
  console.error(error.message);
  emitArchiveResultRecord("failed", error.message);
  process.exitCode = 1;
});
