"use strict";

// The direct Anthropic Messages API changed the computer-use wire format.
// https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
const MODEL_ALIASES = new Map([
  ["sonnet", "claude-sonnet-5-5"],
  ["haiku", "claude-haiku-5-5"],
  ["opus", "claude-opus-5-5"],
]);
const TOOLSET_MODELS = new Set([
  ...MODEL_ALIASES.values(),
  "claude-opus-5",
  "claude-opus-4-8",
  "claude-sonnet-5",
]);
const NOVEMBER_MODELS = /^(claude-(?:sonnet-4-6|opus-4-[567])|claude-opus-4-5-20251101)$/;
const CLICK_ACTIONS = [
  "left_click", "right_click", "middle_click", "double_click", "triple_click",
];
const DISABLED_ACTIONS = [
  "zoom",
  "left_mouse_down",
  "left_mouse_up",
  "cursor_position",
  "hold_key",
];

function modelID(value) {
  return MODEL_ALIASES.get(value) || value;
}

function computerProtocol(model, viewport) {
  if (TOOLSET_MODELS.has(model)) {
    return {
      toolset: true,
      fields: {
        tools: [
          {
            type: "computer_toolset_20260801",
            configs: Object.fromEntries(
              DISABLED_ACTIONS.map((action) => [action, { enabled: false }])
            ),
          },
        ],
        tool_choice: { type: "auto", disable_parallel_tool_use: true },
      },
    };
  }
  // Existing explicit IDs retain their family-specific protocol. Anthropic
  // requires November for Sonnet 4.6 and Opus 4.5–4.7, January for Haiku 4.5
  // and legacy Sonnet 4.5.
  const november = NOVEMBER_MODELS.test(model);
  return {
    beta: november ? "computer-use-2025-11-24" : "computer-use-2025-01-24",
    fields: {
      tools: [
        {
          type: november ? "computer_20251124" : "computer_20250124",
          name: "computer",
          display_width_px: viewport.width,
          display_height_px: viewport.height,
          display_number: 1,
        },
      ],
      tool_choice: { type: "auto", disable_parallel_tool_use: true },
    },
  };
}

function computerAction(block, toolset, image, viewport) {
  if (
    toolset &&
    (block.toolset_name !== "computer" || DISABLED_ACTIONS.includes(block.name))
  ) {
    throw new Error("Unsupported computer toolset member");
  }
  if (!toolset && block.name !== "computer") {
    throw new Error("Unsupported legacy computer tool");
  }
  const input = block.input || {};
  const action = { ...input, action: toolset ? block.name : input.action || input.type };
  if (toolset) {
    if (
      [...CLICK_ACTIONS, "left_click_drag", "scroll"].includes(action.action) &&
      input.text
    ) {
      throw new Error("Computer action modifiers are not supported");
    }
    if (CLICK_ACTIONS.includes(action.action) && input.coordinate === undefined) {
      throw new Error("Browser click requires a screenshot coordinate");
    }
    for (const name of ["coordinate", "start_coordinate"]) {
      if (input[name] === undefined) continue;
      const point = input[name];
      if (
        !Array.isArray(point) ||
        point.length !== 2 ||
        !point.every(Number.isFinite) ||
        point[0] < 0 ||
        point[1] < 0 ||
        point[0] >= image.width ||
        point[1] >= image.height
      ) {
        throw new Error("Computer action coordinate is outside the screenshot");
      }
      action[name] = [
        (point[0] * viewport.width) / image.width,
        (point[1] * viewport.height) / image.height,
      ];
    }
  }
  return action;
}

const TOOL_ERRORS = new Map([
  ["halted", "Not executed: an earlier computer action in this turn failed."],
  ["one-action", "Not executed: this integration accepts one computer action per turn."],
  ["budget", "Computer action budget exhausted."],
  ["failed", "The computer action could not be completed."],
]);

function toolResult(block, toolset, screenshot, error) {
  return {
    type: "tool_result",
    tool_use_id: block.id,
    ...(toolset ? { toolset_name: "computer" } : {}),
    ...(error
      ? { is_error: true, content: TOOL_ERRORS.get(error) || TOOL_ERRORS.get("failed") }
      : {
          content: [
            {
              type: "image",
              source: { type: "base64", media_type: "image/png", data: screenshot },
            },
          ],
        }),
  };
}

function appendComputerResults(messages, assistantContent, results) {
  if (!results.length) return;
  // Keep signed thinking blocks and the original tool blocks intact, then
  // reply to every tool_use id in one immediately following user message.
  messages.push(
    { role: "assistant", content: assistantContent },
    { role: "user", content: results }
  );
}

function screenshotOptions(viewport, deviceScaleFactor) {
  // Stay below the stricter per-side wire limit for long image histories,
  // even when Chrome captures HiDPI devices at a higher native pixel count.
  const scale = Math.min(
    1,
    1568 / (Math.max(viewport.width, viewport.height) * deviceScaleFactor)
  );
  return scale < 1
    ? {
        captureBeyondViewport: true,
        clip: { x: 0, y: 0, ...viewport, scale },
      }
    : {};
}

async function captureViewportScreenshot(page, captureOptions, timeoutMs, attempts = 1) {
  let lastError;
  for (let attempt = 0; attempt < attempts; attempt++) {
    const attemptDeadline = Date.now() + timeoutMs;
    let options = captureOptions;
    if (options?.clip) {
      const { x, y } = await page.evaluate(() => ({
        x: window.visualViewport?.pageLeft ?? window.scrollX,
        y: window.visualViewport?.pageTop ?? window.scrollY,
      }));
      options = { ...options, clip: { ...options.clip, x, y } };
    }

    // Detach the timed-out request before starting the next screenshot.
    const session = await page.createCDPSession();
    try {
      const { data } = await session.send("Page.captureScreenshot", {
        format: "png",
        fromSurface: true,
        captureBeyondViewport: false,
        ...options,
      }, { timeout: Math.max(1, attemptDeadline - Date.now()) });
      return data;
    } catch (error) {
      lastError = error;
    } finally {
      if (!session.detached) await session.detach();
    }
    if (attempt + 1 < attempts) await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw lastError;
}

function pngDimensions(screenshot) {
  // PNG stores width and height in the mandatory first IHDR chunk.
  const header = Buffer.from(screenshot.slice(0, 32), "base64");
  return { width: header.readUInt32BE(16), height: header.readUInt32BE(20) };
}

module.exports = {
  appendComputerResults,
  captureViewportScreenshot,
  computerAction,
  computerProtocol,
  modelID,
  pngDimensions,
  screenshotOptions,
  toolResult,
};
