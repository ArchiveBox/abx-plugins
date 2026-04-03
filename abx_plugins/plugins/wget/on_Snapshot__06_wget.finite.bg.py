#!/usr/bin/env -S uv run --active --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "rich-click",
#   "abx-plugins",
# ]
# ///
#
# Archive a URL using wget.
#
# Usage: on_Snapshot__06_wget.finite.bg.py --url=<url>
# Output: Downloads files to $PWD
#
# Environment variables:
#     WGET_ENABLED: Enable wget archiving (default: True)
#     WGET_WARC_ENABLED: Save WARC file (default: True)
#     WGET_BINARY: Path to wget binary (default: wget)
#     WGET_TIMEOUT: Timeout in seconds (x-fallback: TIMEOUT)
#     WGET_USER_AGENT: User agent string (x-fallback: USER_AGENT)
#     WGET_COOKIES_FILE: Path to cookies file (x-fallback: COOKIES_FILE)
#     WGET_CHECK_SSL_VALIDITY: Whether to check SSL certificates (x-fallback: CHECK_SSL_VALIDITY)
#     WGET_ARGS: Default wget arguments (JSON array)
#     WGET_ARGS_EXTRA: Extra arguments to append (JSON array)
#

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    has_staticfile_output,
    load_config,
)

import rich_click as click


# Extractor metadata
PLUGIN_NAME = "wget"
BIN_NAME = "wget"
BIN_PROVIDERS = "env,apt,brew"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
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


def save_wget(url: str, binary: str) -> tuple[bool, str | None, str]:
    """
    Archive URL using wget.

    Returns: (success, output_path, error_message)
    """
    # Load config from config.json (auto-resolves x-aliases and x-fallback from env)
    config = load_config()
    timeout = config.WGET_TIMEOUT
    user_agent = config.WGET_USER_AGENT or "Mozilla/5.0 (compatible; ArchiveBox/1.0)"
    check_ssl = config.WGET_CHECK_SSL_VALIDITY
    cookies_file = config.WGET_COOKIES_FILE
    wget_args = config.WGET_ARGS
    wget_args_extra = config.WGET_ARGS_EXTRA
    warc_enabled = config.WGET_WARC_ENABLED

    # Build wget command (later options take precedence)
    cmd = [
        binary,
        *wget_args,
        f"--timeout={timeout}",
    ]

    if user_agent:
        cmd.append(f"--user-agent={user_agent}")

    if warc_enabled:
        warc_dir = Path("warc")
        warc_dir.mkdir(exist_ok=True)
        warc_path = warc_dir / str(int(datetime.now(timezone.utc).timestamp()))
        cmd.append(f"--warc-file={warc_path}")
    else:
        cmd.append("--timestamping")

    if cookies_file and Path(cookies_file).is_file():
        cmd.extend(["--load-cookies", cookies_file])

    if not check_ssl:
        cmd.extend(["--no-check-certificate", "--no-hsts"])

    if wget_args_extra:
        cmd.extend(wget_args_extra)

    cmd.append(url)

    # Run wget
    try:
        print("saving page with wget...")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout * 2,  # Allow extra time for large downloads
        )

        # Find downloaded files
        downloaded_files = [
            f
            for f in Path(".").rglob("*")
            if f.is_file() and f.name != ".gitkeep" and not str(f).startswith("warc/")
        ]

        if not downloaded_files:
            if result.returncode != 0:
                return False, None, f"wget failed (exit={result.returncode})"
            return True, "No files downloaded", ""

        # Find main HTML file
        html_files = [
            f
            for f in downloaded_files
            if re.search(r"\.[Ss]?[Hh][Tt][Mm][Ll]?$", str(f))
        ]
        output_path = str(html_files[0]) if html_files else str(downloaded_files[0])

        return True, output_path, ""

    except subprocess.TimeoutExpired:
        return False, None, f"Timed out after {timeout * 2} seconds"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="URL to archive")
def main(url: str):
    """Archive a URL using wget."""

    output = None
    error = ""

    try:
        config = load_config()

        # Check if wget is enabled
        if not config.WGET_ENABLED:
            print("Skipping wget (WGET_ENABLED=False)", file=sys.stderr)
            emit_archive_result_record("skipped", "WGET_ENABLED=False")
            sys.exit(0)

        # Check if staticfile extractor already handled this (permanent skip)
        if has_staticfile_output():
            print(
                "Skipping wget - staticfile extractor already downloaded this",
                file=sys.stderr,
            )
            emit_archive_result_record("noresults", "staticfile already handled")
            sys.exit(0)

        # Get binary from environment
        binary = config.WGET_BINARY

        # Run extraction
        success, output, error = save_wget(url, binary)

        if success:
            status = "noresults" if output == "No files downloaded" else "succeeded"
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
