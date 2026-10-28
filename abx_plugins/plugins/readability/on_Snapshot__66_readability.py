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
      Runs after live browser hooks and background cleanup so archived image
      bodies exist before the article decides which images it can preserve.
"""

import sys
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import ExitStack
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

from abx_plugins.plugins.base.utils import (
    load_config,
    emit_archive_result_record,
    has_staticfile_output,
    is_non_html_document,
    write_text_atomic,
    find_article_html_source,
    preserve_article_image_dimensions,
    read_article_html,
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

    # Repeated image URLs in this document share one validated alias. Keep
    # this local to the render so no filesystem decisions survive a capture.
    linked_images: dict[tuple[str, ...], str] = {}

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
        key = (parsed.hostname, *path_parts)
        archived = linked_images.get(key)
        if archived is None:
            for root in ("responses/image", "responses", "wget"):
                candidate = SNAP_DIR / root / parsed.hostname / Path(*path_parts)
                # Most fallback paths are absent. Probe existence before paying
                # for resolve's ancestor lookups, but do not read or link the
                # file until its target passes the snapshot boundary below.
                if not candidate.is_file():
                    continue
                try:
                    resolved = candidate.resolve()
                except (OSError, RuntimeError):
                    continue
                # Responses may point into wget or another sibling plugin.
                # Constrain the final target to the whole snapshot, excluding
                # private browser state, instead of rejecting those aliases.
                if not resolved.is_relative_to(SNAP_DIR) or any(
                    part.casefold() == ".persona"
                    for part in resolved.relative_to(SNAP_DIR).parts
                ):
                    continue
                if resolved.is_file():
                    link = output_dir / "images" / parsed.hostname / Path(*path_parts)
                    try:
                        link.parent.mkdir(parents=True, exist_ok=True)
                        target = os.path.relpath(resolved, link.parent)
                        try:
                            link.symlink_to(target)
                        except FileExistsError:
                            # Repeated images share a link. Compare its stored target
                            # without resolving every remote filesystem component.
                            if os.readlink(link) != target:
                                link.unlink()
                                link.symlink_to(target)
                    except OSError:
                        return ""
                    archived = f"./{link.relative_to(output_dir).as_posix()}"
                    linked_images[key] = archived
                    break
        if archived is None:
            return ""
        tag = f"{tag[: src_match.start(2)]}{archived}{tag[src_match.end(2) :]}"
        return re.sub(r'\s+srcset\s*=\s*(["\']).*?\1', "", tag, flags=re.I)

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
a {{ color: #0369a1 }} img {{ max-width: 100%; vertical-align: middle }} svg {{ max-width: 100% }} svg:not([height]), video {{ max-width: 100%; height: auto }}
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


def primary_article_html(source: str) -> str:
    """Exclude separately headed articles appended to a known primary article.

    Exactly one H1 must agree with explicit Article JSON-LD. Other H1s must
    belong to bounded articles, not ambiguous page-level headings. Without that
    evidence, leave selection to Readability. Keep the primary article's nested
    sections and unheaded blocks, which may continue the same story.
    """
    if "application/ld+json" not in source:
        return source
    offsets = [0, *(match.end() for match in re.finditer("\n", source))]

    class Articles(HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = 0
            self.start = 0
            self.articles = []
            self.headings = []
            self.heading = None
            self.script = None
            self.headlines = set()

        def position(self):
            line, column = self.getpos()
            return offsets[line - 1] + column

        def handle_starttag(self, tag, attrs):
            if tag == "article":
                if not self.depth:
                    self.start = self.position()
                self.depth += 1
            if tag in {"h1", "h2"}:
                self.heading = [tag, self.position(), ""]
            if tag == "script" and dict(attrs).get("type") == "application/ld+json":
                self.script = ""

        def handle_data(self, data):
            if self.heading is not None:
                self.heading[2] += data
            if self.script is not None:
                self.script += data

        def handle_endtag(self, tag):
            if tag == "article" and self.depth:
                self.depth -= 1
                if not self.depth:
                    self.articles.append(
                        (self.start, source.index(">", self.position()) + 1),
                    )
            if self.heading is not None and tag == self.heading[0]:
                self.headings.append(tuple(self.heading))
                self.heading = None
            if tag == "script" and self.script is not None:
                try:
                    data = json.loads(self.script)
                    nodes = data if isinstance(data, list) else [data]
                    for node in nodes:
                        if not isinstance(node, dict):
                            continue
                        # JSON-LD commonly wraps Article nodes in @graph. Match
                        # infiniscroll's metadata handling, including headlines
                        # containing HTML entities rather than literal glyphs.
                        if isinstance(node.get("@graph"), list):
                            nodes.extend(node["@graph"])
                        types = node.get("@type", [])
                        types = [types] if isinstance(types, str) else types
                        if not isinstance(types, list) or not all(
                            isinstance(t, str) for t in types
                        ):
                            continue
                        if any(
                            t in {"Article", "NewsArticle", "BlogPosting"}
                            for t in types
                        ):
                            headline = node.get("headline")
                            if isinstance(headline, str):
                                self.headlines.add(
                                    " ".join(html.unescape(headline).split()),
                                )
                except (ValueError, TypeError):
                    pass
                self.script = None

    parser = Articles()
    parser.feed(source)
    primary = [
        h
        for h in parser.headings
        if h[0] == "h1" and " ".join(h[2].split()) in parser.headlines
    ]
    if parser.depth or len(primary) != 1:
        return source
    _, primary_start, primary_title = primary[0]
    # Appended stories legitimately have their own H1. A heading outside any
    # complete article has no safe removal boundary, so keep native selection.
    if any(
        tag == "h1"
        and position != primary_start
        and not any(start <= position < end for start, end in parser.articles)
        for tag, position, _ in parser.headings
    ):
        return source
    primary_title = " ".join(primary_title.split())
    chunks = []
    cursor = 0
    for start, end in parser.articles:
        headings = [
            " ".join(text.split())
            for _, position, text in parser.headings
            if start <= position < end
        ]
        if start <= primary_start < end or not headings or primary_title in headings:
            continue
        chunks.append(source[cursor:start])
        cursor = end
    return "".join([*chunks, source[cursor:]])


def extract_readability(url: str, binary: str) -> tuple[str, str]:
    """
    Extract article using Readability.

    Returns: (success, output_path, error_message)
    """
    started_at = time.monotonic()
    if has_staticfile_output():
        return "noresults", "staticfile already handled"
    if is_non_html_document():
        return "noresults", "Browser document is not HTML"

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
        source_html, cleaned_source = read_article_html(html_source)
        print(
            f"[readability] phase=source-read elapsed_ms={(time.monotonic() - started_at) * 1000:.0f}",
            file=sys.stderr,
        )
        scoped_html = primary_article_html(source_html)
        with ExitStack() as files:
            if cleaned_source or scoped_html != source_html:
                scoped = files.enter_context(
                    tempfile.NamedTemporaryFile(
                        mode="w+",
                        suffix=".html",
                        encoding="utf-8",
                    ),
                )
                scoped.write(scoped_html)
                scoped.flush()
                cmd[-3] = scoped.name
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                timeout=max(1, timeout - 5),
                text=True,
            )

        # Article HTML/text already have saved artifacts. Keep successful logs
        # small; these phase timings distinguish extraction from filesystem work
        # when a hook's wall time is much longer than its native parser runtime.
        print(
            f"[readability] phase=extractor-finished elapsed_ms={(time.monotonic() - started_at) * 1000:.0f}",
            file=sys.stderr,
        )
        if result.returncode != 0 and result.stdout:
            sys.stderr.write(result.stdout)
            sys.stderr.flush()

        if result.returncode < 0:
            return (
                "failed",
                f"Readability extractor terminated by signal {-result.returncode}",
            )
        if result.returncode != 0:
            return "noresults", "No content extracted"

        # Parse JSON output
        try:
            result_json = json.loads(result.stdout)
        except json.JSONDecodeError:
            sys.stderr.write(result.stdout)
            sys.stderr.flush()
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

        html_content = preserve_article_image_dimensions(
            html_content,
            source_html,
            url,
        )
        html_content = render_readability_document(
            html_content,
            result_json,
            url,
            output_dir,
        )
        write_text_atomic(output_dir / OUTPUT_FILE, html_content)
        write_text_atomic(output_dir / TEXT_FILE, text_content)
        write_text_atomic(output_dir / METADATA_FILE, json.dumps(result_json, indent=2))
        print(
            f"[readability] phase=outputs-saved elapsed_ms={(time.monotonic() - started_at) * 1000:.0f}",
            file=sys.stderr,
        )

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
