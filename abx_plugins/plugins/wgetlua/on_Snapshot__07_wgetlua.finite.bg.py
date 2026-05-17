#!/usr/bin/env -S uv run --script
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
# Archive a URL using wget-at (Archive Team wget-lua) for better WARC compliance.
#
# Usage: on_Snapshot__07_wgetlua.finite.bg.py --url=<url>
# Output: Downloads files to $PWD
#
# Environment variables:
#     WGETLUA_ENABLED: Enable wget-at archiving (default: True)
#     WGETLUA_WARC_ENABLED: Save WARC file (default: True)
#     WGETLUA_BINARY: Path to wget-at binary (default: wget-at)
#     WGETLUA_TIMEOUT: Timeout in seconds (x-fallback: TIMEOUT)
#     WGETLUA_USER_AGENT: User agent string (x-fallback: USER_AGENT)
#     WGETLUA_COOKIES_FILE: Path to cookies file (x-fallback: COOKIES_FILE)
#     WGETLUA_CHECK_SSL_VALIDITY: Whether to check SSL certificates (x-fallback: CHECK_SSL_VALIDITY)
#     WGETLUA_ARGS: Default wget-at arguments (JSON array)
#     WGETLUA_ARGS_EXTRA: Extra arguments to append (JSON array)
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
PLUGIN_NAME = "wgetlua"
BIN_NAME = "wget-at"
BIN_PROVIDERS = "env,brew,custom"
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


def save_wgetlua(url: str, binary: str) -> tuple[bool, str | None, str]:
    """
    Archive URL using wget-at (Archive Team wget-lua).

    Returns: (success, output_path, error_message)
    """
    # Load config from config.json (auto-resolves x-aliases and x-fallback from env)
    config = load_config()
    timeout = config.WGETLUA_TIMEOUT
    user_agent = config.WGETLUA_USER_AGENT or "Mozilla/5.0 (compatible; ArchiveBox/1.0)"
    check_ssl = config.WGETLUA_CHECK_SSL_VALIDITY
    cookies_file = config.WGETLUA_COOKIES_FILE
    wgetlua_args = config.WGETLUA_ARGS
    wgetlua_args_extra = config.WGETLUA_ARGS_EXTRA
    warc_enabled = config.WGETLUA_WARC_ENABLED

    # Build wget-at command (later options take precedence)
    cmd = [
        binary,
        *wgetlua_args,
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

    if wgetlua_args_extra:
        cmd.extend(wgetlua_args_extra)

    cmd.append(url)

    # Run wget-at
    try:
        print("saving page with wget-at (Archive Team wget-lua)...")
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
                return False, None, f"wget-at failed (exit={result.returncode})"
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
    """Archive a URL using wget-at (Archive Team wget-lua)."""

    output = None
    error = ""

    try:
        config = load_config()

        # Check if wgetlua is enabled
        if not config.WGETLUA_ENABLED:
            print("Skipping wgetlua (WGETLUA_ENABLED=False)", file=sys.stderr)
            emit_archive_result_record("skipped", "WGETLUA_ENABLED=False")
            sys.exit(0)

        # Check if staticfile extractor already handled this (permanent skip)
        if has_staticfile_output():
            print(
                "Skipping wgetlua - staticfile extractor already downloaded this",
                file=sys.stderr,
            )
            emit_archive_result_record("noresults", "staticfile already handled")
            sys.exit(0)

        # Get binary from environment
        binary = config.WGETLUA_BINARY

        # Run extraction
        success, output, error = save_wgetlua(url, binary)

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
