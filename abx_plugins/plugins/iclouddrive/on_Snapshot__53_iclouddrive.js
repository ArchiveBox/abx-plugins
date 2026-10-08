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
  if (!getEnvBool("ICLOUDDRIVE_ENABLED", true)) {
    console.error("ICLOUDDRIVE_ENABLED=False");
    return emitArchiveResultRecord("skipped", "ICLOUDDRIVE_ENABLED=False");
  }
  const timeoutMs = getEnvInt("ICLOUDDRIVE_TIMEOUT", 120) * 1000;
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
      value.hostname === "www.icloud.com" &&
      /^\/iclouddrive\/[A-Za-z0-9_-]+\/?$/.test(value.pathname);
    if (!candidate(new URL(url)) && !candidate(new URL(page.url()))) {
      console.error("Not an iCloud Drive public share");
      return emitArchiveResultRecord(
        "noresults",
        "Not an iCloud Drive public share",
      );
    }
    await waitForNavigationComplete(chromeSessionDir, remaining());
    const current = new URL(page.url());
    const share = current.pathname.match(
      /^\/iclouddrive\/([A-Za-z0-9_-]+)\/?$/,
    )?.[1];
    if (
      current.protocol !== "https:" ||
      current.hostname !== "www.icloud.com" ||
      !share
    ) {
      console.error("Not an iCloud Drive public share");
      return emitArchiveResultRecord(
        "noresults",
        "Not an iCloud Drive public share",
      );
    }
    // Resolve the public file in the attached browser's context. No Apple login,
    // API token, separate HTTP session, or new browser navigation is required.
    const resolved = await page.evaluate(
      async (token, ms) => {
        const response = await fetch(
          "https://ckdatabasews.icloud.com/database/1/com.apple.cloudkit/production/public/records/resolve",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ shortGUIDs: [{ value: token }] }),
            signal: AbortSignal.timeout(ms),
          },
        );
        if (!response.ok)
          throw new Error(
            `iCloud share resolution failed: HTTP ${response.status}`,
          );
        const result = (await response.json()).results?.[0];
        return {
          fields: result?.rootRecord?.fields,
          error: result?.serverErrorCode,
          reason: result?.reason,
          login: result?.requireAppleLogin,
        };
      },
      share,
      remaining(),
    );
    if (resolved.login) {
      console.error("Persona must be logged in to icloud.com");
      return emitArchiveResultRecord(
        "skipped",
        "Persona must be logged in to icloud.com",
      );
    }
    if (resolved.error)
      throw new Error(
        `iCloud share unavailable: ${resolved.error} (${
          resolved.reason || "Unknown reason"
        })`,
      );
    const fields = resolved.fields;
    const asset = fields?.fileContent?.value;
    if (!asset?.downloadURL)
      throw new Error(
        "iCloud share has no public original file; folders and restricted shares require an authorized Apple Account session",
      );
    const basename = Buffer.from(
      fields.encryptedBasename?.value || "",
      "base64",
    ).toString("utf8");
    const extension = fields.extension?.value;
    if (!basename)
      throw new Error("iCloud did not return the original filename");
    const filename = extension ? `${basename}.${extension}` : basename;
    const downloadURL = new URL(
      asset.downloadURL.replace("${f}", encodeURIComponent(filename)),
    );
    if (
      downloadURL.protocol !== "https:" ||
      !downloadURL.hostname.endsWith(".icloud-content.com")
    )
      throw new Error("iCloud returned an unexpected original download URL");
    const output = path.join(snapshotDir, "iclouddrive");
    await fs.promises.mkdir(output, { recursive: true });
    temporary = path.join(output, `.download-${process.pid}.tmp`);
    await downloadBrowserResource({
      cdpSession,
      url: downloadURL.href,
      outputPath: temporary,
      timeoutMs: remaining(),
    });
    if (fs.statSync(temporary).size !== asset.size)
      throw new Error(
        "iCloud original download size does not match share metadata",
      );
    await saveDownloads(
      output,
      filename,
      [{ filePath: temporary, suggestedFilename: filename }],
      { timeoutMs: remaining() },
    );
    emitArchiveResultRecord("succeeded", "iclouddrive/downloads.json");
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
