// Run one bounded OpenCode task with an explicit completion tool. No host imports.
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

async function runTask({
  prompt,
  model,
  timeoutMs,
  env = process.env,
  cwd,
  outputDir,
}) {
  if (!model || !model.includes("/"))
    throw new Error("An agent model in provider/model format is required");
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "abx-opencode-"));
  const toolsDir = path.join(workspace, ".opencode", "tools");
  fs.mkdirSync(toolsDir, { recursive: true });
  fs.copyFileSync(
    path.join(__dirname, "done.js"),
    path.join(toolsDir, "done.js"),
  );
  const config = JSON.parse(env.OPENCODE_CONFIG_CONTENT || "{}");
  Object.assign(config, {
    snapshot: false,
    instructions: [],
    permission: { skill: "deny", bash: "allow", done: "allow" },
  });
  if (outputDir)
    config.permission.external_directory = {
      [outputDir]: "allow",
      [path.join(outputDir, "**")]: "allow",
    };
  const childEnv = { ...env, OPENCODE_CONFIG_CONTENT: JSON.stringify(config) };
  let child,
    timer,
    forceTimer,
    stopping = false;
  const stop = () => {
    if (
      child &&
      !stopping &&
      child.exitCode === null &&
      child.signalCode === null
    ) {
      stopping = true;
      try {
        process.kill(-child.pid, "SIGTERM");
      } catch (error) {
        if (error.code !== "ESRCH") throw error;
      }
      forceTimer = setTimeout(() => {
        try {
          process.kill(-child.pid, "SIGKILL");
        } catch (error) {
          if (error.code !== "ESRCH") throw error;
        }
      }, 5000);
    }
  };
  process.once("SIGTERM", stop);
  const deadline = Date.now() + timeoutMs;
  const messages = [];
  let sessionId;
  let turns = 0;
  try {
    while (Date.now() < deadline) {
      stopping = false;
      turns += 1;
      const finished = await new Promise((resolve, reject) => {
        child = spawn(
          childEnv.OPENCODE_BINARY || "opencode",
          [
            "run",
            "--pure",
            "--format=json",
            `--model=${model}`,
            ...(sessionId ? ["--session", sessionId] : []),
          ],
          {
            cwd: cwd || workspace,
            env: {
              ...childEnv,
              OPENCODE_CONFIG_DIR: path.join(workspace, ".opencode"),
            },
            detached: true,
            stdio: ["pipe", "pipe", "pipe"],
          },
        );
        let buffer = "",
          stderr = "",
          finished = false,
          failure = null;
        child.stdout.setEncoding("utf8");
        child.stderr.setEncoding("utf8");
        child.stderr.on("data", (data) => {
          stderr = (stderr + data).slice(-4000);
        });
        child.stdout.on("data", (data) => {
          buffer += data;
          let newline;
          while ((newline = buffer.indexOf("\n")) !== -1) {
            const line = buffer.slice(0, newline);
            buffer = buffer.slice(newline + 1);
            if (!line.trim()) continue;
            try {
              const event = JSON.parse(line);
              if (event.sessionID) sessionId = event.sessionID;
              if (outputDir)
                fs.appendFileSync(
                  path.join(outputDir, "agent.log"),
                  line + "\n",
                  { mode: 0o600 },
                );
              if (event.type === "text") messages.push(event.part?.text || "");
              if (event.type === "error") {
                failure = new Error(
                  "OpenCode provider error: " +
                    (event.error?.data?.message ||
                      event.error?.name ||
                      "unknown"),
                );
                stop();
              }
              if (
                event.type === "tool_use" &&
                event.part?.tool === "done" &&
                event.part.state?.status === "completed"
              ) {
                finished = true;
                stop();
              }
            } catch (error) {
              failure = error;
              stop();
            }
          }
        });
        child.on("error", reject);
        child.on("close", (code) => {
          if (failure) reject(failure);
          else if (finished) resolve(true);
          else if (code === 0 && sessionId) resolve(false);
          else
            reject(
              new Error(
                `OpenCode exited (${code}) without calling done. ${stderr}`,
              ),
            );
        });
        timer = setTimeout(
          () => {
            failure = new Error("OpenCode task timed out before done");
            stop();
          },
          Math.max(1, deadline - Date.now()),
        );
        child.stdin.end(
          prompt +
            "\n\nWhen the requested task is complete, summarize the result and call the done tool. If you are unable to complete it, explain why before calling done. Do not end without calling done.",
        );
      });
      clearTimeout(timer);
      clearTimeout(forceTimer);
      if (finished) return { done: true, summary: messages.join("\n"), turns };
      // A model turn ending is not task completion. Continue this same session
      // until its done tool fires, within the original task deadline.
      prompt =
        "Continue the same task from the current page state. If your work is complete, call the done tool now. Otherwise finish the remaining work. Do not repeat completed actions or reload the page.";
    }
    throw new Error("OpenCode task timed out before done");
  } finally {
    clearTimeout(timer);
    clearTimeout(forceTimer);
    process.removeListener("SIGTERM", stop);
    stop();
    clearTimeout(forceTimer);
    fs.rmSync(workspace, { recursive: true, force: true });
  }
}
module.exports = { runTask };
if (require.main === module) {
  runTask(JSON.parse(fs.readFileSync(0, "utf8")))
    .then((result) => {
      console.log(JSON.stringify(result));
    })
    .catch((error) => {
      console.error(error.message);
      process.exitCode = 1;
    });
}
