#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""Save the exact navigated Chrome snapshot tab with SingleFile."""

import sys
import os
import subprocess
import time
from collections import deque
from threading import Thread
from pathlib import Path

import rich_click as click

from abx_plugins.plugins.base.utils import (
    load_config,
    emit_archive_result_record,
    has_staticfile_output,
    is_non_html_document,
    has_failed_source_navigation,
)


PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
CONFIG = load_config(CONFIG_PATH)
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
OUTPUT_FILE = "singlefile.html"
EXTENSION_SAVE_SCRIPT = Path(__file__).parent / "singlefile_extension_save.js"
# Gallery already opts into bounded failure artifacts with this variable. Use
# it only as a switch; hooks must never access that directory.
TIMELINE_ENABLED = sys.platform == "linux" and os.path.isabs(
    os.environ.get("SCREENSHOT_FAILURE_DIR", ""),
)


def temp_path_for(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.tmp")


def summarize_error(detail: str) -> str:
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    for line in reversed(lines):
        for prefix in ("ERROR:", "Error:", "[❌]"):
            if line.startswith(prefix):
                return line.removeprefix(prefix).strip()
    return lines[-1] if lines else "SingleFile extension failed"


def emit_timeline(event: str, monotonic_ns: int) -> None:
    # Sample now, not when stderr is writable. Diagnostics are best-effort:
    # never let an unread pipe or filesystem hold capture or helper cleanup.
    record = f"[timeline] event={event} monotonic_ns={monotonic_ns}\n".encode()

    def write_record() -> None:
        try:
            os.write(2, record)
        except OSError:
            pass

    try:
        Thread(target=write_record, name="singlefile-timeline", daemon=True).start()
    except (OSError, RuntimeError):
        pass


def wait_for_singlefile_helper(
    helper: subprocess.Popen,
    reader: Thread,
    deadline: float,
    timeout: int,
) -> None:
    try:
        helper.wait(timeout=max(0, deadline - time.monotonic()))
        reader.join(timeout=max(0, deadline - time.monotonic()))
        if reader.is_alive():
            raise subprocess.TimeoutExpired(helper.args, timeout)
    except BaseException as exc:
        timed_out_at = (
            time.monotonic_ns()
            if TIMELINE_ENABLED and isinstance(exc, subprocess.TimeoutExpired)
            else None
        )
        helper.kill()
        helper.wait()
        if timed_out_at is not None:
            emit_timeline("singlefile_wrapper_timeout", timed_out_at)
        raise


def save_singlefile_with_extension(
    url: str,
    timeout: int,
) -> tuple[bool, str | None, str]:
    # We already own OUTPUT_DIR as cwd. Keep publication relative to that inode:
    # absolute paths revalidate expired rclone ancestors (potentially listing
    # thousands of sibling snapshots) before even returning cross-device EXDEV.
    # The helper inherits this cwd; validation and atomic replacement must too.
    output_path = Path(OUTPUT_FILE)
    temp_output_path = temp_path_for(output_path)
    # The runner may kill this wrapper before its own helper deadline expires.
    # Forward phases immediately, retaining only a bounded tail for the result.
    recent_output: deque[str] = deque(maxlen=64)
    deadline = time.monotonic() + timeout
    if TIMELINE_ENABLED:
        emit_timeline("singlefile_wrapper_begin", time.monotonic_ns())
    with subprocess.Popen(
        [
            str(EXTENSION_SAVE_SCRIPT),
            f"--url={url}",
            f"--output-path={temp_output_path}",
        ],
        cwd=OUTPUT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    ) as helper:
        output_pipe = helper.stdout
        assert output_pipe is not None

        def forward_output() -> None:
            while detail := output_pipe.readline(8192):
                recent_output.append(detail)
                print(detail, end="", file=sys.stderr, flush=True)

        reader = Thread(target=forward_output, daemon=True)
        reader.start()
        wait_for_singlefile_helper(helper, reader, deadline, timeout)

    if (
        helper.returncode == 0
        and temp_output_path.exists()
        and temp_output_path.stat().st_size > 0
    ):
        temp_output_path.replace(output_path)
        return True, f"{PLUGIN_DIR}/{OUTPUT_FILE}", ""
    return False, None, summarize_error("".join(recent_output))


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="URL to archive")
def main(url: str) -> None:
    config = load_config(CONFIG_PATH)
    if not config.SINGLEFILE_ENABLED:
        emit_archive_result_record("skipped", "SINGLEFILE_ENABLED=False")
        raise SystemExit(0)

    if has_staticfile_output():
        emit_archive_result_record("noresults", "staticfile already handled")
        raise SystemExit(0)

    if is_non_html_document():
        emit_archive_result_record("noresults", "Browser document is not HTML")
        raise SystemExit(0)
    if has_failed_source_navigation(url, SNAP_DIR):
        emit_archive_result_record("failed", "Source navigation failed")
        raise SystemExit(1)

    try:
        print("SingleFile extraction started", flush=True)
        print("generating singlefile.html...")
        success, output, error = save_singlefile_with_extension(
            url,
            int(config.SINGLEFILE_TIMEOUT),
        )
        status = "succeeded" if success else "failed"
    except subprocess.TimeoutExpired:
        output = None
        error = f"Timed out after {config.SINGLEFILE_TIMEOUT} seconds"
        status = "failed"
    except Exception as exc:
        output = None
        error = f"{type(exc).__name__}: {exc}"
        status = "failed"

    if error:
        print(f"ERROR: {error}", file=sys.stderr)
    emit_archive_result_record(status, output or error or "")
    raise SystemExit(0 if status == "succeeded" else 1)


if __name__ == "__main__":
    main()
