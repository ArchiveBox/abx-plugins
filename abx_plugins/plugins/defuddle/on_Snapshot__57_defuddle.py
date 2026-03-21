#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "rich-click",
# ]
# ///
#
# Extract article content using Defuddle.

import argparse
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import load_config, emit_archive_result_record, write_text_atomic, find_html_source

PLUGIN_DIR = Path(__file__).resolve().parent.name
SNAP_DIR = Path(os.environ.get("SNAP_DIR", ".")).resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
HTML_FILE = "content.html"
TEXT_FILE = "content.txt"
METADATA_FILE = "article.json"


def extract_defuddle(url: str, binary: str) -> tuple[str, str]:
    config = load_config()
    timeout = config.DEFUDDLE_TIMEOUT
    defuddle_args = config.DEFUDDLE_ARGS
    defuddle_args_extra = config.DEFUDDLE_ARGS_EXTRA
    output_dir = Path(OUTPUT_DIR)
    html_source = find_html_source()
    if not html_source:
        return "noresults", "No HTML source found"

    try:
        cmd = [
            binary,
            *defuddle_args,
            "parse",
            html_source,
            *defuddle_args_extra,
        ]
        if "--json" not in cmd and "-j" not in cmd:
            cmd.append("--json")
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True,
        )

        if result.returncode != 0:
            err = (result.stderr or "").strip()
            if err:
                return "failed", f"defuddle failed (exit={result.returncode}): {err}"
            return "failed", f"defuddle failed (exit={result.returncode})"

        raw_output = result.stdout.strip()
        html_content = ""
        text_content = ""
        metadata: dict[str, object] = {}

        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError:
            parsed = None

        if isinstance(parsed, dict):
            html_content = str(parsed.get("content") or parsed.get("html") or "")
            text_content = str(
                parsed.get("textContent")
                or parsed.get("text")
                or parsed.get("markdown")
                or ""
            )
            metadata = {
                key: value
                for key, value in parsed.items()
                if key not in {"content", "html", "textContent", "text", "markdown"}
            }
        elif raw_output:
            text_content = raw_output

        if text_content and not html_content:
            html_content = f"<pre>{html.escape(text_content)}</pre>"

        if not text_content and html_content:
            text_content = re.sub(r"<[^>]+>", " ", html_content)
            text_content = " ".join(text_content.split())

        if not text_content and not html_content:
            return "noresults", "No content extracted"

        write_text_atomic(output_dir / HTML_FILE, html_content)
        write_text_atomic(output_dir / TEXT_FILE, text_content)
        write_text_atomic(
            output_dir / METADATA_FILE, json.dumps(metadata, indent=2)
        )

        return "succeeded", f"{PLUGIN_DIR}/{HTML_FILE}"
    except subprocess.TimeoutExpired:
        return "failed", f"Timed out after {timeout} seconds"
    except Exception as e:
        return "failed", f"{type(e).__name__}: {e}"


def main():
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument("--url", required=True, help="URL to extract article from")
        parser.add_argument("--snapshot-id", required=True, help="Snapshot UUID")
        args = parser.parse_args()

        config = load_config()

        if not config.DEFUDDLE_ENABLED:
            print("Skipping defuddle (DEFUDDLE_ENABLED=False)", file=sys.stderr)
            emit_archive_result_record("skipped", "DEFUDDLE_ENABLED=False")
            sys.exit(0)

        binary = config.DEFUDDLE_BINARY
        status, output = extract_defuddle(args.url, binary)
        if status == "failed":
            print(f"ERROR: {output}", file=sys.stderr)
        emit_archive_result_record(status, output)
        sys.exit(0 if status != "failed" else 1)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result_record("failed", error)
        sys.exit(1)


if __name__ == "__main__":
    main()
