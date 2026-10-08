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
  captureBrowserDownloads,
  resolveChromeLaunchOptions,
} = require("../chrome/chrome_utils.js");
const { saveDownloads } = require("../base/downloads.js");

async function main() {
  const config = loadConfig();
  const { url } = parseArgs();
  if (!url) throw new Error("Missing --url");
  if (!getEnvBool("MICROSOFT365_ENABLED", true)) {
    console.error("MICROSOFT365_ENABLED=False");
    return emitArchiveResultRecord("skipped", "MICROSOFT365_ENABLED=False");
  }
  const timeoutMs = getEnvInt("MICROSOFT365_TIMEOUT", 120) * 1000;
  const deadline = Date.now() + timeoutMs;
  const remaining = () => {
    const ms = deadline - Date.now();
    if (ms <= 0) throw new Error("Document download deadline exceeded");
    return ms;
  };
  const snapshotDir = path.resolve(config.SNAP_DIR || ".");
  const chromeSessionDir = path.join(snapshotDir, "chrome");
  const { browser, page, cdpSession } = await connectToPage({
    chromeSessionDir,
    timeoutMs,
  });
  let temporary;
  let downloads = [];
  try {
    const original = new URL(url);
    const sharepointHost = (host) => /(^|\.)sharepoint\.com$/.test(host);
    const sharepointCandidate = (value) =>
      value.protocol === "https:" &&
      sharepointHost(value.hostname) &&
      (/\/:([bwpx]):\//.test(value.pathname) ||
        (value.searchParams.get("p") === "true" &&
          /\.[a-z0-9]{1,12}$/i.test(value.searchParams.get("id") || "")));
    const officeSource = (value) => {
      const sourceURL = value.searchParams.get("src");
      if (
        value.protocol !== "https:" ||
        value.hostname !== "view.officeapps.live.com" ||
        !/^\/op\/(view|embed)\.aspx$/.test(value.pathname) ||
        !sourceURL ||
        !URL.canParse(sourceURL)
      )
        return null;
      const source = new URL(sourceURL);
      return /^https?:$/.test(source.protocol) &&
        !source.username &&
        !source.password &&
        /\.(docx?|xlsx?|pptx?)$/i.test(source.pathname)
        ? source
        : null;
    };
    const attached = new URL(page.url());
    const initialDocument =
      sharepointCandidate(original) ||
      sharepointCandidate(attached) ||
      Boolean(officeSource(original) || officeSource(attached));
    if (!initialDocument) {
      console.error("Not a Microsoft365 document URL");
      return emitArchiveResultRecord(
        "noresults",
        "Not a Microsoft365 document URL",
      );
    }
    await waitForNavigationComplete(chromeSessionDir, remaining());
    const current = new URL(page.url());
    const originalDocument =
      sharepointHost(original.hostname) &&
      /\/:([bwpx]):\//.test(original.pathname);
    const previewPath = current.searchParams.get("id") || "";
    const sharepoint =
      current.protocol === "https:" &&
      sharepointHost(current.hostname) &&
      (originalDocument ||
        /\/:([bwpx]):\//.test(current.pathname) ||
        (current.searchParams.get("p") === "true" &&
          /\.[a-z0-9]{1,12}$/i.test(previewPath)));
    if (sharepoint) {
      await page.bringToFront();
      downloads = await captureBrowserDownloads({
        browser,
        page,
        timeoutMs: remaining(),
        downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
        trigger: async () => {
          const button = await page.waitForSelector("#downloadCommand", {
            visible: true,
            timeout: remaining(),
          });
          await button.click();
        },
      });
      await saveDownloads(
        path.join(snapshotDir, "microsoft365"),
        await page.title(),
        downloads,
        { timeoutMs: remaining() },
      );
      return emitArchiveResultRecord(
        "succeeded",
        "microsoft365/downloads.json",
      );
    }
    if (
      initialDocument &&
      ["login.microsoftonline.com", "login.live.com"].includes(current.hostname)
    ) {
      console.error("Persona must be logged in to microsoft.com");
      return emitArchiveResultRecord(
        "skipped",
        "Persona must be logged in to microsoft.com",
      );
    }
    if (originalDocument)
      throw new Error(
        "Microsoft365 document requires an authorized browser session or a supported native Download control",
      );
    if (
      current.protocol !== "https:" ||
      current.hostname !== "view.officeapps.live.com" ||
      !/^\/op\/(view|embed)\.aspx$/.test(current.pathname)
    ) {
      console.error("Not an Office web viewer document");
      return emitArchiveResultRecord(
        "noresults",
        "Not an Office web viewer document",
      );
    }
    const source = officeSource(current);
    if (!source) {
      console.error("Office viewer has no valid document source URL");
      return emitArchiveResultRecord(
        "noresults",
        "Office viewer has no valid document source URL",
      );
    }
    const filename = decodeURIComponent(source.pathname.split("/").pop());
    const extension = path.extname(filename).toLowerCase();
    if (
      ![".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt"].includes(extension)
    ) {
      console.error("Unsupported Office document format");
      return emitArchiveResultRecord(
        "noresults",
        "Unsupported Office document format",
      );
    }
    const output = path.join(snapshotDir, "microsoft365");
    await fs.promises.mkdir(output, { recursive: true });
    temporary = path.join(output, `.download-${process.pid}.tmp`);
    const response = await downloadBrowserResource({
      cdpSession,
      url: source.href,
      outputPath: temporary,
      timeoutMs: remaining(),
    });
    const type =
      Object.entries(response.headers).find(
        ([key]) => key.toLowerCase() === "content-type",
      )?.[1] || "";
    const file = await fs.promises.open(temporary, "r");
    let header;
    try {
      header = Buffer.alloc(8);
      await file.read(header, 0, 8, 0);
    } finally {
      await file.close();
    }
    const modern = extension.endsWith("x");
    const signature = modern
      ? Buffer.from([0x50, 0x4b, 0x03, 0x04])
      : Buffer.from([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1]);
    if (
      /html|json/i.test(type) ||
      !header.subarray(0, signature.length).equals(signature)
    )
      throw new Error("Office viewer source did not return an Office document");
    await saveDownloads(
      output,
      filename,
      [{ filePath: temporary, suggestedFilename: filename }],
      { timeoutMs: remaining() },
    );
    emitArchiveResultRecord("succeeded", "microsoft365/downloads.json");
  } finally {
    for (const { filePath } of downloads)
      await fs.promises.unlink(filePath).catch((error) => {
        if (error.code !== "ENOENT") console.error(error.message);
      });
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
