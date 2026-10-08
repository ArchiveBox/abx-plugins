#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,../defuddle/config.json:required_binaries node
// /// script
// ///
const fs = require("fs");
const path = require("path");
const { execFile } = require("child_process");
const { promisify } = require("util");
const {
  loadConfig,
  getEnvBool,
  getEnvInt,
  parseArgs,
  emitArchiveResultRecord,
  writeFileAtomic,
} = require("../base/utils.js");
const {
  connectToPage,
  waitForNavigationComplete,
} = require("../chrome/chrome_utils.js");
const { saveDownloads } = require("../base/downloads.js");

async function isNotionPage(
  page,
  url = new URL(page.url()),
  requireContent = true,
) {
  if (
    url.protocol !== "https:" ||
    !/(^|\.)notion\.(so|site|com)$/.test(url.hostname)
  )
    return false;
  // Standard document URLs end in a 32-character page ID, optionally with a
  // title slug. Published sites can put a document at their subdomain root.
  if (/(?:^|\/)[^/]*[0-9a-f]{32}\/?$/i.test(url.pathname)) return true;
  return (
    url.hostname.endsWith(".notion.site") &&
    url.pathname === "/" &&
    (!requireContent ||
      (await page.evaluate(() =>
        Boolean(document.querySelector(".notion-page-content")),
      )))
  );
}

async function main() {
  const { url } = parseArgs();
  if (!url) throw new Error("Missing --url");
  if (!getEnvBool("NOTION_ENABLED", true)) {
    console.error("NOTION_ENABLED=False");
    return emitArchiveResultRecord("skipped", "NOTION_ENABLED=False");
  }
  const config = loadConfig();
  const snapshotDir = path.resolve(config.SNAP_DIR || ".");
  const timeoutMs = getEnvInt("NOTION_TIMEOUT", 120) * 1000;
  const deadline = Date.now() + timeoutMs;
  const remaining = () => {
    const value = deadline - Date.now();
    if (value <= 0) throw new Error("Notion capture deadline exceeded");
    return value;
  };
  const { browser, page } = await connectToPage({
    chromeSessionDir: path.join(snapshotDir, "chrome"),
    timeoutMs,
  });
  let staging;
  try {
    if (
      !(await isNotionPage(page, new URL(url), false)) &&
      !(await isNotionPage(page, new URL(page.url()), false))
    ) {
      console.error("Not a Notion document page");
      return emitArchiveResultRecord("noresults", "Not a Notion document page");
    }
    await waitForNavigationComplete(
      path.join(snapshotDir, "chrome"),
      remaining(),
    );
    if (!(await isNotionPage(page))) {
      console.error("Not a Notion document page");
      return emitArchiveResultRecord("noresults", "Not a Notion document page");
    }
    // Snapshot tabs may be hidden, so requestAnimationFrame polling can stop
    // even after Notion has rendered. Observe DOM changes instead.
    await page.waitForFunction(
      () => document.querySelector(".notion-page-content")?.innerText.trim(),
      { timeout: remaining(), polling: "mutation" },
    );
    const saved = await page.evaluate(() => {
      const root = document.querySelector(".notion-page-content");
      const title =
        document
          .querySelector('[role="heading"][aria-level="1"]')
          ?.textContent.trim() || document.title;
      const content = root.cloneNode(true);
      // Preserve actual rendered content, with standard semantic HTML for the
      // existing converter. No private Notion records or block reconstruction.
      for (const heading of content.querySelectorAll(
        '[role="heading"][aria-level]',
      )) {
        const level = Number(heading.getAttribute("aria-level"));
        if (level < 1 || level > 6) continue;
        const tag = document.createElement(`h${level}`);
        tag.innerHTML = heading.innerHTML;
        heading.replaceWith(tag);
      }
      for (const link of content.querySelectorAll("a[href]"))
        link.setAttribute("href", link.href);
      for (const image of content.querySelectorAll("img[src]"))
        image.setAttribute("src", image.src);
      for (const unsafe of content.querySelectorAll("script,style,noscript"))
        unsafe.remove();
      for (const element of content.querySelectorAll("*")) {
        for (const attribute of [...element.attributes]) {
          if (attribute.name.startsWith("on"))
            element.removeAttribute(attribute.name);
        }
      }
      const wrapper = document.createElement("html");
      const head = document.createElement("head");
      const charset = document.createElement("meta");
      charset.setAttribute("charset", "utf-8");
      const titleTag = document.createElement("title");
      titleTag.textContent = title;
      head.append(charset, titleTag);
      const body = document.createElement("body");
      const article = document.createElement("article");
      const h1 = document.createElement("h1");
      h1.textContent = title;
      article.append(h1, content);
      body.append(article);
      wrapper.append(head, body);
      return { title, html: "<!doctype html>\n" + wrapper.outerHTML };
    });
    const outputDir = path.join(snapshotDir, "notion");
    await fs.promises.mkdir(outputDir, { recursive: true });
    staging = await fs.promises.mkdtemp(path.join(outputDir, ".capture-"));
    const htmlPath = path.join(staging, "public-page.html");
    const markdownPath = path.join(staging, "public-page.md");
    writeFileAtomic(htmlPath, saved.html);
    const converter = loadConfig(
      path.join(__dirname, "../defuddle/config.json"),
    );
    const { stdout } = await promisify(execFile)(
      converter.DEFUDDLE_BINARY,
      ["parse", htmlPath, "--markdown"],
      { timeout: remaining(), maxBuffer: 32 * 1024 * 1024 },
    );
    if (!stdout.trim())
      throw new Error("Saved public page produced no Markdown");
    // Defuddle omits the document's title from its article body.
    writeFileAtomic(
      markdownPath,
      `# ${saved.title.replace(/\s+/g, " ")}\n\n${stdout}`,
    );
    await saveDownloads(
      outputDir,
      saved.title,
      [
        { filePath: htmlPath, suggestedFilename: "public-page.html" },
        { filePath: markdownPath, suggestedFilename: "public-page.md" },
      ],
      { timeoutMs: remaining() },
    );
    emitArchiveResultRecord("succeeded", "notion/downloads.json");
  } finally {
    if (staging)
      await fs.promises.rm(staging, { recursive: true, force: true });
    await browser.disconnect();
  }
}
main().catch((error) => {
  console.error(error.message);
  emitArchiveResultRecord("failed", error.message);
  process.exitCode = 1;
});
