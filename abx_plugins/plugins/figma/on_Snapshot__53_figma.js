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
  if (!getEnvBool("FIGMA_ENABLED", true)) {
    console.error("FIGMA_ENABLED=False");
    return emitArchiveResultRecord("skipped", "FIGMA_ENABLED=False");
  }
  const timeoutMs = getEnvInt("FIGMA_TIMEOUT", 120) * 1000;
  const deadline = Date.now() + timeoutMs;
  const remaining = () => {
    const ms = deadline - Date.now();
    if (ms <= 0) throw new Error("Document export deadline exceeded");
    return ms;
  };
  const snapshotDir = path.resolve(config.SNAP_DIR || ".");
  const isDocument = (candidate) =>
    candidate.protocol === "https:" &&
    ["www.figma.com", "figma.com"].includes(candidate.hostname) &&
    /^\/(?:design|file|board|slides|buzz|sites|make)\/[^/]+/.test(
      candidate.pathname,
    );
  const navigatedUrls = readNavigatedUrls(path.join(snapshotDir, "chrome"));
  if (
    navigatedUrls &&
    ![url, ...navigatedUrls].some((value) => isDocument(new URL(value)))
  ) {
    console.error("Not a Figma document");
    return emitArchiveResultRecord("noresults", "Not a Figma document");
  }
  const { browser, page: sourcePage } = await connectToPage({
    chromeSessionDir: path.join(snapshotDir, "chrome"),
    timeoutMs,
  });
  let page = sourcePage;
  const downloads = [];
  let phase = "Checking Figma document";
  let lastClick = null;
  try {
    if (!isDocument(new URL(url)) && !isDocument(new URL(page.url()))) {
      console.error("Not a Figma document");
      return emitArchiveResultRecord("noresults", "Not a Figma document");
    }
    await waitForNavigationComplete(
      path.join(snapshotDir, "chrome"),
      remaining(),
    );
    const current = new URL(page.url());
    if (!isDocument(current)) {
      console.error("Not a Figma document");
      return emitArchiveResultRecord("noresults", "Not a Figma document");
    }
    const blocked = await page.evaluate(() => {
      const text = document.body.innerText;
      if (/403 ERROR|Request blocked/.test(text)) return "403";
      if (document.title === "Human Verification" && text.includes("Let's confirm you are human"))
        return "human-verification";
      return null;
    });
    if (blocked === "403")
      throw new ExportPrerequisiteError("Figma blocked this browser session (HTTP 403)");
    if (blocked === "human-verification")
      throw new ExportPrerequisiteError("Figma requires human verification");
    // Export controls must never modify the shared capture tab.
    phase = "Opening background export page";
    page = await openExportPage({
      page: sourcePage,
      chromeSessionDir: path.join(snapshotDir, "chrome"),
      timeoutMs: remaining(),
    });
    phase = "Loading background Figma document";
    await page.goto(sourcePage.url(), {
      waitUntil: "domcontentloaded",
      timeout: remaining(),
    });
    const click = async (selector) => {
      phase = `Clicking ${selector}`;
      const element = await page.waitForSelector(selector, {
        visible: true,
        timeout: remaining(),
      });
      const box = await element.boundingBox();
      if (!box) throw new Error("Figma export control has no visible bounds");
      // Figma's submenu hover guard covers new items until pointer movement
      // settles. Moving and clicking in the same turn hits that backdrop.
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      const ready = await page.waitForFunction(
        (element) => {
          const control = element.closest('[role="menuitem"]') || element;
          const box = element.getBoundingClientRect();
          const x = box.x + box.width / 2;
          const y = box.y + box.height / 2;
          return control.contains(document.elementFromPoint(x, y)) && {
            x,
            y,
            disabled: control.matches(":disabled, [aria-disabled='true']"),
          };
        },
        { timeout: remaining(), polling: 100 },
        element,
      );
      const { x, y, disabled } = await ready.jsonValue();
      await ready.dispose();
      lastClick = {
        selector,
        disabled,
        elapsedMs: Date.now() - (deadline - timeoutMs),
      };
      await page.mouse.click(x, y);
    };
    const openFileMenu = async () => {
      await click(
        'button[aria-label="Main menu"], [data-testid="toggle-menu-button"]',
      );
      // Figma retains an earlier hidden dialog in the DOM. Select the actual
      // visible popup, not the first matching (possibly hidden) element.
      const menu = await page.waitForFunction(
        () =>
          [...document.querySelectorAll('[role="menu"], [role="dialog"]')].find(
            (el) => {
              const box = el.getBoundingClientRect();
              return (
                box.width > 0 &&
                box.height > 0 &&
                getComputedStyle(el).visibility !== "hidden"
              );
            },
          ),
        { polling: 100, timeout: remaining() },
      );
      if (
        await menu.evaluate(
          (el) =>
            /Sign up|Log in/.test(el.innerText) &&
            !el.innerText.includes("File"),
        )
      )
        throw new ExportPrerequisiteError(
          "Persona must be logged in to figma.com",
        );
      await (
        await page.waitForSelector(
          '::-p-aria([name="File"][role="menuitem"])',
          { visible: true, timeout: remaining() },
        )
      ).hover();
      const submenu = await page.waitForSelector(
        '[role="menu"][id^="mainMenu.file-menu-"]',
        { visible: true, timeout: remaining() },
      );
      await page.waitForFunction(
        (menu) => getComputedStyle(menu).pointerEvents !== "none",
        { timeout: remaining(), polling: 100 },
        submenu,
      );
    };
    await openFileMenu();
    const pdf = /^\/(?:design|file)\//.test(current.pathname)
      ? await page.$('[role="menuitem"]::-p-text(Export frames to PDF)')
      : null;
    const exportPdf = pdf && !(await pdf.evaluate((el) =>
      Boolean(el.closest('[aria-disabled="true"], [disabled]')),
    ));
    phase = "Exporting native Figma document";
    downloads.push(
      ...(await captureBrowserDownloads({
        browser,
        page,
        timeoutMs: remaining(),
        expectedDownloads: exportPdf ? 2 : 1,
        downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
        trigger: async ({ downloadStarted }) => {
          const save = await page.$('[role="menuitem"]::-p-text(Save local copy)');
          if (save && (await save.evaluate((el) =>
            Boolean(el.closest('[aria-disabled="true"], [disabled]')),
          ))) throw new ExportPrerequisiteError("Owner disabled export");
          if (!save) throw new Error(
            "Figma owner has restricted copying/export or Save local copy is unavailable",
          );
          const loginCheck = new AbortController();
          try {
            await Promise.race([
              (async () => {
                await click('[role="menuitem"]::-p-text(Save local copy)');
                if (exportPdf) {
                  // Figma prepares native files asynchronously. Start its PDF
                  // export while that preparation runs, in the same owned tab.
                  await openFileMenu();
                  const item = await page.$('[role="menuitem"]::-p-text(Export frames to PDF)');
                  const control = await item.evaluateHandle((el) => el.closest('[role="menuitem"]'));
                  await control.asElement().focus();
                  await page.keyboard.press("Enter");
                  await click('::-p-aria([name="Export"][role="button"])');
                }
                phase = "Waiting for Figma downloads";
                await downloadStarted;
              })(),
              page.waitForFunction(
                () => {
                  const email = document.querySelector('input[type="email"]');
                  return email?.getBoundingClientRect().width > 0 &&
                    /Sign up for Figma|Log in to Figma/.test(document.body.innerText);
                },
                { polling: 100, timeout: remaining(), signal: loginCheck.signal },
              ).then(() => {
                throw new ExportPrerequisiteError("Persona must be logged in to figma.com");
              }),
            ]);
          } finally {
            loginCheck.abort();
          }
        },
      })),
    );
    phase = "Validating Figma downloads";
    for (const download of downloads) {
      const file = await fs.promises.open(download.filePath, "r");
      const { buffer, bytesRead } = await file
        .read(Buffer.alloc(200), 0, 200, 0)
        .finally(() => file.close());
      const data = buffer.subarray(0, bytesRead);
      if (download.suggestedFilename.endsWith(".pdf")) {
        if (!data.subarray(0, 5).equals(Buffer.from("%PDF-")))
          throw new Error("Invalid Figma PDF export");
      } else if (
        !/\.(fig|jam|deck|buzz|site|make)$/i.test(download.suggestedFilename) ||
        data.length < 8 ||
        /^\s*(?:<!doctype|<html)/i.test(data.subarray(0, 200).toString())
      ) throw new Error("Figma did not return a native editable document");
    }
    if (!downloads.some((item) => /\.(fig|jam|deck|buzz|site|make)$/i.test(item.suggestedFilename)))
      throw new Error("Figma did not return a native editable document");
    if (exportPdf && !downloads.some((item) => item.suggestedFilename.endsWith(".pdf")))
      throw new Error("Figma did not return its PDF export");
    phase = "Saving Figma downloads";
    await saveDownloads(
      path.join(snapshotDir, "figma"),
      await page.title(),
      downloads,
      { timeoutMs: remaining() },
    );
    emitArchiveResultRecord("succeeded", "figma/downloads.json");
  } catch (error) {
    if (!(error instanceof ExportPrerequisiteError)) {
      console.error("Figma last export click:", JSON.stringify(lastClick));
      error.message = `${phase} (${timeoutMs - (deadline - Date.now())}ms elapsed, ${timeoutMs}ms budget): ${error.message}`;
      // Record the primary failure before target teardown can fail separately.
      console.error(error.stack || error.message);
    }
    throw error;
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
    return emitArchiveResultRecord("noresults", error.message);
  }
  console.error(error.stack || error.message);
  emitArchiveResultRecord("failed", error.message);
  process.exitCode = 1;
});
