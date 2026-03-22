#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "rich-click",
#   "abx-plugins",
# ]
# [tool.uv.sources]
# abx-plugins = { path = "../../..", editable = true }
# ///
#
# Download forum content from a URL using forum-dl with Pydantic v2 compatibility.
# Outputs forum data to $PWD/ and emits ArchiveResult events to stdout.
#
# Usage:
#     ./on_Snapshot__04_forumdl.finite.bg.py --url=<url>

import os
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    load_config,
    resolve_binary_path as resolve_binary_ref,
)

import rich_click as click


# Extractor metadata
PLUGIN_NAME = "forumdl"
BIN_NAME = "forum-dl"
BIN_PROVIDERS = "env,pip"
PLUGIN_DIR = Path(__file__).resolve().parent.name
SNAP_DIR = Path(os.environ.get("SNAP_DIR", ".")).resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def rel_output(path_str: str | None) -> str | None:
    if not path_str:
        return path_str
    path = Path(path_str)
    resolved = path.resolve()
    if not resolved.exists():
        return path_str
    try:
        return str(resolved.relative_to(SNAP_DIR.resolve()))
    except Exception:
        return path.name or path_str


def resolve_binary_path(binary: str) -> str | None:
    """Resolve binary to an absolute path if possible."""
    return resolve_binary_ref(binary)


def save_forum(url: str, binary: str) -> tuple[bool, str | None, str]:
    """
    Download forum using forum-dl.

    Returns: (success, output_path, error_message)
    """
    # Load config from config.json (auto-resolves x-aliases and x-fallback from env)
    config = load_config()
    timeout = config.FORUMDL_TIMEOUT
    forumdl_args = config.FORUMDL_ARGS
    forumdl_args_extra = config.FORUMDL_ARGS_EXTRA
    output_format = config.FORUMDL_OUTPUT_FORMAT

    # Output directory is current directory (hook already runs in output dir)
    output_dir = Path(OUTPUT_DIR)

    # Build output filename based on format
    if output_format == "warc":
        output_file = output_dir / "forum.warc.gz"
    elif output_format == "jsonl":
        output_file = output_dir / "forum.jsonl"
    elif output_format == "maildir":
        output_file = output_dir / "forum"  # maildir is a directory
    elif output_format in ("mbox", "mh", "mmdf", "babyl"):
        output_file = output_dir / f"forum.{output_format}"
    else:
        output_file = output_dir / f"forum.{output_format}"

    resolved_binary = resolve_binary_path(binary) or binary
    # Inject a sitecustomize shim via PYTHONPATH so forum-dl can still run as a
    # black-box executable while we patch its Pydantic v2 incompatibility.
    sitecustomize_code = textwrap.dedent(
        """
        try:
            from forum_dl.writers.jsonl import JsonlWriter
            from pydantic import BaseModel
            if hasattr(BaseModel, "model_dump_json"):
                def _patched_serialize_entry(self, entry):
                    return entry.model_dump_json()
                JsonlWriter._serialize_entry = _patched_serialize_entry
        except Exception:
            pass
        """,
    ).strip()
    cmd = [
        resolved_binary,
        *forumdl_args,
        "-f",
        output_format,
        "-o",
        str(output_file),
    ]

    if forumdl_args_extra:
        cmd.extend(forumdl_args_extra)

    cmd.append(url)

    try:
        print(f"[forumdl] Starting download (timeout={timeout}s)", file=sys.stderr)
        output_lines: list[str] = []
        with tempfile.TemporaryDirectory(prefix="forumdl-sitecustomize-") as shim_dir:
            shim_path = Path(shim_dir) / "sitecustomize.py"
            shim_path.write_text(sitecustomize_code, encoding="utf-8")

            env = os.environ.copy()
            existing_pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                f"{shim_dir}{os.pathsep}{existing_pythonpath}"
                if existing_pythonpath
                else shim_dir
            )
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )

            def _read_output() -> None:
                if not process.stdout:
                    return
                for line in process.stdout:
                    output_lines.append(line)
                    sys.stderr.write(line)

            reader = threading.Thread(target=_read_output, daemon=True)
            reader.start()

            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                reader.join(timeout=1)
                return False, None, f"Timed out after {timeout} seconds"

            reader.join(timeout=1)
            combined_output = "".join(output_lines)

            # Check if output file was created
            if output_file.exists() and output_file.stat().st_size > 0:
                return True, str(output_file), ""
            else:
                stderr = combined_output

                # These are NOT errors - page simply has no downloadable forum content
                stderr_lower = stderr.lower()
                if "unsupported url" in stderr_lower:
                    return True, "No forum found", ""
                if "no content" in stderr_lower:
                    return True, "No forum found", ""
                if "extractornotfounderror" in stderr_lower:
                    return True, "No forum found", ""
                if process.returncode == 0:
                    return True, "No forum found", ""

                # These ARE errors - something went wrong
                if "404" in stderr:
                    return False, None, "404 Not Found"
                if "403" in stderr:
                    return False, None, "403 Forbidden"
                if "unable to extract" in stderr_lower:
                    return False, None, "Unable to extract forum info"

                return False, None, f"forum-dl error: {stderr}"

    except subprocess.TimeoutExpired:
        return False, None, f"Timed out after {timeout} seconds"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="URL to download forum from")
def main(url: str):
    """Download forum content from a URL using forum-dl."""

    output = None
    error = ""

    try:
        config = load_config()

        # Check if forum-dl is enabled
        if not config.FORUMDL_ENABLED:
            print("Skipping forum-dl (FORUMDL_ENABLED=False)", file=sys.stderr)
            emit_archive_result_record("skipped", "FORUMDL_ENABLED=False")
            sys.exit(0)

        # Get binary from environment
        binary = config.FORUMDL_BINARY

        # Run extraction
        success, output, error = save_forum(url, binary)

        if success:
            status = "noresults" if output == "No forum found" else "succeeded"
            # Success - emit ArchiveResult
            emit_archive_result_record(status, rel_output(output) or "")
            sys.exit(0)
        else:
            print(f"ERROR: {error}", file=sys.stderr)
            emit_archive_result_record("failed", error or "")
            sys.exit(1)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result_record("failed", error)
        sys.exit(1)


if __name__ == "__main__":
    main()
