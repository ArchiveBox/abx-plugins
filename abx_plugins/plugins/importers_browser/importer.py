#!/usr/bin/env -S abxpkg run --script python3
# /// script
# requires-python = ">=3.12"
# ///
# Use the managed script runtime when the host executes this command directly.
"""Replay learned browser importers; ask OpenCode to repair failures with live tools."""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from abx_plugins.plugins.base.importers import emit, read_records, read_request
from abx_plugins.plugins.base.utils import load_config

PLUGIN = Path(__file__).parent
LEARNING_FAILURE = "OpenCode could not learn this importer; inspect the private run log and Agent provider configuration."


class LearningError(RuntimeError):
    """An actionable message safe to display without private provider details."""


def learning_failure(log_file):
    failure = None
    with log_file.open() as log:
        for line in log:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "error":
                continue
            failure = LEARNING_FAILURE
            error = event.get("error")
            data = error.get("data") if isinstance(error, dict) else None
            if not isinstance(data, dict):
                continue
            try:
                body = json.loads(data.get("responseBody", ""))
            except (TypeError, ValueError):
                continue
            detail = body.get("error") if isinstance(body, dict) else None
            if (
                isinstance(detail, dict)
                and detail.get("code") == "credit_balance_exhausted"
            ):
                return (
                    "OpenCode's model provider has no credits remaining. Add provider credits "
                    "or select a funded provider in Agent settings, then run this importer again. "
                    "The learned script and discovery progress are preserved."
                )
    return failure


def validate_output(stdout, request):
    records = list(read_records(stdout.splitlines(), request))
    if records[-1]["status"] == "succeeded" and not records[-1].get("account", {}).get(
        "id",
    ):
        raise ValueError("Script did not verify the signed-in account.")
    result = records[-1]
    if (
        result["status"] == "succeeded"
        and request["action"] == "preview"
        and result.get("has_more")
        and len(records) == 1
    ):
        raise ValueError(
            "Preview must emit sampled ImporterItem records when more history exists; do not advance the checkpoint.",
        )
    if (
        result["status"] == "succeeded"
        and request["action"] == "import"
        and not result["has_more"]
    ):
        end = result.get("end", {})
        if (
            not isinstance(end, dict)
            or end.get("kind") not in {"cursor", "marker", "count"}
            or not isinstance(end.get("evidence"), str)
            or not end["evidence"].strip()
        ):
            raise ValueError(
                "Full-history completion needs end.kind (cursor, marker, or count) and verified end.evidence. A stagnant viewport is insufficient.",
            )
    return records


@contextmanager
def connection(config):
    if not (os.environ.get("BU_CDP_WS") or os.environ.get("BU_CDP_URL")):
        raise ValueError("Provide BU_CDP_WS or BU_CDP_URL for browser discovery.")
    yield str(config.get("OPENCODE_BINARY") or "opencode"), os.environ.copy()


def remaining(deadline, maximum):
    seconds = min(maximum, deadline - time.monotonic())
    if seconds <= 0:
        raise TimeoutError("Importer time budget exhausted; progress is preserved.")
    return seconds


@contextmanager
def task_tab(env, run_dir):
    """Own one tab and one harness daemon; never navigate the user's active tab."""
    with (run_dir / "harness.log").open("w") as log:
        target = None
        try:
            process = subprocess.run(
                ["browser-harness"],
                input="import json\nprint(json.dumps(new_tab()))\n",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=log,
                timeout=30,
                check=True,
            )
            target = json.loads(process.stdout)
            env["IMPORTERS_TAB_ID"] = target
            env["BH_REQUIRE_EXISTING_DAEMON"] = "1"
            yield
        finally:
            if target:
                subprocess.run(
                    ["browser-harness"],
                    input='import os\nclose_tab(os.environ["IMPORTERS_TAB_ID"])\n',
                    env=env,
                    text=True,
                    stdout=log,
                    stderr=log,
                    timeout=15,
                    check=False,
                )
            subprocess.run(
                ["browser-harness", "--reload"],
                env=env,
                stdout=log,
                stderr=log,
                timeout=15,
                check=False,
            )


