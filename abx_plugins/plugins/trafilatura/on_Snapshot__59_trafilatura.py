#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "abx-plugins",
# ]
# [tool.uv.sources]
# abx-plugins = { path = "../../..", editable = true }
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
    resolve_binary_path,
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
OUTPUT_ENV_TO_FORMAT = {
    "TRAFILATURA_OUTPUT_TXT": "txt",
    "TRAFILATURA_OUTPUT_MARKDOWN": "markdown",
    "TRAFILATURA_OUTPUT_HTML": "html",
    "TRAFILATURA_OUTPUT_CSV": "csv",
    "TRAFILATURA_OUTPUT_JSON": "json",
    "TRAFILATURA_OUTPUT_XML": "xml",
    "TRAFILATURA_OUTPUT_XMLTEI": "xmltei",
}

TRAFILATURA_EXTRACT_SCRIPT = """
import sys
from pathlib import Path
import trafilatura

html = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
url = sys.argv[2]
fmt = sys.argv[3]
result = trafilatura.extract(
    html,
    output_format=fmt,
    with_metadata=True,
    url=url,
) or ""
sys.stdout.write(result)
"""


def get_enabled_formats() -> list[str]:
    """Return list of output formats enabled via config (e.g. TRAFILATURA_OUTPUT_TXT=true).

    Defaults come from config.json: txt, markdown, html are enabled;
    csv, json, xml, xmltei are disabled.
    """
    config = load_config()
    return [
        fmt
        for env_name, fmt in OUTPUT_ENV_TO_FORMAT.items()
        if getattr(config, env_name)
    ]


def run_trafilatura(
    binary: str,
    html_source: str,
    url: str,
    fmt: str,
    timeout: int,
) -> tuple[bool, str]:
    resolved_binary = resolve_binary_path(binary) or binary
    binary_path = Path(resolved_binary)

    python_candidates = (
        binary_path.with_name("python"),
        binary_path.with_name("python3"),
    )
    python_bin = next(
        (candidate for candidate in python_candidates if candidate.exists()),
        Path(sys.executable),
    )

    cmd = [
        str(python_bin),
        "-c",
        TRAFILATURA_EXTRACT_SCRIPT,
        html_source,
        url,
        fmt,
    ]
    result = subprocess.run(
        cmd,
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
        success, error = run_trafilatura(binary, html_source, url, fmt, timeout)
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
