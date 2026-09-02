#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""
Extract article content using Mozilla's Readability.

Usage: on_Snapshot__readability.py --url=<url>
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

import sys
import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from abx_plugins.plugins.base.utils import (
    load_config,
    emit_archive_result_record,
    write_text_atomic,
    find_article_html_source,
)

from urllib.parse import unquote, urljoin, urlparse

import rich_click as click


# Extractor metadata
PLUGIN_NAME = "readability"
BIN_NAME = "readability-extractor"
BIN_PROVIDERS = "env,pnpm"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
OUTPUT_FILE = "content.html"
TEXT_FILE = "content.txt"
METADATA_FILE = "article.json"


def link_archived_images(content: str, url: str, output_dir: Path) -> str:
    images_dir = output_dir / "images"
    if images_dir.is_symlink() or images_dir.is_file():
        images_dir.unlink()
    elif images_dir.is_dir():
        shutil.rmtree(images_dir)

    def replace(match: re.Match) -> str:
        tag = match.group(0)
        src_match = re.search(r'\bsrc\s*=\s*(["\'])([^"\']+)\1', tag, flags=re.I)
        if not src_match:
            return ""
        source = html.unescape(src_match.group(2))
        parsed = urlparse(urljoin(url, source))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        path_parts = tuple(
            part for part in PurePosixPath(unquote(parsed.path)).parts if part != "/"
        )
        if not path_parts or any(part in {".", ".."} for part in path_parts):
            return ""
        for root in ("responses/image", "responses", "wget"):
            candidate = SNAP_DIR / root / parsed.hostname / Path(*path_parts)
            if candidate.is_file():
                link = output_dir / "images" / parsed.hostname / Path(*path_parts)
                try:
                    link.parent.mkdir(parents=True, exist_ok=True)
                    if link.exists() and not link.is_symlink():
                        return ""
                    if link.is_symlink() and link.resolve() != candidate.resolve():
                        link.unlink()
                    if not link.exists():
                        link.symlink_to(os.path.relpath(candidate, link.parent))
                except OSError:
                    return ""
                archived = f"./{link.relative_to(output_dir).as_posix()}"
                tag = f"{tag[: src_match.start(2)]}{archived}{tag[src_match.end(2) :]}"
                return re.sub(r'\s+srcset\s*=\s*(["\']).*?\1', "", tag, flags=re.I)
        return ""

    content = re.sub(
        r"<img\b[^>]*>",
        replace,
        content,
        flags=re.I,
    )
    content = re.sub(r"<source\b[^>]*>", "", content, flags=re.I)
    for tag in ("picture", "a", "figure", "p"):
        content = re.sub(rf"<{tag}\b[^>]*>\s*</{tag}>", "", content, flags=re.I)
    return content


def render_readability_document(
    content: str,
    metadata: dict,
    url: str,
    output_dir: Path,
) -> str:
    title = html.escape(str(metadata.get("title") or ""))
    content = link_archived_images(content, url, output_dir)
    return f'''<!doctype html>
<html lang="{html.escape(str(metadata.get("lang") or "en"), quote=True)}"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
* {{ box-sizing: border-box }}
html {{ min-height: 100%; background: #fff }}
body {{ min-height: 100vh; margin: 0; color: #1f2937; background: #fff }}
main {{ width: 100%; min-height: 100vh; margin: 0; padding: clamp(1.5rem, 4vw, 4rem) clamp(1rem, 5vw, 4.5rem) 6rem; background: #fff }}
article {{ width: min(100%, 72rem); margin: 0 auto; font: 1.15rem/1.72 Georgia, 'Times New Roman', serif }}
article > :first-child {{ margin-top: 0 }}
h2, h3, h4 {{ margin: 2em 0 .65em; line-height: 1.25 }} p, ul, ol, blockquote {{ margin: 0 0 1.25em }}
a {{ color: #0369a1 }} img, svg, video {{ display: block; max-width: 100%; height: auto; margin: 1.5rem auto }}
article > *, article section, article div, article figure {{ max-width: 100% !important }}
article table {{ width: 100% !important; max-width: 100% !important; border-collapse: collapse }}
article td, article th {{ max-width: 100%; vertical-align: top }}
article table[width], article table[style*="width"] {{ width: 100% !important }}
article :is(tbody, thead, tfoot, tr)[width], article :is(tbody, thead, tfoot, tr)[style*="width"] {{ width: 100% !important }}
article :is(div, section, article, main, figure, p, td, th)[width],
article :is(div, section, article, main, figure, p, td, th)[style*="width"] {{ width: auto !important; max-width: 100% !important }}
blockquote {{ padding-left: 1.25rem; border-left: 4px solid #cbd5e1; color: #475569 }} pre, table {{ max-width: 100%; overflow: auto }}
@media (max-width: 40rem) {{ main {{ padding: 1.5rem 1rem 4rem }} h1 {{ font-size: 1.9rem }} article {{ font-size: 1.05rem }} }}
</style></head><body><main><article>{content}</article></main></body></html>'''


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
    html_source = find_article_html_source()
    if not html_source:
        return "noresults", "No HTML source found"

    # Output directory is current directory (hook already runs in output dir)
    output_dir = Path(OUTPUT_DIR)

    try:
        # Run readability-extractor (outputs JSON by default)
        cmd = [
            binary,
            *readability_args,
            *readability_args_extra,
            html_source,
            url,
            "utf-8",
        ]
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            timeout=max(1, timeout - 5),
            text=True,
        )

        if result.stdout:
            sys.stderr.write(result.stdout)
            sys.stderr.flush()

        if result.returncode != 0:
            return "noresults", "No content extracted"

        # Parse JSON output
        try:
            result_json = json.loads(result.stdout)
        except json.JSONDecodeError:
            return "noresults", "No content extracted"

        # Extract and save content
        # readability-extractor uses camelCase field names (textContent, content)
        text_content = result_json.pop(
            "textContent",
            result_json.pop("text-content", ""),
        )
        html_content = result_json.pop("content", result_json.pop("html-content", ""))

        if not text_content and not html_content:
            return "noresults", "No content extracted"

        html_content = render_readability_document(
            html_content,
            result_json,
            url,
            output_dir,
        )
        write_text_atomic(output_dir / OUTPUT_FILE, html_content)
        write_text_atomic(output_dir / TEXT_FILE, text_content)
        write_text_atomic(output_dir / METADATA_FILE, json.dumps(result_json, indent=2))

        return "succeeded", f"{PLUGIN_DIR}/{OUTPUT_FILE}"

    except subprocess.TimeoutExpired:
        return "noresults", "No content extracted"
    except Exception as e:
        return "failed", f"{type(e).__name__}: {e}"


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="URL to extract article from")
def main(url: str):
    """Extract article content using Mozilla's Readability."""

    try:
        config = load_config()

        if not config.READABILITY_ENABLED:
            print("Skipping readability (READABILITY_ENABLED=False)", file=sys.stderr)
            emit_archive_result_record("skipped", "READABILITY_ENABLED=False")
            sys.exit(0)

        # Get binary from environment
        binary = config.READABILITY_BINARY

        # Run extraction
        print("Readability extraction started", flush=True)
        status, output = extract_readability(url, binary)
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
