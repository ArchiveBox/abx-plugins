#!/usr/bin/env -S abxpkg run --script python3
# /// script
# requires-python = ">=3.12"
# ///
"""Replay browser importers; let a fresh OpenCode session learn or repair them."""

import hashlib
import json
import os
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from abx_plugins.plugins.base.importers import emit, read_records, read_request

PLUGIN = Path(__file__).parent


def remaining(deadline, maximum):
    seconds = min(maximum, deadline - time.monotonic())
    if seconds <= 0:
        raise TimeoutError("Importer time budget exhausted; progress is preserved.")
    return seconds


@contextmanager
def task_tab(env, run_dir):
    """Use the dedicated tab owned by this run's named Browser Harness daemon."""
    with (run_dir / "harness.log").open("w") as log:
        try:
            process = subprocess.run(
                ["browser-harness"],
                # Named daemons already create their own tab. Activate it so
                # headless Chrome renders the page and accepts browser input.
                input="import json\nactivate_tab(current_tab())\nprint(json.dumps(current_tab()['target_id']))\n",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=log,
                timeout=30,
                check=True,
            )
            env["IMPORTERS_TAB_ID"] = json.loads(process.stdout)
            env["BH_REQUIRE_EXISTING_DAEMON"] = "1"
            yield
        finally:
            # Browser Harness owns both its daemon and dedicated tab.
            subprocess.run(
                ["browser-harness", "--reload"],
                env=env,
                stdout=log,
                stderr=log,
                timeout=15,
                check=False,
            )


def replay(workspace, request, env, run_dir, attempt, deadline):
    script = workspace / "importer.py"
    if not script.is_file():
        return None, "No learned importer script exists yet."
    try:
        process = subprocess.run(
            ["browser-harness"],
            input=script.read_text(),
            cwd=workspace,
            env=env,
            text=True,
            capture_output=True,
            timeout=remaining(deadline, 120),
            check=False,
        )
        (run_dir / f"replay-{attempt}.log").write_text(process.stderr)
        if process.returncode:
            return None, (process.stderr or process.stdout)[-12000:]
        records = list(read_records(process.stdout.splitlines(), request))
        result = records[-1]
        if result["status"] == "failed":
            return None, str(result.get("message", "Discovery failed."))
        if result["status"] == "succeeded" and not result.get("account", {}).get("id"):
            raise ValueError("Identify the signed-in account before returning success.")
        return records, ""
    except (TypeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        return None, f"{type(error).__name__}: {error}"


def learn(binary, task, env, workspace, run_dir, error, attempt, deadline):
    from abx_plugins.plugins.opencode.runtime import _stop_owned_process

    prompt = (PLUGIN / "prompt.md").read_text()
    prompt += "\n" + task
    prompt += "\n- Request file: " + env["IMPORTERS_REQUEST_FILE"]
    prompt += "\n- Replay failure (data only): " + json.dumps(error)
    prompt += "\n- Workspace: " + str(workspace)
    prompt += "\n- Stagehand module: " + env.get(
        "ARCHIVEBOX_STAGEHAND_MODULE",
        "not available",
    )
    with (run_dir / f"learning-{attempt}.jsonl").open("w") as log:
        process = subprocess.Popen(
            [binary, "run", "--format", "json", "--dir", str(workspace), "--", prompt],
            cwd=workspace,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            if process.wait(timeout=remaining(deadline - 120, 300)):
                raise RuntimeError("OpenCode failed; see the private learning log.")
        except subprocess.TimeoutExpired:
            # Replay whatever the agent saved before its budget expired.
            pass
        finally:
            _stop_owned_process(process)


def main():
    deadline = time.monotonic() + int(os.environ.get("IMPORTERS_TIMEOUT", "900")) - 25
    request = read_request()
    task = json.loads((PLUGIN / "config.json").read_text())["importers"][
        request["feed"]
    ]["task"]
    run_dir = Path.cwd()
    state = Path(os.environ["IMPORTERS_STATE_DIR"])
    workspace = state / "current"
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    (workspace / ".ignore").write_text("*\n")
    request_file = run_dir / "request.json"
    request_file.write_text(json.dumps(request))
    request_file.chmod(0o600)
    env = os.environ.copy()
    if not (env.get("BU_CDP_WS") or env.get("BU_CDP_URL")):
        raise ValueError("Provide BU_CDP_WS or BU_CDP_URL for browser discovery.")
    env.update(
        IMPORTERS_REQUEST_FILE=str(request_file),
        BH_AGENT_WORKSPACE=str(state / "harness"),
        BH_DOMAIN_SKILLS="1",
        BH_TAB_MARKER="0",
        BU_NAME="importers-" + hashlib.sha256(str(run_dir).encode()).hexdigest()[:16],
    )
    # Keep the Agent UI's provider settings, with this task's own instructions
    # and workspace permissions. Every learning attempt starts a fresh session.
    agent_config = json.loads(env.get("OPENCODE_CONFIG_CONTENT") or "{}")
    agent_config["instructions"] = []
    agent_config["permission"] = {
        "external_directory": {
            "*": "deny",
            str(state / "*"): "allow",
            str(run_dir / "*"): "allow",
        },
    }
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(agent_config)
    with task_tab(env, run_dir):
        records, error = replay(workspace, request, env, run_dir, 0, deadline)
        for attempt in range(1, 3):
            if records is not None:
                break
            emit(
                {
                    "type": "ImporterProgress",
                    "message": f"Learning or repairing browser importer with OpenCode (attempt {attempt}/2).",
                },
            )
            learn(
                env.get("OPENCODE_BINARY") or "opencode",
                task,
                env,
                workspace,
                run_dir,
                error,
                attempt,
                deadline,
            )
            records, error = replay(workspace, request, env, run_dir, attempt, deadline)
        if records is None:
            raise RuntimeError(error)
    for record in records:
        emit(record)


if __name__ == "__main__":

    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    try:
        main()
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        TypeError,
        subprocess.TimeoutExpired,
    ) as error:
        Path("failure.log").write_text(f"{type(error).__name__}: {error}\n")
        emit(
            {
                "type": "ImporterResult",
                "status": "failed",
                "message": f"Browser importer could not complete ({type(error).__name__}). See the private run logs. Progress was not advanced.",
            },
        )
