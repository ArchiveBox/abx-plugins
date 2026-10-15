#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries node
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
  readNavigatedUrls,
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
  const navigatedUrls = readNavigatedUrls(path.join(snapshotDir, "chrome"));
  if (navigatedUrls) {
    // Without requireContent, isNotionPage checks only the URL, not the page.
    const matches = await Promise.all(
      [url, ...navigatedUrls].map((value) =>
        isNotionPage(null, new URL(value), false),
      ),
    );
    if (!matches.some(Boolean)) {
      console.error("Not a Notion document page");
      return emitArchiveResultRecord("noresults", "Not a Notion document page");
    }
  }
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
    // Notion renders table skeletons before its page-data requests finish.
    // Wait for same-origin fetch/XHR bodies, not page-wide network idle: the
    // SQLite shared-worker request transfers to another target without a page
    // completion event, and third-party telemetry may continue indefinitely.
    const origin = new URL(page.url()).origin;
    const pending = new Set();
    let lastActivity = Date.now();
    const started = (request) => {
      if (
        ["fetch", "xhr"].includes(request.resourceType()) &&
        new URL(request.url()).origin === origin
      ) {
        pending.add(request);
        lastActivity = Date.now();
      }
    };
    const finished = (request) => {
      if (pending.delete(request)) lastActivity = Date.now();
    };
    page.on("request", started);
    page.on("requestfinished", finished);
    page.on("requestfailed", finished);
    let renderedItems = null;
    try {
      await page.waitForFunction(
        () => document.querySelector(".notion-page-content")?.innerText.trim(),
        // Text-node updates do not trigger Puppeteer's mutation poller, and
        // hidden capture tabs can suspend its default animation-frame poller.
        { timeout: remaining(), polling: 100 },
      );
      const waitForPageData = async () => {
        lastActivity = Date.now();
        while (pending.size || Date.now() - lastActivity < 500) {
          remaining();
          await new Promise((resolve) => setTimeout(resolve, 100));
        }
      };
      await waitForPageData();
      if (await page.$(".notion-page-content [data-virtual-item-slot]")) {
        // Public pages virtualize their DOM: neither network idle nor one
        // final DOM clone includes blocks removed above/below the viewport.
        // Visit the actual scroll container and retain the rendered HTML,
        // replacing earlier copies as partially loaded blocks fill in.
        const scroller = await page.evaluateHandle(() => {
          let element = document.querySelector(".notion-page-content").parentElement;
          while (element && !(element.scrollHeight > element.clientHeight &&
            ["auto", "scroll"].includes(getComputedStyle(element).overflowY))) element = element.parentElement;
          return element || document.scrollingElement;
        });
        const originalTop = await scroller.evaluate(element => element.scrollTop);
        const items = new Map();
        try {
          await page.bringToFront();
          await scroller.evaluate(element => { element.scrollTop = 0; });
          while (true) {
            await waitForPageData();
            for (const [index, html] of await page.evaluate(() =>
              [...document.querySelectorAll(".notion-page-content [data-virtual-item-slot] > [data-index]")]
                .map(element => [Number(element.dataset.index), element.outerHTML]))) items.set(index, html);
            const atEnd = await scroller.evaluate(element => {
              if (element.scrollTop + element.clientHeight >= element.scrollHeight - 1) return true;
              element.scrollTop += Math.max(1, Math.floor(element.clientHeight * 0.8));
              return false;
            });
            if (atEnd) break;
          }
          const ordered = [...items].sort(([a], [b]) => a - b);
          if (!ordered.length || ordered.some(([index], position) => index !== position))
            throw new Error("Notion virtual page has missing rendered sections");
          renderedItems = ordered.map(([, html]) => html);
        } finally {
          await scroller.evaluate((element, top) => { element.scrollTop = top; }, originalTop);
          await scroller.dispose();
        }
      }
    } finally {
      page.off("request", started);
      page.off("requestfinished", finished);
      page.off("requestfailed", finished);
    }
    const saved = await page.evaluate((renderedItems) => {
      const root = document.querySelector(".notion-page-content");
      const title =
        document
          .querySelector('[role="heading"][aria-level="1"]')
          ?.textContent.trim() || document.title;
      const content = root.cloneNode(true);
      if (renderedItems) {
        // Drop virtual scroll spacers and absolute-positioned viewport wrappers.
        // The actual rendered sections remain in document order and normal flow.
        const template = document.createElement("template");
        template.innerHTML = renderedItems.join("");
        content.replaceChildren(template.content);
      }
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
    }, renderedItems);
    const outputDir = path.join(snapshotDir, "notion");
    await fs.promises.mkdir(outputDir, { recursive: true });
    staging = await fs.promises.mkdtemp(path.join(outputDir, ".capture-"));
    const htmlPath = path.join(staging, "public-page.html");
    const markdownPath = path.join(staging, "public-page.md");
    writeFileAtomic(htmlPath, saved.html);
    // Resolve the existing converter only when there is a page to convert.
    // Unrelated captures must not install Defuddle or emit installer logs.
    const { stdout: converterEnv } = await promisify(execFile)(
      "abxpkg",
      [
        "env",
        "--install",
        "--json",
        `--deps-from=${path.join(__dirname, "../defuddle/config.json")}:required_binaries`,
      ],
      { timeout: remaining(), maxBuffer: 1024 * 1024 },
    );
    const env = { ...process.env, ...JSON.parse(converterEnv) };
    // Run the configured launcher through abxpkg: an env/bin projection of a
    // pnpm shell shim changes its $0-based package path when executed directly.
    const { stdout } = await promisify(execFile)(
      "abxpkg",
      [
        "run",
        "--binproviders=env",
        process.env.DEFUDDLE_BINARY || env.DEFUDDLE_BINARY,
        "parse",
        htmlPath,
        "--markdown",
      ],
      { env, timeout: remaining(), maxBuffer: 32 * 1024 * 1024 },
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
