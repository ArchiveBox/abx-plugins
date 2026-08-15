#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""Extract article content using trafilatura from local HTML snapshots."""

import sys
import argparse
import json
import os
import subprocess
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

TRAFILATURA_WORKER = r"""
import json
import sys
from copy import deepcopy
from pathlib import Path

from trafilatura.core import bare_extraction, determine_returnstring
from trafilatura.settings import Extractor

html_source, url, raw_formats = sys.argv[1:]
formats = json.loads(raw_formats)
html = Path(html_source).read_text(encoding="utf-8", errors="replace")
extraction_options = Extractor(
    output_format="python",
    url=url,
    with_metadata=True,
    formatting=True,
)
document = bare_extraction(html, options=extraction_options)
if document is None:
    print("{}")
    raise SystemExit(0)

outputs = {}
for output_format in formats:
    output_options = Extractor(
        output_format=output_format,
        url=url,
        with_metadata=output_format != "html",
        formatting=output_format == "markdown",
    )
    outputs[output_format] = determine_returnstring(
        deepcopy(document),
        output_options,
    )
print(json.dumps(outputs))
"""


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
    url: str,
    formats: list[str],
    timeout: int,
) -> tuple[bool, str]:
    managed_python = Path(binary).resolve().with_name("python")
    cmd = [
        str(managed_python),
        "-c",
        TRAFILATURA_WORKER,
        html_source,
        url,
        json.dumps(formats),
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
        return False, f"trafilatura failed (exit={result.returncode})"

    outputs = json.loads(result.stdout)
    if not outputs:
        return False, "trafilatura returned no extracted content"
    for fmt in formats:
        if fmt not in outputs:
            return False, f"trafilatura returned no format={fmt} output"
        write_text_atomic(OUTPUT_DIR / FORMAT_TO_FILE[fmt], outputs[fmt] or "")
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

    success, error = run_trafilatura(binary, html_source, url, formats, timeout)
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

        print("Trafilatura extraction started", flush=True)
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
