#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""Save the exact navigated Chrome snapshot tab with SingleFile."""

import sys
import os
import subprocess
from pathlib import Path

import rich_click as click

from abx_plugins.plugins.base.utils import load_config, emit_archive_result_record


PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
CONFIG = load_config(CONFIG_PATH)
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
OUTPUT_FILE = "singlefile.html"
EXTENSION_SAVE_SCRIPT = Path(__file__).parent / "singlefile_extension_save.js"


def temp_path_for(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.tmp")


def summarize_error(detail: str) -> str:
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    for line in reversed(lines):
        for prefix in ("ERROR:", "Error:", "[❌]"):
            if line.startswith(prefix):
                return line.removeprefix(prefix).strip()
    return lines[-1] if lines else "SingleFile extension failed"


def save_singlefile_with_extension(
    url: str,
    timeout: int,
) -> tuple[bool, str | None, str]:
    output_path = OUTPUT_DIR / OUTPUT_FILE
    temp_output_path = temp_path_for(output_path)
    result = subprocess.run(
        [
            str(EXTENSION_SAVE_SCRIPT),
            f"--url={url}",
            f"--output-path={temp_output_path}",
        ],
        cwd=OUTPUT_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.stdout:
        print(result.stdout, end="", file=sys.stderr)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    if (
        result.returncode == 0
        and temp_output_path.exists()
        and temp_output_path.stat().st_size > 0
    ):
        temp_output_path.replace(output_path)
        return True, f"{PLUGIN_DIR}/{OUTPUT_FILE}", ""
    return False, None, summarize_error(result.stderr or result.stdout)


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="URL to archive")
def main(url: str) -> None:
    config = load_config(CONFIG_PATH)
    if not config.SINGLEFILE_ENABLED:
        emit_archive_result_record("skipped", "SINGLEFILE_ENABLED=False")
        raise SystemExit(0)

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
