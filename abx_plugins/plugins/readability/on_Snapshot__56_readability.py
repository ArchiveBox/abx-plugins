#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic-settings",
#     "rich-click",
# ]
# ///
"""
Extract article content using Mozilla's Readability.

Usage: on_Snapshot__readability.py --url=<url> --snapshot-id=<uuid>
Output: Creates readability/ directory with content.html, content.txt, article.json

Environment variables:
    READABILITY_BINARY: Path to readability-extractor binary
    READABILITY_TIMEOUT: Timeout in seconds (default: 60)
    READABILITY_ARGS: Default Readability arguments (JSON array)
    READABILITY_ARGS_EXTRA: Extra arguments to append (JSON array)
    TIMEOUT: Fallback timeout

Note: Requires readability-extractor from https://github.com/ArchiveBox/readability-extractor
      This extractor looks for HTML source from other extractors (wget, singlefile, dom)
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from base.utils import load_config, emit_archive_result, write_text_atomic, find_html_source

from urllib.parse import urlparse

import rich_click as click


# Extractor metadata
PLUGIN_NAME = "readability"
BIN_NAME = "readability-extractor"
BIN_PROVIDERS = "env,npm"
PLUGIN_DIR = Path(__file__).resolve().parent.name
SNAP_DIR = Path(os.environ.get("SNAP_DIR", ".")).resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
OUTPUT_FILE = "content.html"
TEXT_FILE = "content.txt"
METADATA_FILE = "article.json"



def extract_readability(url: str, binary: str) -> tuple[str, str]:
    """
    Extract article using Readability.

    Returns: (success, output_path, error_message)
    """
    config = load_config()
    timeout = config.READABILITY_TIMEOUT
    readability_args = config.READABILITY_ARGS
    readability_args_extra = config.READABILITY_ARGS_EXTRA

    # Find HTML source
    html_source = find_html_source()
    if not html_source:
        return "noresults", "No HTML source found"

    # Output directory is current directory (hook already runs in output dir)
    output_dir = Path(OUTPUT_DIR)

    try:
        # Run readability-extractor (outputs JSON by default)
        cmd = [binary, *readability_args, *readability_args_extra, html_source]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, timeout=timeout, text=True)

        if result.stdout:
            sys.stderr.write(result.stdout)
            sys.stderr.flush()

        if result.returncode != 0:
            return "failed", f"readability-extractor failed (exit={result.returncode})"

        # Parse JSON output
        try:
            result_json = json.loads(result.stdout)
        except json.JSONDecodeError:
            return "failed", "readability-extractor returned invalid JSON"

        # Extract and save content
        # readability-extractor uses camelCase field names (textContent, content)
        text_content = result_json.pop(
            "textContent", result_json.pop("text-content", "")
        )
        html_content = result_json.pop("content", result_json.pop("html-content", ""))

        if not text_content and not html_content:
            return "noresults", "No content extracted"

        write_text_atomic(output_dir / OUTPUT_FILE, html_content)
        write_text_atomic(output_dir / TEXT_FILE, text_content)
        write_text_atomic(
            output_dir / METADATA_FILE, json.dumps(result_json, indent=2)
        )

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
                            responses_images = None
                    if responses_images:
                        rel_target = os.path.relpath(
                            str(responses_images), str(output_dir)
                        )
                        link_path.symlink_to(rel_target)
        except Exception:
            pass

        return "succeeded", OUTPUT_FILE

    except subprocess.TimeoutExpired:
        return "failed", f"Timed out after {timeout} seconds"
    except Exception as e:
        return "failed", f"{type(e).__name__}: {e}"


@click.command()
@click.option("--url", required=True, help="URL to extract article from")
@click.option("--snapshot-id", required=True, help="Snapshot UUID")
def main(url: str, snapshot_id: str):
    """Extract article content using Mozilla's Readability."""

    try:
        config = load_config()
        # Get binary from environment
        binary = config.READABILITY_BINARY

        # Run extraction
        status, output = extract_readability(url, binary)
        if status == "failed":
            print(f"ERROR: {output}", file=sys.stderr)
        emit_archive_result(status, output)
        sys.exit(0 if status != "failed" else 1)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result("failed", error)
        sys.exit(1)


if __name__ == "__main__":
    main()
