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
  readNavigatedUrls,
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
  const navigatedUrls = readNavigatedUrls(chromeSessionDir);
  if (navigatedUrls && ![url, ...navigatedUrls].some(isDesign)) {
    console.error("Not a Canva design");
    return emitArchiveResultRecord("noresults", "Not a Canva design");
  }
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
    const reportControls = async (frame, selector, target = null) => {
      try {
        const state = await frame.evaluate((selector, target) => {
          const control = document.querySelector(
            '[role="combobox"][aria-label="File type"]',
          );
          return {
            selectedFormat: control?.innerText.slice(0, 100),
            expanded: control?.getAttribute("aria-expanded"),
            viewport: { width: innerWidth, height: innerHeight },
            documentSize: {
              width: document.documentElement.clientWidth,
              height: document.documentElement.clientHeight,
            },
            // Re-querying can hide a replaced control; retain the failed handle.
            target: target && {
              connected: target.isConnected,
              tag: target.tagName,
              current: target === control,
              rects: [...target.getClientRects()].map((rect) => ({
                x: rect.x,
                y: rect.y,
                width: rect.width,
                height: rect.height,
              })),
            },
            controls: [...document.querySelectorAll(selector)]
              .slice(0, 20)
              .map((el) => {
                const box = el.getBoundingClientRect();
                return {
                  label: el.getAttribute("aria-label"),
                  text: el.innerText.slice(0, 100),
                  disabled:
                    Boolean(el.disabled) ||
                    el.getAttribute("aria-disabled") === "true",
                  visible: box.width > 0 && box.height > 0,
                  centerExposed: el.contains(
                    document.elementFromPoint(
                      box.x + box.width / 2,
                      box.y + box.height / 2,
                    ),
                  ),
                };
              }),
          };
        }, selector, target);
        console.error("Canva export controls:", JSON.stringify(state));
      } catch (error) {
        console.error("Canva menu diagnostics unavailable:", error.message);
      }
    };
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
                ![name]
                  .flat()
                  .some((label) =>
                    role === "option"
                      ? text.startsWith(label + " ") || text === label
                      : text === label,
                  )
              )
                continue;
              if (el.disabled || el.getAttribute("aria-disabled") === "true")
                continue;
              const bounds = el.getBoundingClientRect();
              if (!bounds.width || !bounds.height) continue;
              el.scrollIntoView({
                block: "nearest",
                inline: "nearest",
                behavior: "instant",
              });
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
              // Role-less popup wrappers can still transform the click target.
              let animating = false;
              for (
                let ancestor = el;
                ancestor;
                ancestor = ancestor.parentElement
              ) {
                if (
                  ancestor
                    .getAnimations()
                    .some((animation) => animation.playState === "running")
                ) {
                  animating = true;
                  break;
                }
              }
              if (animating) continue;
              return el;
            }
            return false;
          },
          { polling: 100, timeout: remaining() },
          role,
          name,
          selector,
        )
        .catch(async () => {
          await reportControls(frame, selector);
          throw new Error(`Canva export control unavailable: ${role} ${name}`);
        });
      const element = handle.asElement();
      try {
        // The export tab can stop painting under Xvfb even with focus emulation.
        // Readiness and scrolling above must not be followed by Locator's RAF /
        // IntersectionObserver waits. Keep a trusted click at the native point,
        // which also accounts for controls inside the template iframe.
        const { x, y } = await element.clickablePoint();
        await page.mouse.click(x, y);
      } catch (error) {
        await reportControls(frame, selector, element);
        throw new Error(`Canva export click failed: ${role} ${name}: ${error.message}`);
      } finally {
        await handle.dispose();
      }
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
          "noresults",
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
    // Compact layouts label the publish menu differently and also expose a
    // separate Share control that only changes collaboration permissions.
    await click(
      "menuitem",
      ["Share", "All Publish Options"],
      page,
      '[role="menuitem"]:not([aria-label="Share"])',
    );
    await click("button", "Download");
    await click("combobox", "File type");
    await page
      .waitForSelector('[role="option"]', {
        visible: true,
        timeout: remaining(),
      })
      .catch(async (error) => {
        await reportControls(page, '[role="option"]');
        throw error;
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
        await click(
          "menuitem",
          ["Share", "All Publish Options"],
          page,
          '[role="menuitem"]:not([aria-label="Share"])',
        );
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
    emitArchiveResultRecord("noresults", error.message);
    return;
  }
  console.error(error.stack || error.message);
  emitArchiveResultRecord("failed", error.message);
  process.exitCode = 1;
});
