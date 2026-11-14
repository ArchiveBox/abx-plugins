#!/usr/bin/env -S abxpkg run --script python3
# /// script
# requires-python = ">=3.12"
# ///
"""Replay browser importers; let a fresh OpenCode session learn or repair them."""

import json
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from abx_plugins.plugins.base.importers import emit, read_request
from abx_plugins.plugins.browserharness.runtime import (
    capture_failure,
    importer_environment,
    remaining,
    replay,
    task_tab,
)

PLUGIN = Path(__file__).parent


@contextmanager
def connection(run_dir):
    from abx_plugins.plugins.base.agents import agent_environment
    from abx_plugins.plugins.base.utils import load_config
    from abx_plugins.plugins.opencode import runtime

    config = load_config(PLUGIN / "config.json", hydrate_binaries=False).model_dump(
        mode="json",
    )
    settings = runtime._settings(config)
    binary, env = runtime._resolve_binary(settings["binary"], config)
    owner = None
    log = None
    try:
        endpoint = os.environ.get("BU_CDP_WS") or os.environ.get("BU_CDP_URL")
        if not endpoint:
            name, directory = config.get("ACTIVE_PERSONA"), config.get("PERSONAS_DIR")
            if not name or not directory:
                raise ValueError(
                    "Provide BU_CDP_WS/BU_CDP_URL or ACTIVE_PERSONA and PERSONAS_DIR for browser discovery.",
                )
            chrome = PLUGIN.parent / "chrome"
            command = json.loads((chrome / "config.json").read_text())["commands"][
                "browser-open"
            ]
            log = (run_dir / "browser.log").open("w")
            owner = subprocess.Popen(
                [sys.executable, str(chrome / command[0]), *command[1:]],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                env=os.environ.copy(),
            )
            assert owner.stdin is not None and owner.stdout is not None
            owner.stdin.write(
                json.dumps(
                    {
                        "config": config,
                        "persona_dir": str(Path(directory) / name),
                        "name": name,
                        "headless": True,
                    },
                ),
            )
            owner.stdin.close()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if select.select(
                    [owner.stdout],
                    [],
                    [],
                    max(0, min(1, deadline - time.monotonic())),
                )[0]:
                    line = owner.stdout.readline()
                    if line:
                        ready = json.loads(line)
                        if ready.get("ready"):
                            endpoint = ready["cdp_url"]
                            break
                if owner.poll() is not None:
                    break
            if not endpoint:
                raise RuntimeError(
                    "Persona browser did not become ready; see this run's browser.log.",
                )
        env = agent_environment("browser", endpoint, config=config, environ=env)
        binary_path = binary.loaded_abspath
        if binary.loaded_binprovider is not None:
            binary_path = binary.loaded_binprovider._exec_bin_abspath(Path(binary_path))
        env = runtime.process_environment(settings, env)
        env["OPENCODE_BINARY"] = str(binary_path)
        yield env
    finally:
        if owner is not None and owner.poll() is None:
            owner.terminate()
            try:
                owner.wait(timeout=15)
            except subprocess.TimeoutExpired:
                owner.kill()
                owner.wait(timeout=5)
        if log:
            log.close()


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
        except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
            capture_failure(env, run_dir, attempt)
            raise
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
    with connection(run_dir) as env:
        env.update(importer_environment(state, run_dir, request_file))
        # Keep the Agent UI's provider settings, with this task's own instructions
        # and workspace permissions. Every learning attempt starts a fresh session.
        agent_config = json.loads(env.get("OPENCODE_CONFIG_CONTENT") or "{}")
        agent_config["instructions"] = []
        agent_config["permission"] = {
            # Global Agent UI skills assume a collection cwd and own browser setup.
            # This standalone task already has its connection and Harness knowledge.
            "skill": "deny",
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
                    deadline - (120 if request["action"] == "import" else 0),
                )
                records, error = replay(
                    workspace,
                    request,
                    env,
                    run_dir,
                    attempt,
                    deadline,
                )
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
