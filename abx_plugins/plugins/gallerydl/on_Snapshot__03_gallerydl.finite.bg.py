#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "rich-click",
#   "abx-plugins",
# ]
# ///
#
# Download image galleries from a URL using gallery-dl binary, handling SSL verification,
# cookies, and timeout configurations via environment variables.
#
# Usage:
#     ./on_Snapshot__03_gallerydl.finite.bg.py --url=<url> --snapshot-id=<uuid> > events.jsonl

import os
import subprocess
import sys
import threading
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    has_staticfile_output,
    load_config,
)

import rich_click as click


# Extractor metadata
PLUGIN_NAME = "gallerydl"
BIN_NAME = "gallery-dl"
BIN_PROVIDERS = "env,pip"
PLUGIN_DIR = Path(__file__).resolve().parent.name
SNAP_DIR = Path(os.environ.get("SNAP_DIR", ".")).resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
EXECUTOR_ARTIFACT_SUFFIXES = (
    ".stdout.log",
    ".stderr.log",
    ".pid",
    ".sh",
    ".meta.json",
)


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


def save_gallery(url: str, binary: str) -> tuple[bool, str | None, str]:
    """
    Download gallery using gallery-dl.

    Returns: (success, output_path, error_message)
    """
    # Load config from config.json (auto-resolves x-aliases and x-fallback from env)
    config = load_config()
    timeout = config.GALLERYDL_TIMEOUT
    check_ssl = config.GALLERYDL_CHECK_SSL_VALIDITY
    gallerydl_args = config.GALLERYDL_ARGS
    gallerydl_args_extra = config.GALLERYDL_ARGS_EXTRA
    cookies_file = config.GALLERYDL_COOKIES_FILE

    # Output directory is current directory (hook already runs in output dir)
    output_dir = Path(OUTPUT_DIR)

    # Build command
    # Use -D for exact directory (flat structure) instead of -d (nested structure)
    cmd = [
        binary,
        *gallerydl_args,
        "-D",
        str(output_dir),
    ]

    if not check_ssl:
        cmd.append("--no-check-certificate")

    if cookies_file and Path(cookies_file).exists():
        cmd.extend(["-C", cookies_file])

    if gallerydl_args_extra:
        cmd.extend(gallerydl_args_extra)

    cmd.append(url)

    try:
        print(f"[gallerydl] Starting download (timeout={timeout}s)", file=sys.stderr)
        output_lines: list[str] = []
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
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

        # Check if any gallery files were downloaded (search recursively)
        gallery_extensions = (
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".bmp",
            ".svg",
            ".mp4",
            ".webm",
            ".mkv",
            ".avi",
            ".mov",
            ".flv",
            ".json",
            ".txt",
            ".zip",
        )

        downloaded_files = [
            f
            for f in output_dir.rglob("*")
            if f.is_file()
            and f.suffix.lower() in gallery_extensions
            and not any(
                f.name.endswith(suffix) for suffix in EXECUTOR_ARTIFACT_SUFFIXES
            )
        ]

        if downloaded_files:
            # Return first image file, or first file if no images
            image_files = [
                f
                for f in downloaded_files
                if f.suffix.lower()
                in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
            ]
            output = str(image_files[0]) if image_files else str(downloaded_files[0])
            return True, output, ""
        else:
            stderr = combined_output

            # These are NOT errors - page simply has no downloadable gallery
            # Return success with no output (legitimate "nothing to download")
            stderr_lower = stderr.lower()
            if "unsupported url" in stderr_lower:
                return True, "No gallery found", ""
            if "no results" in stderr_lower:
                return True, "No gallery found", ""
            if process.returncode == 0:
                return True, "No gallery found", ""

            # These ARE errors - something went wrong
            if "404" in stderr:
                return False, None, "404 Not Found"
            if "403" in stderr:
                return False, None, "403 Forbidden"
            if "unable to extract" in stderr_lower:
                return False, None, "Unable to extract gallery info"

            return False, None, f"gallery-dl error: {stderr}"

    except subprocess.TimeoutExpired:
        return False, None, f"Timed out after {timeout} seconds"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


@click.command()
@click.option("--url", required=True, help="URL to download gallery from")
@click.option("--snapshot-id", required=True, help="Snapshot UUID")
def main(url: str, snapshot_id: str):
    """Download image gallery from a URL using gallery-dl."""

    output = None
    error = ""

    try:
        config = load_config()

        # Check if gallery-dl is enabled
        if not config.GALLERYDL_ENABLED:
            print("Skipping gallery-dl (GALLERYDL_ENABLED=False)", file=sys.stderr)
            emit_archive_result_record("skipped", "GALLERYDL_ENABLED=False")
            sys.exit(0)

        # Check if staticfile extractor already handled this (permanent skip)
        if has_staticfile_output():
            print(
                "Skipping gallery-dl - staticfile extractor already downloaded this",
                file=sys.stderr,
            )
            emit_archive_result_record("succeeded", "staticfile already handled")
            sys.exit(0)

        # Get binary from environment
        binary = config.GALLERYDL_BINARY

        # Run extraction
        success, output, error = save_gallery(url, binary)

        if success:
            status = "noresults" if output == "No gallery found" else "succeeded"
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
