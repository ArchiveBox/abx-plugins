#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
# ruff: noqa: E402
"""
Download scientific papers from a URL or its article citations using papers-dl.

Usage: on_Snapshot__papersdl.py --url=<url>
Output: Downloads paper PDFs to $PWD/

Environment variables:
    PAPERSDL_BINARY: Path to papers-dl binary
    PAPERSDL_TIMEOUT: Timeout in seconds (default: 300 for paper downloads)
    PAPERSDL_ARGS: Default papers-dl arguments (JSON array, default: ["fetch"])
    PAPERSDL_ARGS_EXTRA: Extra arguments to append (JSON array)

    # papers-dl feature toggles
    SAVE_PAPERSDL: Enable papers-dl paper extraction (default: True)

    # Fallback to ARCHIVING_CONFIG values if PAPERSDL_* not set:
    TIMEOUT: Fallback timeout
"""

import signal
import sys

# Snapshot cleanup sends SIGTERM to the whole hook process group as the polite
# shutdown signal before the hard SIGKILL deadline. This hook is a finite
# downloader, so treating SIGTERM as "stop now" corrupts the normal contract:
# an in-flight download becomes a failed ArchiveResult even though cleanup would
# have allowed it to finish within the hook timeout. Installing SIG_IGN before
# any heavy imports or subprocess creation also makes downloader children inherit
# the same disposition across exec, so the whole process group either finishes
# its work and exits normally or is stopped by the later SIGKILL deadline.
signal.signal(signal.SIGTERM, signal.SIG_IGN)

if any(arg == "--url" or arg.startswith("--url=") for arg in sys.argv[1:]):
    print("papers-dl search started", flush=True)

import os
import json
import re
import subprocess
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    find_article_html_source,
    is_non_html_document,
    load_config,
)

import rich_click as click


# Extractor metadata
PLUGIN_NAME = "papersdl"
BIN_NAME = "papers-dl"
BIN_PROVIDERS = "env,pip"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
ARXIV_ID = r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]+)?/\d{7})(?:v\d+)?"


def extract_doi_from_url(url: str) -> str | None:
    """Extract DOI from common paper URLs."""
    # Match DOI pattern in URL
    doi_pattern = r"10\.\d{4,}/[^\s]+"
    match = re.search(doi_pattern, unquote(urlsplit(url).path))
    if match:
        return match.group(0)
    return None


def extract_arxiv_id_from_doi(doi: str) -> str | None:
    """Extract arXiv identifier from arXiv DOI format."""
    match = re.fullmatch(rf"10\.48550/arXiv\.({ARXIV_ID})", doi, re.IGNORECASE)
    if not match:
        return None
    return match.group(1)


def extract_arxiv_id_from_url(url: str) -> str | None:
    """Extract arXiv identifier from common arxiv.org URL paths."""
    match = re.search(
        rf"arxiv\.org/(?:abs|pdf|html)/({ARXIV_ID})",
        unquote(url),
        re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1)


class PaperReferences(HTMLParser):
    """Collect article text and citation links without script/style contents."""

    def __init__(self):
        super().__init__()
        self.references: list[str] = []
        self.text: list[str] = []
        self.ignored_tag: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored_tag = tag
        if self.ignored_tag:
            return
        attrs = dict(attrs)
        href = attrs.get("href")
        if tag in {"a", "link"} and href:
            self.references.append(unquote(href))
        if tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            value = attrs.get("content") or ""
            if name == "citation_arxiv_id":
                self.references.append(f"arXiv:{value}")
            elif name in {
                "citation_doi",
                "dc.identifier",
                "dc.identifier.doi",
                "prism.doi",
            }:
                self.references.append(value)
        if tag in {"p", "div", "li", "br", "td", "h1", "h2", "h3"}:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag == self.ignored_tag:
            self.ignored_tag = None
        if not self.ignored_tag and tag in {"p", "div", "li", "td", "h1", "h2", "h3"}:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.ignored_tag:
            self.text.append(data)


def normalize_identifier(identifier: str) -> str:
    """Remove citation punctuation and unify arXiv DOI/URL/text references."""
    identifier = identifier.strip().rstrip(".,;:")
    while identifier.endswith(")") and identifier.count(")") > identifier.count("("):
        identifier = identifier[:-1].rstrip(".,;:")
    arxiv_id = extract_arxiv_id_from_doi(identifier)
    if arxiv_id:
        return f"arXiv:{arxiv_id.lower()}"
    return identifier.lower()


