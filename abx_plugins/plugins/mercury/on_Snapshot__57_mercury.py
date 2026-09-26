#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
#
# Extract article content using Postlight's Mercury Parser.
# Creates content.html, content.txt, and article.json files from the extracted article.
#
# Usage:
#     ./on_Snapshot__57_mercury.py [...] > events.jsonl

import sys
import html
import json
import os
import argparse
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from abx_plugins.plugins.base.utils import (
    find_article_html_source,
    preserve_article_image_dimensions,
    emit_archive_result_record,
    has_staticfile_output,
    is_non_html_document,
    write_text_atomic,
)


# Extractor metadata
PLUGIN_NAME = "mercury"
BIN_NAME = "postlight-parser"
BIN_PROVIDERS = "env,npm"
PLUGIN_DIR = Path(__file__).resolve().parent.name
HTML_FILE = "content.html"
TEXT_FILE = "content.txt"
METADATA_FILE = "article.json"

_PARSER_API_SCRIPT = r"""
const fs = require('fs');
const legacyUrl = require('url');
const cheerio = require('cheerio');
const Parser = require(process.argv[1]);
const pageUrl = process.argv[2];
const html = fs.readFileSync(0, 'utf8');
const $ = cheerio.load(html);
const removed = { href: 0, src: 0, srcset: 0 };

let baseUrl = $('base').first().attr('href') || pageUrl;
try {
    legacyUrl.resolve(pageUrl, baseUrl);
} catch (_error) {
    $('base').first().removeAttr('href');
    baseUrl = pageUrl;
}

// Match Postlight 2.2.3's per-attribute URL.resolve passes. Drop only an
// attribute that would throw; keep its element and all article text.
for (const attr of ['href', 'src']) {
    const resolutionBase = $('base').first().attr('href') || pageUrl;
    $(`[${attr}]`).each((_index, element) => {
        const node = $(element);
        const value = node.attr(attr);
        if (!value) return;
        try {
            legacyUrl.resolve(resolutionBase, value);
        } catch (_error) {
            node.removeAttr(attr);
            removed[attr] += 1;
        }
    });
}

// Postlight resolves each srcset candidate against the page URL. Use its
// candidate splitter and remove only the attribute if that call would throw.
$('[srcset]').each((_index, element) => {
    const node = $(element);
    const value = node.attr('srcset');
    const candidates = value && value.match(/(?:\s*)(\S+(?:\s*[\d.]+[wx])?)(?:\s*,\s*)?/g);
    if (!candidates) return;
    try {
        for (const candidate of candidates) {
            const parts = candidate.trim().replace(/,$/, '').split(/\s+/);
            legacyUrl.resolve(pageUrl, parts[0]);
        }
    } catch (_error) {
        node.removeAttr('srcset');
        removed.srcset += 1;
    }
});

(async () => {
    const result = await Parser.parse(pageUrl, {
        html: $.html(),
        contentType: 'html',
        fetchAllPages: false,
    });
    if (Object.values(removed).some(count => count > 0)) {
        console.error(`Removed unresolvable URL attributes: ${JSON.stringify(removed)}`);
    }
    process.stdout.write(JSON.stringify(result));
})().catch(error => {
    console.error(error && error.stack ? error.stack : error);
    process.exitCode = 1;
});
"""


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
        MERCURY_BINARY=env.get("MERCURY_BINARY", ""),
        MERCURY_TIMEOUT=parse_int(env.get("MERCURY_TIMEOUT"), timeout),
        MERCURY_ARGS=parse_args_env(
            env.get("MERCURY_ARGS") or env.get("MERCURY_DEFAULT_ARGS"),
        ),
        MERCURY_ARGS_EXTRA=parse_args_env(
            env.get("MERCURY_ARGS_EXTRA") or env.get("MERCURY_EXTRA_ARGS"),
        ),
    )