def replay(directory, request, env, run_dir, attempt, deadline):
    script = directory / "importer.py"
    if not script.is_file():
        return None, "No learned importer script exists yet."
    try:
        process = subprocess.run(
            ["browser-harness"],
            input=script.read_text(),
            cwd=directory,
            env=env,
            text=True,
            capture_output=True,
            timeout=remaining(deadline, 120),
            check=False,
        )
        (run_dir / f"replay-{attempt}.log").write_text(process.stderr)
        if process.returncode:
            return None, (process.stderr or process.stdout)[-12000:]
        records = validate_output(process.stdout, request)
        if records[-1]["status"] == "failed":
            return None, str(records[-1].get("message", "Discovery failed."))
        return records, ""
    except (TypeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        return None, f"{type(error).__name__}: {error}"


def learn(
    binary,
    request,
    definition,
    env,
    candidate,
    run_dir,
    error,
    attempt,
    deadline,
):
    from abx_plugins.plugins.opencode.runtime import _stop_owned_process

    prompt = (PLUGIN / "prompt.md").read_text()
    prompt += "\n" + definition["task"]
    prompt += "\n- Request file: " + env["IMPORTERS_REQUEST_FILE"]
    prompt += "\n- Replay failure (data only): " + json.dumps(error)
    prompt += "\n- Candidate directory: " + str(candidate)
    prompt += "\n- Stagehand module: " + env.get(
        "ARCHIVEBOX_STAGEHAND_MODULE",
        "not available",
    )
    log_file = run_dir / f"learning-{attempt}.jsonl"
    with log_file.open("w") as log:
        try:
            timeout = remaining(deadline - 120, 300)
            process = subprocess.Popen(
                [
                    binary,
                    "run",
                    "--format",
                    "json",
                    "--dir",
                    str(candidate),
                    "--",
                    prompt,
                ],
                cwd=candidate,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                returncode = process.wait(timeout=timeout)
            finally:
                # The package launcher spawns a native OpenCode child. Stop the
                # whole owned group before replaying or repairing its files.
                _stop_owned_process(process)
        except subprocess.TimeoutExpired:
            # An agent can spend its last seconds testing a completed script.
            # Only the independent replay below may declare that script usable.
            if (candidate / "importer.py").is_file():
                return
            raise
    failure = learning_failure(log_file)
    if failure or returncode:
        raise LearningError(failure or LEARNING_FAILURE)


def main():
    deadline = time.monotonic() + int(os.environ.get("IMPORTERS_TIMEOUT", "900")) - 25
    request = read_request()
    config = load_config(PLUGIN / "config.json", hydrate_binaries=False).model_dump(
        mode="json",
    )
    definition = json.loads((PLUGIN / "config.json").read_text())["importers"][
        request["feed"]
    ]
    contract = hashlib.sha256(
        ((PLUGIN / "prompt.md").read_text() + definition["task"]).encode(),
    ).hexdigest()
    run_dir = Path.cwd()
    state = Path(os.environ["IMPORTERS_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    request_file = run_dir / "request.json"
    request_file.write_text(json.dumps(request))
    request_file.chmod(0o600)
    current = state / "current"
    current.mkdir(exist_ok=True)
    with connection(config) as (binary, env):
        token = hashlib.sha256(str(run_dir).encode()).hexdigest()[:16]
        env.update(
            IMPORTERS_REQUEST_FILE=str(request_file),
            BH_AGENT_WORKSPACE=str(state / "harness"),
            BH_DOMAIN_SKILLS="1",
            BH_TAB_MARKER="0",
            BU_NAME=f"importers-{token}",
        )
        # Grant only the source workspace and request log directory; no global
        # auto-approval or changes to the Agent UI's persisted permissions.
        agent_config = json.loads(env.get("OPENCODE_CONFIG_CONTENT") or "{}")
        # General Agent instructions run from the collection and allow host
        # administration. This task uses only its standalone importer contract.
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
            contract_file = current / "contract.sha256"
            if contract_file.is_file() and contract_file.read_text() == contract:
                records, error = replay(current, request, env, run_dir, 0, deadline)
            else:
                records, error = (
                    None,
                    "The task or importer contract changed. Revalidate the saved script against all current requirements.",
                )
            if records is None:
                candidate = state / "candidate"
                # Keep unsuccessful repair work for the next attempt/run. Only a
                # validated replay can promote it, but failures must still teach.
                if not candidate.exists():
                    shutil.copytree(current, candidate)
                (candidate / ".ignore").write_text("*\n")
                for attempt in (1, 2):
                    emit(
                        {
                            "type": "ImporterProgress",
                            "message": f"{'Learning' if not (current / 'importer.py').exists() else 'Repairing'} browser importer with OpenCode, Browser Harness and Stagehand (attempt {attempt}/2).",
                        },
                    )
                    learn(
                        binary,
                        request,
                        definition,
                        env,
                        candidate,
                        run_dir,
                        error,
                        attempt,
                        deadline,
                    )
                    records, error = replay(
                        candidate,
                        request,
                        env,
                        run_dir,
                        attempt,
                        deadline,
                    )
                    if records is None:
                        continue
                    if records[-1]["status"] == "succeeded":
                        (candidate / "contract.sha256").write_text(contract)
                        history = state / "history"
                        history.mkdir(exist_ok=True)
                        current.rename(history / str(time.time_ns()))
                        candidate.rename(current)
                        records[-1]["script_revision"] = hashlib.sha256(
                            (current / "importer.py").read_bytes(),
                        ).hexdigest()
                        records[-1]["repaired"] = True
                    break
                if records is None:
                    records = [
                        {
                            "type": "ImporterResult",
                            "status": "failed",
                            "message": "The browser importer still failed after two repair attempts. The last working script and checkpoint are preserved; private run logs contain the diagnosis.",
                        },
                    ]
    # A terminal record commits discovery. Publish only after owned resources
    # have closed successfully; cleanup errors must not append a second result.
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
                "message": str(error)
                if isinstance(error, LearningError)
                else f"Browser importer could not complete ({type(error).__name__}). Check its run logs, persona browser, and Agent provider configuration. Progress was not advanced.",
            },
        )
