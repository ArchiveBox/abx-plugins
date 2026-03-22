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
# Extract article content using Postlight's Mercury Parser.
# Creates content.html, content.txt, and article.json files from the extracted article.
#
# Usage:
#     ./on_Snapshot__57_mercury.py [...] > events.jsonl

import html
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from abx_plugins.plugins.base.utils import (
    load_config,
    emit_archive_result_record,
    write_text_atomic,
)

import rich_click as click


# Extractor metadata
PLUGIN_NAME = "mercury"
BIN_NAME = "postlight-parser"
BIN_PROVIDERS = "env,npm"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
HTML_FILE = "content.html"
TEXT_FILE = "content.txt"
METADATA_FILE = "article.json"


def extract_mercury(url: str, binary: str) -> tuple[str, str]:
    """
    Extract article using Mercury Parser.

    Returns: (success, output_path, error_message)
    """
    config = load_config()
    timeout = config.MERCURY_TIMEOUT
    mercury_args = config.MERCURY_ARGS
    mercury_args_extra = config.MERCURY_ARGS_EXTRA

    # Output directory is current directory (hook already runs in output dir)
    output_dir = Path(OUTPUT_DIR)

    try:
        # Get text version
        cmd_text = [binary, *mercury_args, *mercury_args_extra, url, "--format=text"]
        result_text = subprocess.run(
            cmd_text,
            stdout=subprocess.PIPE,
            timeout=timeout,
            text=True,
        )
        if result_text.stdout:
            sys.stderr.write(result_text.stdout)
            sys.stderr.flush()

        if result_text.returncode != 0:
            return "failed", f"postlight-parser failed (exit={result_text.returncode})"

        try:
            text_json = json.loads(result_text.stdout)
        except json.JSONDecodeError:
            return "failed", "postlight-parser returned invalid JSON"

        if text_json.get("failed"):
            return "noresults", "Mercury was not able to extract article"

        # Save text content
        text_content = text_json.get("content", "")
        write_text_atomic(output_dir / TEXT_FILE, text_content)

        # Get HTML version
        cmd_html = [binary, *mercury_args, *mercury_args_extra, url, "--format=html"]
        result_html = subprocess.run(
            cmd_html,
            stdout=subprocess.PIPE,
            timeout=timeout,
            text=True,
        )
        if result_html.stdout:
            sys.stderr.write(result_html.stdout)
            sys.stderr.flush()

        try:
            html_json = json.loads(result_html.stdout)
        except json.JSONDecodeError:
            html_json = {}

        # Save HTML content and metadata
        html_content = html_json.pop("content", "")
        # Some sources return HTML-escaped markup inside the content blob.
        # If it looks heavily escaped, unescape once so it renders properly.
        if html_content:
            escaped_count = html_content.count("&lt;") + html_content.count("&gt;")
            tag_count = html_content.count("<")
            if escaped_count and escaped_count > tag_count * 2:
                html_content = html.unescape(html_content)
        write_text_atomic(output_dir / HTML_FILE, html_content)

        # Save article metadata
        metadata = {k: v for k, v in text_json.items() if k != "content"}
        write_text_atomic(output_dir / METADATA_FILE, json.dumps(metadata, indent=2))

        # Link images/ to responses capture (if available)
        try:
            hostname = urlparse(url).hostname or ""
            if hostname:
                responses_images = (
                    output_dir / ".." / "responses" / "image" / hostname / "images"
                ).resolve()
                link_path = output_dir / "images"
                if responses_images.exists() and responses_images.is_dir():
                    if link_path.exists() or link_path.is_symlink():
                        if link_path.is_symlink() or link_path.is_file():
                            link_path.unlink()
                        else:
                            # Don't remove real directories
                            responses_images = None
                    if responses_images:
                        rel_target = os.path.relpath(
                            str(responses_images),
                            str(output_dir),
                        )
                        link_path.symlink_to(rel_target)
        except Exception:
            pass

        return "succeeded", f"{PLUGIN_DIR}/{HTML_FILE}"

    except subprocess.TimeoutExpired:
        return "failed", f"Timed out after {timeout} seconds"
    except Exception as e:
        return "failed", f"{type(e).__name__}: {e}"


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="URL to extract article from")
def main(url: str):
    """Extract article content using Postlight's Mercury Parser."""

    try:
        config = load_config()

        # Check if mercury extraction is enabled
        if not config.MERCURY_ENABLED:
            print("Skipping mercury (MERCURY_ENABLED=False)", file=sys.stderr)
            emit_archive_result_record("skipped", "MERCURY_ENABLED=False")
            sys.exit(0)

        # Get binary from environment
        binary = config.MERCURY_BINARY

        # Run extraction
        status, output = extract_mercury(url, binary)
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