def parse_captured_html(
    url: str,
    source_html: str,
    binary: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run the installed Postlight API against the already captured DOM."""
    parser_module = Path(binary).resolve().parent / "dist" / "mercury.js"
    node = shutil.which("node")
    if not parser_module.is_file() or not node:
        missing = (
            "Postlight parser module" if not parser_module.is_file() else "Node.js"
        )
        raise RuntimeError(f"{missing} is unavailable for captured HTML parsing")
    return subprocess.run(
        [node, "-e", _PARSER_API_SCRIPT, str(parser_module), url],
        capture_output=True,
        timeout=timeout,
        text=True,
        input=source_html,
        cwd=parser_module.parent,
    )


def extract_mercury(url: str, config, output_dir: Path) -> tuple[str, str]:
    """
    Extract article using Mercury Parser.

    Returns: (status, output_path_or_error)
    """
    if has_staticfile_output():
        return "noresults", "staticfile already handled"
    if is_non_html_document():
        return "noresults", "Browser document is not HTML"

    timeout = config.MERCURY_TIMEOUT
    mercury_args = config.MERCURY_ARGS
    mercury_args_extra = config.MERCURY_ARGS_EXTRA
    binary = config.MERCURY_BINARY

    try:
        deadline = time.monotonic() + timeout
        # CLI-specific arguments keep their established behavior; the default
        # API path consumes the best already-captured HTML source when present.
        html_source_path = find_article_html_source()
        source = html_source_path if not (mercury_args or mercury_args_extra) else None
        if source:
            result_html = parse_captured_html(
                url,
                Path(source).read_text(encoding="utf-8", errors="replace"),
                binary,
                timeout,
            )
        else:
            cmd_html = [
                binary,
                *mercury_args,
                *mercury_args_extra,
                url,
                "--format=html",
            ]
            result_html = subprocess.run(
                cmd_html,
                capture_output=True,
                timeout=timeout,
                text=True,
            )
        # Postlight 2.2.3 can parse the requested page, then crash when a
        # linked page fails to load: _collectAllPages calls $.html() on the
        # error object. Keep the requested page using its official API. A
        # configured CLI invocation keeps its own arguments and failure.
        if (
            result_html.returncode == 1
            and not mercury_args
            and not mercury_args_extra
            and "TypeError: $.html is not a function" in result_html.stderr
            and "dist/mercury.js:8012:" in result_html.stderr
        ):
            parser_module = Path(binary).resolve().parent / "dist" / "mercury.js"
            node = shutil.which("node")
            if parser_module.is_file() and node:
                fallback = subprocess.run(
                    [
                        node,
                        "-e",
                        "const Parser = require(process.argv[1]); "
                        "Parser.parse(process.argv[2], {contentType: 'html', fetchAllPages: false})"
                        ".then(result => console.log(JSON.stringify(result)))"
                        ".catch(error => { console.error(error); process.exit(1); });",
                        str(parser_module),
                        url,
                    ],
                    capture_output=True,
                    timeout=max(deadline - time.monotonic(), 0),
                    text=True,
                )
                if fallback.returncode == 0:
                    try:
                        first_page = json.loads(fallback.stdout)
                    except json.JSONDecodeError:
                        first_page = None
                    if isinstance(first_page, dict) and not first_page.get("failed"):
                        print(
                            "Mercury could not load a linked page; saved the requested page only.",
                            file=sys.stderr,
                        )
                        result_html = fallback
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

        if html_json.get("error"):
            return "failed", str(
                html_json.get("message") or "postlight-parser returned an error",
            )
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
        if html_source_path:
            html_content = preserve_article_image_dimensions(
                html_content,
                Path(html_source_path).read_text(encoding="utf-8", errors="replace"),
                url,
            )
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

        mercury_binary = Path(config.MERCURY_BINARY)
        if not mercury_binary.is_absolute() or not mercury_binary.is_file():
            raise RuntimeError("MERCURY_BINARY was not resolved by abxpkg")

        # Run extraction
        print("Mercury extraction started", flush=True)
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
