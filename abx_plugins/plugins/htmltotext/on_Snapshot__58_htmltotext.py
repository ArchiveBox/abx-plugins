#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "rich-click",
# ]
# ///
#
# Convert HTML to plain text for search indexing.
# Reads HTML from other extractors (wget, singlefile, dom) and converts to plain text for full-text search.
#
# Usage:
#     ./on_Snapshot__58_htmltotext.py --url=<url> --snapshot-id=<snapshot-id> > events.jsonl

import json
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from base.utils import emit_archive_result, write_text_atomic

import rich_click as click


# Extractor metadata
PLUGIN_NAME = "htmltotext"
PLUGIN_DIR = Path(__file__).resolve().parent.name
SNAP_DIR = Path(os.environ.get("SNAP_DIR", ".")).resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
OUTPUT_FILE = "htmltotext.txt"


class HTMLTextExtractor(HTMLParser):
    """Extract text content from HTML, ignoring scripts/styles."""

    def __init__(self):
        super().__init__()
        self.result = []
        self.skip_tags = {"script", "style", "head", "meta", "link", "noscript"}
        self.current_tag = None

    def handle_starttag(self, tag, attrs):
        self.current_tag = tag.lower()

    def handle_endtag(self, tag):
        self.current_tag = None

    def handle_data(self, data):
        if self.current_tag not in self.skip_tags:
            text = data.strip()
            if text:
                self.result.append(text)

    def get_text(self) -> str:
        return " ".join(self.result)


def html_to_text(html: str) -> str:
    """Convert HTML to plain text."""
    parser = HTMLTextExtractor()
    try:
        parser.feed(html)
        return parser.get_text()
    except Exception:
        # Fallback: strip HTML tags with regex
        text = re.sub(
            r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE
        )
        text = re.sub(
            r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE
        )
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()


def find_html_source() -> str | None:
    """Find HTML content from other extractors in the snapshot directory."""
    # Hooks run in snapshot_dir, sibling extractor outputs are in subdirectories
    search_patterns = [
        "singlefile/singlefile.html",
        "*_singlefile/singlefile.html",
        "singlefile/*.html",
        "*_singlefile/*.html",
        "dom/output.html",
        "*_dom/output.html",
        "dom/*.html",
        "*_dom/*.html",
        "wget/**/*.html",
        "*_wget/**/*.html",
        "wget/**/*.htm",
        "*_wget/**/*.htm",
    ]

    for base in (Path.cwd(), Path.cwd().parent):
        for pattern in search_patterns:
            matches = list(base.glob(pattern))
            for match in matches:
                if match.is_file() and match.stat().st_size > 0:
                    try:
                        return match.read_text(errors="ignore")
                    except Exception:
                        continue

    return None


def extract_htmltotext(url: str) -> tuple[str, str]:
    """
    Extract plain text from HTML sources.

    Returns: (success, output_path, error_message)
    """
    # Find HTML source from other extractors
    html_content = find_html_source()
    if not html_content:
        return "noresults", "No HTML source found"

    # Convert HTML to text
    text = html_to_text(html_content)

    if not text or len(text) < 10:
        return "noresults", "No meaningful text extracted"

    # Output directory is current directory (hook already runs in output dir)
    output_dir = Path(OUTPUT_DIR)
    output_path = output_dir / OUTPUT_FILE
    write_text_atomic(output_path, text)

    return "succeeded", OUTPUT_FILE


@click.command()
@click.option("--url", required=True, help="URL that was archived")
@click.option("--snapshot-id", required=True, help="Snapshot UUID")
def main(url: str, snapshot_id: str):
    """Convert HTML to plain text for search indexing."""

    try:
        # Run extraction
        status, output = extract_htmltotext(url)
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
