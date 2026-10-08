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
  openExportPage,
  closeExportPage,
  waitForNavigationComplete,
  captureBrowserDownloads,
  resolveChromeLaunchOptions,
} = require("../chrome/chrome_utils.js");
const { saveDownloads } = require("../base/downloads.js");

class ExportPrerequisiteError extends Error {}

async function main() {
  const config = loadConfig();
  const { url } = parseArgs();
  if (!url) throw new Error("Missing --url");
  if (!getEnvBool("CANVA_ENABLED", true)) {
    console.error("CANVA_ENABLED=False");
    return emitArchiveResultRecord("skipped", "CANVA_ENABLED=False");
  }
  const timeoutMs = getEnvInt("CANVA_TIMEOUT", 120) * 1000;
  const deadline = Date.now() + timeoutMs;
  const remaining = () => {
    const ms = deadline - Date.now();
    if (ms <= 0) throw new Error("Document export deadline exceeded");
    return ms;
  };
  const snapshotDir = path.resolve(config.SNAP_DIR || ".");
  const chromeSessionDir = path.join(snapshotDir, "chrome");
  const isDesign = (value) => {
    try {
      const candidate = new URL(value);
      return (
        candidate.protocol === "https:" &&
        ["www.canva.com", "canva.com"].includes(candidate.hostname) &&
        /^\/design\/[^/]+/.test(candidate.pathname)
      );
    } catch {
      return false;
    }
  };
  const { browser, page: sourcePage } = await connectToPage({
    chromeSessionDir,
    timeoutMs,
  });
  let page = sourcePage;
  const downloads = [];
  try {
    if (!isDesign(url) && !isDesign(page.url())) {
      console.error("Not a Canva design");
      return emitArchiveResultRecord("noresults", "Not a Canva design");
    }
    await waitForNavigationComplete(chromeSessionDir, remaining());
    if (!isDesign(page.url())) {
      console.error("Not a Canva design");
      return emitArchiveResultRecord("noresults", "Not a Canva design");
    }
    const current = new URL(page.url());
    if (
      current.pathname.endsWith("/watch") ||
      (current.pathname.endsWith("/view") &&
        current.searchParams.get("mode") !== "preview")
    ) {
      console.error("No export in public view");
      return emitArchiveResultRecord("noresults", "No export in public view");
    }
    // Export controls must never modify the shared capture tab.
    page = await openExportPage({
      page: sourcePage,
      chromeSessionDir: path.join(snapshotDir, "chrome"),
      timeoutMs: remaining(),
    });
    await page.goto(sourcePage.url(), {
      waitUntil: "domcontentloaded",
      timeout: remaining(),
    });
    const click = async (
      role,
      name,
      frame = page,
      selector = role === "button" ? "button" : `[role="${role}"]`,
    ) => {
      const handle = await frame
        .waitForFunction(
          (role, name, selector) => {
            for (const el of document.querySelectorAll(selector)) {
              const labelledBy = (el.getAttribute("aria-labelledby") || "")
                .split(/\s+/)
                .map((id) => document.getElementById(id)?.innerText || "")
                .join(" ")
                .trim();
              const label = el.id
                ? [...document.querySelectorAll("label")].find(
                    (label) => label.htmlFor === el.id,
                  )?.innerText
                : "";
              const text = (
                el.getAttribute("aria-label") ||
                labelledBy ||
                label ||
                el.innerText ||
                ""
              )
                .trim()
                .replace(/\s+/g, " ");
              if (
                role === "option"
                  ? !text.startsWith(name + " ") && text !== name
                  : text !== name
              )
                continue;
              if (el.disabled || el.getAttribute("aria-disabled") === "true")
                continue;
              const r = el.getBoundingClientRect();
              if (
                !r.width ||
                !r.height ||
                !el.contains(
                  document.elementFromPoint(
                    r.x + r.width / 2,
                    r.y + r.height / 2,
                  ),
                )
              )
                continue;
              const popup = el.closest(
                '[role="listbox"], [role="dialog"], [role="region"]',
              );
              if (
                popup
                  ?.getAnimations()
                  .some((animation) => animation.playState === "running")
              )
                continue;
              return el;
            }
            return false;
          },
          { polling: 100, timeout: remaining() },
          role,
          name,
          selector,
        )
        .catch(() => {
          throw new Error(`Canva export control unavailable: ${role} ${name}`);
        });
      const element = handle.asElement();
      const box = await element.boundingBox();
      if (!box) throw new Error("Canva export control is not visible");
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    };
    if (
      current.pathname.endsWith("/view") &&
      current.searchParams.get("mode") === "preview"
    ) {
      await click("button", "View template");
      const iframe = await page.waitForSelector(
        'iframe[title="Open template"]',
        { visible: true, timeout: remaining() },
      );
      const frame = await iframe.contentFrame();
      if (!frame)
        throw new Error("Canva template sign-in frame is unavailable");
      const state = await frame.waitForFunction(
        () => {
          if (
            [...document.querySelectorAll("button")].some(
              (el) => el.innerText.trim() === "Open in Editor",
            )
          )
            return "ready";
          if (
            /We.ll have you designing again soon|RayID:|verify you are human/i.test(
              document.body.innerText,
            )
          )
            return "blocked";
          if (
            /Sign up|Log in|Sign in|Continue with Google|Continue with email/i.test(
              document.body.innerText,
            )
          )
            return "sign-in";
          return false;
        },
        { polling: 100, timeout: remaining() },
      );
      const templateState = await state.jsonValue();
      if (templateState === "blocked")
        throw new Error(
          "Canva blocked this browser session in the template frame; use an existing browser session accepted by Canva",
        );
      if (templateState === "sign-in") {
        console.error("Persona must be logged in to canva.com");
        return emitArchiveResultRecord(
          "skipped",
          "Persona must be logged in to canva.com",
        );
      }
      // This author-provided action creates a copy in the signed-in account.
      // It does not edit the source design or change sharing permissions.
      await Promise.all([
        page.waitForNavigation({
          timeout: remaining(),
          waitUntil: "domcontentloaded",
        }),
        click("button", "Open in Editor", frame),
      ]);
    }
    await click("menuitem", "Share");
    await click("button", "Download");
    await click("combobox", "File type");
    await page.waitForSelector('[role="option"]', {
      visible: true,
      timeout: remaining(),
    });
    const native = await page.evaluate(
      () =>
        [...document.querySelectorAll('[role="option"]')]
          .map((el) => el.innerText)
          .find((text) => /PPTX|DOCX|XLSX/.test(text)) || null,
    );
    const formats = native
      ? [native.match(/PPTX|DOCX|XLSX/)[0], "PDF"]
      : ["PDF"];
    for (let index = 0; index < formats.length; index++) {
      if (index) {
        await click("menuitem", "Share");
        await click("button", "Download");
        await click("combobox", "File type");
      }
      await click("option", formats[index]);
      downloads.push(
        ...(await captureBrowserDownloads({
          browser,
          page,
          timeoutMs: remaining(),
          downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
          trigger: async ({ downloadStarted }) => {
            // The Share menu also has a Download button. Submit the actual
            // export form only after its selected file type is displayed.
            await page.waitForFunction(
              (format) =>
                [...document.querySelectorAll('form [role="combobox"]')].some(
                  (el) => el.innerText.trim().startsWith(format),
                ),
              { polling: 100, timeout: remaining() },
              formats[index],
            );
            await click(
              "button",
              "Download",
              page,
              'form button[type="submit"]',
            );
            await Promise.race([
              downloadStarted,
              page
                .waitForFunction(
                  () =>
                    /Your design contains premium content/.test(
                      document.body.innerText,
                    ),
                  { timeout: remaining() },
                )
                .then(() => {
                  throw new ExportPrerequisiteError("Export requires payment");
                }),
            ]);
          },
        })),
      );
      await page.keyboard.press("Escape");
    }
    for (const download of downloads) {
      const file = await fs.promises.open(download.filePath, "r");
      const { buffer, bytesRead } = await file
        .read(Buffer.alloc(5), 0, 5, 0)
        .finally(() => file.close());
      const data = buffer.subarray(0, bytesRead);
      const extension = path.extname(download.suggestedFilename).toLowerCase();
      if (
        extension === ".pdf"
          ? !data.subarray(0, 5).equals(Buffer.from("%PDF-"))
          : ![".pptx", ".docx", ".xlsx"].includes(extension) ||
            !data.subarray(0, 4).equals(Buffer.from([0x50, 0x4b, 3, 4]))
      )
        throw new Error("Canva returned an invalid document export");
    }
    await saveDownloads(
      path.join(snapshotDir, "canva"),
      await page.title(),
      downloads,
      { timeoutMs: remaining() },
    );
    emitArchiveResultRecord("succeeded", "canva/downloads.json");
  } finally {
    if (page !== sourcePage)
      await closeExportPage({
        page,
        chromeSessionDir: path.join(snapshotDir, "chrome"),
      });
    for (const { filePath } of downloads)
      await fs.promises.unlink(filePath).catch((error) => {
        if (error.code !== "ENOENT") console.error(error.message);
      });
    await browser.disconnect();
  }
}
main().catch((error) => {
  if (error instanceof ExportPrerequisiteError) {
    console.error(error.message);
    emitArchiveResultRecord("skipped", error.message);
    return;
  }
  console.error(error.message);
  emitArchiveResultRecord("failed", error.message);
  process.exitCode = 1;
});
