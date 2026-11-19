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
const { clickReadyOption } = require("./click_option.js");

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
    const observeNavigation = (target, kind) => {
      target.on("response", (response) => {
        const request = response.request();
        if (!request.isNavigationRequest() || request.frame() !== target.mainFrame()) return;
        console.error(`Canva ${kind} document response:`, JSON.stringify({
          status: response.status(),
          challenge: response.headers()["cf-mitigated"] === "challenge",
        }));
      });
      target.on("domcontentloaded", () => console.error(`Canva ${kind} DOM loaded`));
      target.on("load", () => console.error(`Canva ${kind} load complete`));
    };
    observeNavigation(sourcePage, "source");
    // Export controls must never modify the shared capture tab.
    page = await openExportPage({
      page: sourcePage,
      chromeSessionDir: path.join(snapshotDir, "chrome"),
      timeoutMs: remaining(),
    });
    observeNavigation(page, "export");
    const readinessPrefix = "ArchiveBox Canva readiness: ";
    page.on("console", (message) => {
      if (message.type() === "debug" && message.text().startsWith(readinessPrefix))
        console.error("Canva export readiness:", message.text().slice(readinessPrefix.length));
    });
    await page.evaluateOnNewDocument((prefix) => {
      let previous = "";
      let scheduled = false;
      const report = () => {
        scheduled = false;
        const state = {
          frame: window === top ? "main" : "child",
          readyState: document.readyState,
          blocked: /We.ll have you designing again soon|RayID:|verify you are human/i.test(document.body?.textContent || ""),
          titleState: !document.title ? "empty" : document.title === "Canva" ? "canva" : /just a moment|attention required/i.test(document.title) ? "challenge" : "document",
          controls: [...document.querySelectorAll('button,[role="menuitem"]')]
            .map((element) => ({element, label: element.getAttribute("aria-label") || element.textContent.trim()}))
            .filter(({label}) => ["Share", "All Publish Options", "Download", "Log in", "Sign in", "Continue with Google", "Continue with email"].includes(label))
            .slice(0, 20)
            .map(({element, label}) => {
              const bounds = element.getBoundingClientRect();
              return {label, role: element.getAttribute("role"), tag: element.tagName,
                disabled: Boolean(element.disabled) || element.getAttribute("aria-disabled") === "true",
                visible: bounds.width > 0 && bounds.height > 0,
                exposed: element.contains(document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2))};
            }),
        };
        const signature = JSON.stringify(state);
        if (signature !== previous) {
          previous = signature;
          console.debug(prefix + signature);
        }
      };
      // Capture changes before an outer hook deadline can terminate diagnostics.
      // Only fixed known control labels and title classes leave the browser.
      new MutationObserver(() => {
        if (!scheduled) { scheduled = true; setTimeout(report, 100); }
      }).observe(document, {subtree: true, childList: true, attributes: true,
        attributeFilter: ["role", "aria-label", "aria-disabled", "disabled", "class", "style"]});
      window.addEventListener("load", report, {once: true});
      report();
    }, readinessPrefix);
    const exportResponse = await page.goto(sourcePage.url(), {
      waitUntil: "domcontentloaded",
      timeout: remaining(),
    });
    console.error("Canva export navigation:", JSON.stringify({
      status: exportResponse?.status(),
      path: new URL(page.url()).pathname,
      title: (await page.title()).slice(0, 200),
    }));
    const reportControls = async (frame, selector, target = null) => {
      try {
        const state = await frame.evaluate((selector, target) => {
          const control = document.querySelector(
            '[role="combobox"][aria-label="File type"]',
          );
          return {
            title: document.title,
            path: location.pathname,
            readyState: document.readyState,
            text: document.body?.innerText.slice(0, 1000),
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
        console.error("Canva export controls:", JSON.stringify({
          navigationStatus: exportResponse?.status(),
          ...state,
        }));
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
      // The outer hook deadline can kill us before diagnostics or tab cleanup
      // finish. Preserve the awaited control and original error before either
      // operation touches a possibly destroyed execution context.
      console.error(`Waiting for Canva export control: ${role} ${name}`);
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
        .catch(async (error) => {
          console.error(
            `Canva export control unavailable: ${role} ${name}: ${error.stack || error.message}`,
          );
          await reportControls(frame, selector);
          throw new Error(`Canva export control unavailable: ${role} ${name}`);
        });
      const element = handle.asElement();
      try {
        // The export tab can stop painting under Xvfb even with focus emulation.
        // Readiness and scrolling above must not be followed by Locator's RAF /
        // IntersectionObserver waits. Keep a trusted click at the native point,
        // which also accounts for controls inside the template iframe.
        if (role === "option" && frame === page) {
          await clickReadyOption(page, element, remaining);
        } else {
          const { x, y } = await element.clickablePoint();
          await page.mouse.click(x, y);
        }
      } catch (error) {
        console.error(
          `Canva export click failed: ${role} ${name}: ${error.stack || error.message}`,
        );
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
      // Like the controls above, this frame can appear in an export tab that
      // is not painting under Xvfb. Visible-selector waits poll with RAF;
      // use the same timer polling while retaining the visibility check.
      const iframe = await page.waitForFunction(
        () => {
          const element = document.querySelector('iframe[title="Open template"]');
          // Canva also opens template sign-in in the top-level document.
          // That is the same prerequisite, not a missing iframe to wait out.
          if (!element) return /Continue with Google|Continue with email|We.ll have you designing again soon|RayID:|verify you are human/i.test(document.body.innerText);
          const visibility = getComputedStyle(element).visibility;
          const bounds = element.getBoundingClientRect();
          return visibility !== "hidden" && visibility !== "collapse" &&
            bounds.width > 0 && bounds.height > 0 ? element : false;
        },
        { polling: 100, timeout: remaining() },
      ).catch(async (error) => {
        await reportControls(page, "iframe");
        throw error;
      });
      const frame = iframe.asElement() ? await iframe.asElement().contentFrame() : page;
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
    // Download mounts a provisional form before its export settings are ready.
    // Its submit button is aria-disabled during that work; interacting with the
    // enabled format control then can lose the menu when the form is replaced.
    // Resolve the format control only from a ready form, using the same deadline.
    const fileTypeSelector = 'form:has(button[type="submit"]:not(:disabled):not([aria-disabled="true"])) [role="combobox"]';
    // Compact layouts label the publish menu differently and also expose a
    // separate Share control that only changes collaboration permissions.
    await click(
      "menuitem",
      ["Share", "All Publish Options"],
      page,
      '[role="menuitem"]:not([aria-label="Share"])',
    );
    await click("button", "Download");
    await click("combobox", "File type", page, fileTypeSelector);
    console.error("Waiting for Canva export options");
    await page
      .waitForSelector('[role="option"]', {
        visible: true,
        timeout: remaining(),
      })
      .catch(async (error) => {
        console.error(
          `Canva export options unavailable: ${error.stack || error.message}`,
        );
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
        await click("combobox", "File type", page, fileTypeSelector);
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
