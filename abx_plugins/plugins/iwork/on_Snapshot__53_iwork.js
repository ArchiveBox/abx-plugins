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

async function main() {
  const config = loadConfig();
  const { url } = parseArgs();
  if (!url) throw new Error("Missing --url");
  if (!getEnvBool("IWORK_ENABLED", true)) {
    console.error("IWORK_ENABLED=False");
    return emitArchiveResultRecord("skipped", "IWORK_ENABLED=False");
  }
  const timeoutMs = getEnvInt("IWORK_TIMEOUT", 120) * 1000;
  const deadline = Date.now() + timeoutMs;
  const remaining = () => {
    const ms = deadline - Date.now();
    if (ms <= 0) throw new Error("Document export deadline exceeded");
    return ms;
  };
  const snapshotDir = path.resolve(config.SNAP_DIR || ".");
  const chromeSessionDir = path.join(snapshotDir, "chrome");
  const { browser, page: sourcePage } = await connectToPage({
    chromeSessionDir,
    timeoutMs,
  });
  let page = sourcePage;
  let downloads = [];
  try {
    const candidate = (value) =>
      value.protocol === "https:" &&
      value.hostname === "www.icloud.com" &&
      /^\/(keynote|pages|numbers)\/[\w-]+\/?$/.test(value.pathname);
    if (!candidate(new URL(url)) && !candidate(new URL(page.url()))) {
      console.error("Not a public iWork document");
      return emitArchiveResultRecord(
        "noresults",
        "Not a public iWork document",
      );
    }
    await waitForNavigationComplete(chromeSessionDir, remaining());
    const current = new URL(page.url());
    const documentMatch = current.pathname.match(
      /^\/(keynote|pages|numbers)\/[\w-]+\/?$/,
    );
    if (
      current.protocol !== "https:" ||
      current.hostname !== "www.icloud.com" ||
      !documentMatch
    ) {
      console.error("Not a public iWork document");
      return emitArchiveResultRecord(
        "noresults",
        "Not a public iWork document",
      );
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
    const title = await page.title();
    const application =
      documentMatch[1][0].toUpperCase() + documentMatch[1].slice(1);
    const tools = `ui-button[title="View ${application} tools."]`;
    downloads = await captureBrowserDownloads({
      browser,
      page,
      timeoutMs: remaining(),
      downloadPath: resolveChromeLaunchOptions(config).CHROME_DOWNLOADS_DIR,
      trigger: async () => {
        let previous = "";
        const completed = [];
        while (true) {
          // Apple layers onboarding, participant panes, and export controls.
          // Wait for the actual topmost clickable control, not a hidden or
          // covered copy of a label left behind during a dialog transition.
          const handle = await page.waitForFunction(
            ({ previous, completed, tools }) => {
              const hit = (element) => {
                if (!element) return false;
                const rect = element.getBoundingClientRect();
                if (!rect.width || !rect.height) return false;
                const top = document.elementFromPoint(
                  rect.x + rect.width / 2,
                  rect.y + rect.height / 2,
                );
                return top && element.contains(top);
              };
              const candidates = [
                [
                  "intro",
                  document.querySelector(".iw-first-launch-continue-button"),
                ],
                ["guest", document.querySelector("#participant-name")],
                ...Array.from(
                  document.querySelectorAll("ui-alert-actions ui-button"),
                ).map((el) => [
                  el.textContent.trim() === "Continue" ? "collaboration" : "",
                  el,
                ]),
                ...Array.from(
                  document.querySelectorAll(
                    'ui-pane-backdrop[role="button"][aria-label="Close pane"]',
                  ),
                ).map((el) => [
                  document.body.innerText.includes("Current Participants")
                    ? "participants"
                    : "",
                  el,
                ]),
                ...Array.from(
                  document.querySelectorAll('ui-popup[role="dialog"] button'),
                ).map((el) => [
                  el.textContent.trim() === "PDF" ? "pdf" : "",
                  el,
                ]),
                ...Array.from(
                  document.querySelectorAll('ui-menu-item[role="menuitem"]'),
                ).map((el) => [
                  el.textContent.trim() === "Download a Copy…" ? "menu" : "",
                  el,
                ]),
                ["tools", document.querySelector(tools)],
              ];
              for (const [state, element] of candidates)
                if (
                  state &&
                  state !== previous &&
                  !completed.includes(state) &&
                  hit(element)
                )
                  return { state, element };
              return false;
            },
            { timeout: remaining() },
            { previous, completed, tools },
          );
          const state = await (await handle.getProperty("state")).jsonValue();
          const element = (await handle.getProperty("element")).asElement();
          if (state === "guest") {
            await page.waitForSelector("#participant-name", {
              visible: true,
              timeout: remaining(),
            });
            await page.type("#participant-name", "ArchiveBox");
          }
          if (state === "guest") {
            const join = await page.waitForSelector(
              'ui-alert-actions ui-button[aria-disabled="false"]',
              { visible: true, timeout: remaining() },
            );
            await join.asLocator().setTimeout(remaining()).click();
          } else if (state === "intro") {
            await element.focus();
            await page.keyboard.press("Enter");
          } else await element.asLocator().setTimeout(remaining()).click();
          await handle.dispose();
          if (state === "pdf") return;
          if (
            ["intro", "guest", "collaboration", "participants"].includes(state)
          )
            completed.push(state);
          previous = state;
        }
      },
    });
    await saveDownloads(path.join(snapshotDir, "iwork"), title, downloads, {
      timeoutMs: remaining(),
    });
    emitArchiveResultRecord("succeeded", "iwork/downloads.json");
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
  console.error(error.message);
  emitArchiveResultRecord("failed", error.message);
  process.exitCode = 1;
});
