#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12,<3.14"
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
import argparse
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    write_text_atomic,
)


# Extractor metadata
PLUGIN_NAME = "mercury"
BIN_NAME = "postlight-parser"
BIN_PROVIDERS = "env,pnpm"
PLUGIN_DIR = Path(__file__).resolve().parent.name
HTML_FILE = "content.html"
TEXT_FILE = "content.txt"
METADATA_FILE = "article.json"


@dataclass(frozen=True)
class MercuryConfig:
    SNAP_DIR: str
    MERCURY_ENABLED: bool
    MERCURY_BINARY: str
    MERCURY_TIMEOUT: int
    MERCURY_ARGS: list[str]
    MERCURY_ARGS_EXTRA: list[str]


def parse_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def parse_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_args_env(value: str | None) -> list[str]:
    if value is None or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return shlex.split(value)
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    if isinstance(parsed, str):
        return shlex.split(parsed)
    return []


def load_mercury_config(environ: dict[str, str] | None = None) -> MercuryConfig:
    env = environ or os.environ
    timeout = parse_int(env.get("TIMEOUT"), 30)
    return MercuryConfig(
        SNAP_DIR=env.get("SNAP_DIR") or ".",
        MERCURY_ENABLED=parse_bool(
            env.get("MERCURY_ENABLED")
            or env.get("SAVE_MERCURY")
            or env.get("USE_MERCURY"),
            True,
        ),
        MERCURY_BINARY=env.get("MERCURY_BINARY") or "postlight-parser",
        MERCURY_TIMEOUT=parse_int(env.get("MERCURY_TIMEOUT"), timeout),
        MERCURY_ARGS=parse_args_env(
            env.get("MERCURY_ARGS") or env.get("MERCURY_DEFAULT_ARGS"),
        ),
        MERCURY_ARGS_EXTRA=parse_args_env(
            env.get("MERCURY_ARGS_EXTRA") or env.get("MERCURY_EXTRA_ARGS"),
        ),
    )


def extract_mercury(url: str, config, output_dir: Path) -> tuple[str, str]:
    """
    Extract article using Mercury Parser.

    Returns: (success, output_path, error_message)
    """
    timeout = config.MERCURY_TIMEOUT
    mercury_args = config.MERCURY_ARGS
    mercury_args_extra = config.MERCURY_ARGS_EXTRA
    binary = config.MERCURY_BINARY

    try:
        cmd_html = [binary, *mercury_args, *mercury_args_extra, url, "--format=html"]
        result_html = subprocess.run(
            cmd_html,
            capture_output=True,
            timeout=timeout,
            text=True,
        )
        if result_html.stdout:
            sys.stderr.write(result_html.stdout)
            sys.stderr.flush()
        if result_html.stderr:
            sys.stderr.write(result_html.stderr)
            sys.stderr.flush()
        if result_html.returncode != 0:
            return "failed", f"postlight-parser failed (exit={result_html.returncode})"

        try:
            html_json = json.loads(result_html.stdout)
        except json.JSONDecodeError:
            return "failed", "postlight-parser returned invalid JSON"

        if html_json.get("failed"):
            return "noresults", "Mercury was not able to extract article"

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

        text_content = " ".join(
            re.sub(r"<[^>]+>", " ", html.unescape(html_content)).split(),
        )
        if not text_content:
            text_content = str(html_json.get("excerpt") or html_json.get("title") or "")
        write_text_atomic(output_dir / TEXT_FILE, text_content)

        # Save article metadata
        metadata = {k: v for k, v in html_json.items() if k != "content"}
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


def main():
    """Extract article content using Postlight's Mercury Parser."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="URL to extract article from")
    args, _unknown = parser.parse_known_args()

    try:
        config = load_mercury_config()
        output_dir = Path(config.SNAP_DIR or ".").resolve() / PLUGIN_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(output_dir)

        # Check if mercury extraction is enabled
        if not config.MERCURY_ENABLED:
            print("Skipping mercury (MERCURY_ENABLED=False)", file=sys.stderr)
            emit_archive_result_record("skipped", "MERCURY_ENABLED=False")
            sys.exit(0)

        # Run extraction
        status, output = extract_mercury(args.url, config, output_dir)
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