def find_paper_identifiers(url: str, binary: str, deadline: float) -> list[str]:
    """Keep direct paper URLs fast; otherwise discover citations in article HTML."""
    doi = extract_doi_from_url(url)
    arxiv_id = extract_arxiv_id_from_url(url)
    if doi or arxiv_id:
        return [normalize_identifier(doi or f"arXiv:{arxiv_id}")]

    source = find_article_html_source()
    if source:
        content = Path(source).read_text(encoding="utf-8", errors="replace")
    elif is_non_html_document():
        return []
    else:
        request = Request(url, headers={"User-Agent": CONFIG.USER_AGENT})
        with urlopen(
            request,
            timeout=max(0.001, deadline - time.monotonic()),
        ) as response:
            if response.headers.get_content_type() not in {
                "text/html",
                "application/xhtml+xml",
            }:
                return []
            content = response.read().decode(
                response.headers.get_content_charset() or "utf-8",
                errors="replace",
            )

    parser = PaperReferences()
    parser.feed(content)
    text = "\n".join([*parser.references, "".join(parser.text)])
    # papers-dl's parser recognizes arXiv: identifiers, but not arxiv.org links.
    text = re.sub(
        rf"(?:https?://)?(?:www\.)?arxiv\.org/(?:abs|pdf|html)/({ARXIV_ID})(?:\.pdf)?",
        r"arXiv:\1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"arxiv:\s+", "arXiv:", text, flags=re.IGNORECASE)
    parsed = subprocess.run(
        [binary, "parse", "--match", "doi", "--match", "arxiv", "--format", "jsonl"],
        input=text,
        capture_output=True,
        text=True,
        timeout=max(0.001, deadline - time.monotonic()),
        check=True,
    )
    identifiers = {}
    for line in parsed.stdout.splitlines():
        # Dependency warnings and the CLI's "No papers found" are not identifiers.
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") not in {"doi", "arxiv"}:
            continue
        identifier = normalize_identifier(record["id"])
        if identifier:
            identifiers.setdefault(identifier.casefold(), identifier)
    return list(identifiers.values())


def new_pdf_files(output_dir: Path, files_before: set[Path]) -> list[Path]:
    """Return newly created non-empty PDFs, including partially timed-out downloads."""
    return sorted(
        (
            path
            for path in output_dir.iterdir()
            if path.is_file()
            and path.suffix.lower() == ".pdf"
            and path.stat().st_size > 0
            and path.resolve() not in files_before
        ),
        key=lambda path: path.name,
    )


def save_paper(identifier: str, binary: str, timeout: float) -> tuple[bool, int, str]:
    """
    Download paper using papers-dl.

    Returns: (success, downloaded_file_count, error_message)
    """
    # Get config from env
    config = load_config()
    papersdl_args = config.PAPERSDL_ARGS
    papersdl_args_extra = config.PAPERSDL_ARGS_EXTRA

    # Output directory is current directory (hook already runs in output dir)
    output_dir = Path(OUTPUT_DIR)
    files_before = {path.resolve() for path in output_dir.iterdir() if path.is_file()}

    # Build command - papers-dl <args> <identifier> -o <output_dir>
    cmd = [binary, *papersdl_args, identifier, "-o", str(output_dir)]

    if papersdl_args_extra:
        cmd.extend(papersdl_args_extra)
    elif identifier.lower().startswith("arxiv:"):
        cmd.extend(["--providers", "arxiv"])

    try:
        print(f"Downloading paper: {identifier}", file=sys.stderr)
        output_lines: list[str] = []
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        def _read_output() -> None:
            if not process.stdout:
                return
            for line in process.stdout:
                output_lines.append(line)
                sys.stderr.write(line)

        reader = threading.Thread(target=_read_output, daemon=True)
        reader.start()

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            reader.join(timeout=1)
            downloaded_files = new_pdf_files(output_dir, files_before)
            if downloaded_files:
                return True, len(downloaded_files), ""
            return False, 0, f"Timed out after {timeout} seconds"

        reader.join(timeout=1)
        combined_output = "".join(output_lines)
        downloaded_files = new_pdf_files(output_dir, files_before)

        if downloaded_files:
            return True, len(downloaded_files), ""
        else:
            stderr = combined_output
            stdout = combined_output

            # These are NOT errors - page simply has no downloadable paper
            stderr_lower = stderr.lower()
            stdout_lower = stdout.lower()
            if "not found" in stderr_lower or "not found" in stdout_lower:
                return True, 0, ""  # Paper not available - success, no output
            if "no results" in stderr_lower or "no results" in stdout_lower:
                return True, 0, ""  # No paper found - success, no output
            if process.returncode == 0:
                return (
                    True,
                    0,
                    "",
                )  # papers-dl exited cleanly, just no paper - success

            # These ARE errors - something went wrong
            if "404" in stderr or "404" in stdout:
                return False, 0, "404 Not Found"
            if "403" in stderr or "403" in stdout:
                return False, 0, "403 Forbidden"

            return False, 0, f"papers-dl error: {stderr[:200] or stdout[:200]}"

    except subprocess.TimeoutExpired:
        return False, 0, f"Timed out after {timeout} seconds"
    except Exception as e:
        return False, 0, f"{type(e).__name__}: {e}"


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="URL to download paper from")
def main(url: str):
    """Download scientific paper from a URL using papers-dl."""
    downloaded_count = 0
    error = ""

    try:
        # Check if papers-dl is enabled
        config = load_config()

        if not config.PAPERSDL_ENABLED:
            print("Skipping papers-dl (PAPERSDL_ENABLED=False)", file=sys.stderr)
            emit_archive_result_record("skipped", "PAPERSDL_ENABLED=False")
            sys.exit(0)

        binary = config.PAPERSDL_BINARY

        deadline = time.monotonic() + config.PAPERSDL_TIMEOUT
        identifiers = find_paper_identifiers(url, binary, deadline)
        errors = []
        for identifier in identifiers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors.append(f"Timed out after {config.PAPERSDL_TIMEOUT} seconds")
                break
            success, count, error = save_paper(identifier, binary, remaining)
            downloaded_count += count
            if not success:
                errors.append(f"{identifier}: {error}")

        if not errors:
            # Success - emit ArchiveResult
            pdfs = sorted(path.name for path in OUTPUT_DIR.glob("*.pdf"))
            status = "noresults" if downloaded_count == 0 else "succeeded"
            emit_archive_result_record(
                status,
                (
                    f"{PLUGIN_DIR}/{pdfs[0]}"
                    if len(pdfs) == 1
                    else f"{downloaded_count} PDFs downloaded"
                    if downloaded_count
                    else "No papers found"
                ),
            )
            sys.exit(0)
        else:
            error = "; ".join(errors)
            print(f"ERROR: {error}", file=sys.stderr)
            emit_archive_result_record("failed", error or "")
            sys.exit(1)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result_record("failed", error)
        sys.exit(1)


if __name__ == "__main__":
    main()
