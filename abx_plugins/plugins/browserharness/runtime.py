"""Existing browser-harness tab lifecycle and importer replay."""

import hashlib
import json
import os
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from abx_plugins.plugins.base.importers import read_records


def remaining(deadline, maximum):
    seconds = min(maximum, deadline - time.monotonic())
    if seconds <= 0:
        raise TimeoutError("Importer time budget exhausted; progress is preserved.")
    return seconds


@contextmanager
def task_tab(env, run_dir):
    """Use the dedicated tab owned by this run's named browser-harness daemon."""
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
            try:
                yield
            except BaseException:
                # Cancellation and shutdown still need the last visible screen
                # before browser-harness closes this run's dedicated tab.
                capture_failure(env, run_dir)
                raise
        finally:
            # browser-harness owns both its daemon and dedicated tab.
            subprocess.run(
                ["browser-harness", "--reload"],
                env=env,
                stdout=log,
                stderr=log,
                timeout=15,
                check=False,
            )


def capture_failure(env, run_dir, attempt=0):
    """Keep each attempt's first failing viewport private through tab cleanup."""
    path = run_dir / f"failure-{attempt}.png"
    try:
        if path.exists():
            return
        subprocess.run(
            ["browser-harness"],
            input=f"capture_screenshot(path={str(path)!r})\n",
            env=env,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
        )
        path.chmod(0o600)
    except (OSError, subprocess.SubprocessError) as error:
        # Evidence failure must not replace the actual importer failure.
        try:
            with (run_dir / "evidence.log").open("a") as log:
                log.write(
                    f"Attempt {attempt}: screenshot unavailable ({type(error).__name__}).\n",
                )
        except OSError:
            pass  # The original failure still reaches the caller on a full disk.


def replay(workspace, request, env, run_dir, attempt, deadline):
    script = workspace / "importer.py"
    if not script.is_file():
        if attempt:
            capture_failure(env, run_dir, attempt)
        return None, "No learned importer script exists yet."
    succeeded = False
    try:
        process = subprocess.run(
            ["browser-harness"],
            input=(
                "import runpy\n"
                f"runpy.run_path({str(script.resolve())!r}, init_globals=globals(), run_name='__main__')\n"
            ),
            cwd=workspace,
            env=env,
            text=True,
            capture_output=True,
            timeout=remaining(deadline, 120),
            check=False,
        )
        with (run_dir / f"replay-{attempt}.log").open(
            "a" if request.get("delivered_ids") else "w",
        ) as log:
            if request.get("delivered_ids"):
                log.write("\nContinuation validation with the returned checkpoint:\n")
            log.write(process.stderr)
        if process.returncode:
            raise ValueError((process.stderr or process.stdout)[-12000:])
        records = list(read_records(process.stdout.splitlines(), request))
        result = records[-1]
        if result["status"] == "failed":
            raise ValueError(str(result.get("message", "Discovery failed.")))
        if result["status"] == "succeeded" and not result.get("account", {}).get("id"):
            raise ValueError("Identify the signed-in account before returning success.")
        if (
            result["status"] == "succeeded"
            and request["action"] == "import"
            and not request.get("delivered_ids")
        ):
            delivered = [
                record["id"] for record in records if record["type"] == "ImporterItem"
            ]
            if delivered:
                # Discovery is read-only. Probe one continuation before accepting
                # the batch, so cursor defects reach the existing repair loop.
                # Only the original batch is emitted/committed by the host.
                continuation = {
                    **request,
                    "checkpoint": result.get("checkpoint", {}),
                    "delivered_ids": delivered,
                }
                continuation_file = run_dir / "continuation-request.json"
                continuation_file.write_text(json.dumps(continuation))
                continuation_file.chmod(0o600)
                following, error = replay(
                    workspace,
                    continuation,
                    {**env, "IMPORTERS_REQUEST_FILE": str(continuation_file)},
                    run_dir,
                    attempt,
                    deadline,
                )
                if following is None:
                    return None, error + " Continuation request: " + str(
                        continuation_file,
                    )
                if following[-1]["status"] != "succeeded":
                    return following, ""
        succeeded = result["status"] == "succeeded"
        return records, ""
    except (TypeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        message = f"{type(error).__name__}: {error}"
        try:
            with (run_dir / f"replay-{attempt}.log").open("a") as log:
                log.write("\nReplay rejected: " + message + "\n")
        except OSError:
            pass  # Preserve the actual replay error if evidence cannot be saved.
        return None, message
    finally:
        if not succeeded and not request.get("delivered_ids"):
            capture_failure(env, run_dir, attempt)


def browser_environment(endpoint):
    return {
        "BU_CDP_WS": endpoint,
        "BU_CDP_URL": endpoint,
        "BU_NAME": "abx-" + hashlib.sha256(endpoint.encode()).hexdigest()[:16],
        "BH_TAB_MARKER": "0",
        # Unix socket paths have a 104-byte limit on macOS. Collection paths
        # routinely exceed that; use the harness's private runtime directory.
        "BH_RUNTIME_DIR": str(Path(tempfile.gettempdir()) / f"abx-bh-{os.getuid()}"),
        "BH_RUNTIME_DIR_SHARED": "1",
    }


def importer_environment(state, run_dir, request_file):
    return dict(
        IMPORTERS_REQUEST_FILE=str(request_file),
        BH_AGENT_WORKSPACE=str(state / "harness"),
        BH_DOMAIN_SKILLS="1",
        BH_TAB_MARKER="0",
        BU_NAME="importers-" + hashlib.sha256(str(run_dir).encode()).hexdigest()[:16],
    )


def agent_environment(endpoint, binaries):
    """Connection settings for the registered browser-harness agent tool."""
    return browser_environment(endpoint)
