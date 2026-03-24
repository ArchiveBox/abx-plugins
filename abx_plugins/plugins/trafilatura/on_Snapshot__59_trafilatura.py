#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "abx-plugins",
# ]
# ///
"""Extract article content using trafilatura from local HTML snapshots."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    find_article_html_source,
    load_config,
    write_text_atomic,
)

PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)

FORMAT_TO_FILE = {
    "txt": "content.txt",
    "markdown": "content.md",
    "html": "content.html",
    "csv": "content.csv",
    "json": "content.json",
    "xml": "content.xml",
    "xmltei": "content.xmltei",
}


def get_enabled_formats() -> list[str]:
    """Return enabled output formats from TRAFILATURA_OUTPUT_FORMATS CSV config."""
    config = load_config()
    formats = []
    for fmt in config.TRAFILATURA_OUTPUT_FORMATS.split(","):
        fmt = fmt.strip()
        if fmt and fmt in FORMAT_TO_FILE and fmt not in formats:
            formats.append(fmt)
    return formats


def run_trafilatura(
    binary: str,
    html_source: str,
    fmt: str,
    timeout: int,
) -> tuple[bool, str]:
    html = Path(html_source).read_text(encoding="utf-8", errors="replace")

    cmd = [
        binary,
        "--output-format",
        fmt,
    ]
    if fmt != "html":
        # trafilatura 2.0.0 can emit a traceback and empty output for HTML when
        # metadata contains list values, so only request metadata on formats
        # that serialize it correctly.
        cmd.append("--with-metadata")
    result = subprocess.run(
        cmd,
        input=html,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.stderr:
        sys.stderr.write(result.stderr)
        sys.stderr.flush()
    if result.returncode != 0:
        return False, f"trafilatura failed for format={fmt} (exit={result.returncode})"

    write_text_atomic(OUTPUT_DIR / FORMAT_TO_FILE[fmt], result.stdout or "")
    return True, ""


def extract_trafilatura(url: str, binary: str) -> tuple[str, str]:
    config = load_config()
    timeout = config.TRAFILATURA_TIMEOUT
    html_source = find_article_html_source()
    if not html_source:
        return "noresults", "No HTML source found"

    formats = get_enabled_formats()
    if not formats:
        return "noresults", "No output formats enabled"

    for fmt in formats:
        success, error = run_trafilatura(binary, html_source, fmt, timeout)
        if not success:
            return "failed", error

    output_file = FORMAT_TO_FILE[formats[0]]
    return "succeeded", f"{PLUGIN_DIR}/{output_file}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="URL to extract article from")
    args, _unknown = parser.parse_known_args()

    try:
        config = load_config()

        if not config.TRAFILATURA_ENABLED:
            emit_archive_result_record("skipped", "TRAFILATURA_ENABLED=False")
            sys.exit(0)

        status, output = extract_trafilatura(args.url, config.TRAFILATURA_BINARY)

        if status == "failed":
            print(f"ERROR: {output}", file=sys.stderr)
        emit_archive_result_record(status, output)
        sys.exit(0 if status != "failed" else 1)

    except subprocess.TimeoutExpired as err:
        error = f"Timed out after {err.timeout} seconds"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result_record("failed", error)
        sys.exit(1)
    except Exception as err:
        error = f"{type(err).__name__}: {err}"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result_record("failed", error)
        sys.exit(1)


if __name__ == "__main__":
    main()
