#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
/**
 * Report CAPTCHA solve counts observed during the current snapshot.
 *
 * Runs as a background script before navigation and watches the live page for
 * solved CAPTCHA response tokens until snapshot shutdown.
 */


// Cleanup can SIGTERM the process immediately after spawn; remember early
// signals and replay them to the hook-specific cleanup handler once it exists.
let __abxEarlyShutdownSignal = null;
function __abxRememberEarlyShutdown(signal) {
  if (__abxEarlyShutdownSignal === null) {
    __abxEarlyShutdownSignal = signal;
  }
}
function __abxInstallShutdownHandler(handler) {
  process.removeAllListeners("SIGTERM");
  process.removeAllListeners("SIGINT");
  process.on("SIGTERM", () => handler("SIGTERM"));
  process.on("SIGINT", () => handler("SIGINT"));
  if (__abxEarlyShutdownSignal !== null) {
    const signal = __abxEarlyShutdownSignal;
    __abxEarlyShutdownSignal = null;
    setImmediate(() => handler(signal));
  }
}
process.on("SIGTERM", () => __abxRememberEarlyShutdown("SIGTERM"));
process.on("SIGINT", () => __abxRememberEarlyShutdown("SIGINT"));

const fs = require("fs");
const path = require("path");

const {
  ensureNodeModuleResolution,
  getEnvBool,
  getEnvInt,
  loadConfig,
  parseArgs,
  emitArchiveResultRecord,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);

const {
  connectToPage,
  waitForNavigationComplete,
} = require("../chrome/chrome_utils.js");

const PLUGIN_DIR = path.basename(__dirname);
const hookConfig = loadConfig();
const SNAP_DIR = path.resolve((hookConfig.SNAP_DIR || ".").trim());
const OUTPUT_DIR = path.join(SNAP_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

const CHROME_SESSION_DIR = path.join(SNAP_DIR, "chrome");

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function formatCaptchaCount(count) {
  return `${count} captcha${count === 1 ? "" : "s"} solved`;
}

let lastProgressLine = "";

function emitProgress(line) {
  if (line && line !== lastProgressLine) {
    lastProgressLine = line;
    console.log(line);
  }
}

async function getCaptchaState(page) {
  return page.evaluate(() => {
    const solved = new Set();
    let detected = false;
    const solverErrors = [];
    const selectors = [
      'textarea[name="g-recaptcha-response"]',
      "textarea#g-recaptcha-response",
      'input[name="g-recaptcha-response"]',
      'textarea[name="h-captcha-response"]',
      'input[name="h-captcha-response"]',
      'textarea[name="cf-turnstile-response"]',
      'input[name="cf-turnstile-response"]',
    ];
    const widgetSelectors = [
      ".g-recaptcha",
      ".grecaptcha-badge",
      ".h-captcha",
      ".cf-turnstile",
      'iframe[src*="recaptcha"]',
      'iframe[src*="hcaptcha"]',
      'iframe[src*="turnstile"]',
    ];

    for (const selector of widgetSelectors) {
      try {
        if (document.querySelector(selector)) {
          detected = true;
          break;
        }
      } catch (error) {}
    }

    for (const selector of selectors) {
      try {
        const elements = document.querySelectorAll(selector);
        for (const el of elements) {
          detected = true;
          const value = typeof el.value === "string" ? el.value.trim() : "";
          if (value.length > 20) {
            solved.add(value.slice(0, 128));
          }
        }
      } catch (error) {}
    }

    for (const solver of document.querySelectorAll(
      '.captcha-solver[data-state="error"]'
    )) {
      const message = solver
        .querySelector(".captcha-solver-info")
        ?.textContent?.trim();
      solverErrors.push(message || "unknown 2Captcha extension error");
    }

    try {
      if (
        typeof grecaptcha !== "undefined" &&
        typeof grecaptcha.getResponse === "function"
      ) {
        detected = true;
        const response = grecaptcha.getResponse();
        if (typeof response === "string" && response.trim().length > 20) {
          solved.add(response.trim().slice(0, 128));
        }
      }
    } catch (error) {}

    return {
      detected,
      solved: solved.size,
      error: solverErrors.length ? solverErrors.join("; ") : null,
    };
  });
}

async function main() {
  const args = parseArgs();
  if (!args.url) {
    console.error("Usage: on_Snapshot__14_twocaptcha.daemon.bg.js --url=<url>");
    process.exit(1);
  }

  if (!getEnvBool("TWOCAPTCHA_ENABLED", true)) {
    emitArchiveResultRecord("skipped", "TWOCAPTCHA_ENABLED=False");
    process.exit(0);
  }

  const apiKey = (hookConfig.TWOCAPTCHA_API_KEY || "").trim();
  if (!apiKey || apiKey === "YOUR_API_KEY_HERE") {
    emitArchiveResultRecord("skipped", "TWOCAPTCHA_API_KEY=None");
    process.exit(0);
  }

  const timeoutMs =
    getEnvInt(
      "TWOCAPTCHA_TIMEOUT",
      getEnvInt("CHROME_TIMEOUT", getEnvInt("TIMEOUT", 60))
    ) * 1000;
  const pollIntervalMs = 2000;

  let browser = null;
  let running = true;
  let exiting = false;
  let solvedCaptchas = 0;

  const emitAndExit = () => {
    if (exiting) {
      return;
    }
    exiting = true;
    const status = solvedCaptchas > 0 ? "succeeded" : "noresults";
    emitArchiveResultRecord(status, formatCaptchaCount(solvedCaptchas));
    if (browser) {
      try {
        browser.disconnect();
      } catch (error) {}
    }
    setImmediate(() => process.exit(0));
  };

  __abxInstallShutdownHandler(() => {
    running = false;
    emitAndExit();
  });

  try {
    const connection = await connectToPage({
      chromeSessionDir: CHROME_SESSION_DIR,
      timeoutMs,
      requireTargetId: true,
    });
    browser = connection.browser;
    const page = connection.page;
    emitProgress("0 captchas detected");

    try {
      // CAPTCHA observation is useful only after navigation, but the observer
      // must be considered ready before chrome_navigate can run. Earlier
      // foreground hooks may delay navigation.json past TWOCAPTCHA_TIMEOUT, so
      // treat that wait as advisory and keep the observer alive until cleanup.
      await waitForNavigationComplete(CHROME_SESSION_DIR, timeoutMs, 0);
    } catch (error) {
      console.error(`WARN: ${error.message}`);
    }

    while (running) {
      let state = null;
      try {
        state = await getCaptchaState(page);
      } catch (error) {
        if (!running) break;
      }
      if (state?.error) {
        throw new Error(`2Captcha extension failed: ${state.error}`);
      }
      if (state) {
        solvedCaptchas = Math.max(solvedCaptchas, state.solved);
        if (solvedCaptchas > 0) {
          emitProgress(formatCaptchaCount(solvedCaptchas));
        } else if (state.detected) {
          emitProgress("1 captcha detected");
        } else {
          emitProgress("0 captchas detected");
        }
      }
      await sleep(pollIntervalMs);
    }
  } catch (error) {
    if (browser) {
      try {
        browser.disconnect();
      } catch (disconnectError) {}
    }
    console.error(`ERROR: ${error.name}: ${error.message}`);
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(`Fatal error: ${error.message}`);
  process.exit(1);
});
