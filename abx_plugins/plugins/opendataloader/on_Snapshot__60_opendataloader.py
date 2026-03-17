#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "rich-click",
# ]
# ///
"""
OCR PDFs and images using opendataloader-pdf.

Finds PDF and image files produced by other plugins (pdf, screenshot, responses,
staticfile) and extracts structured text content using opendataloader-pdf.

Usage: on_Snapshot__60_opendataloader.py --url=<url> --snapshot-id=<uuid> > events.jsonl

Environment variables:
    OPENDATALOADER_BINARY: Path to opendataloader-pdf binary
    OPENDATALOADER_TIMEOUT: Timeout in seconds (default: 120)
    OPENDATALOADER_ARGS: Default opendataloader-pdf arguments (JSON array)
    OPENDATALOADER_ARGS_EXTRA: Extra arguments to append (JSON array)
    TIMEOUT: Fallback timeout
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from base.utils import load_config, emit_archive_result, write_text_atomic

import rich_click as click


# Extractor metadata
PLUGIN_NAME = "opendataloader"
BIN_NAME = "opendataloader-pdf"
BIN_PROVIDERS = "env,pip"
PLUGIN_DIR = Path(__file__).resolve().parent.name
SNAP_DIR = Path(os.environ.get("SNAP_DIR", ".")).resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
OUTPUT_FILE = "content.md"
TEXT_FILE = "content.txt"
METADATA_FILE = "metadata.json"

PDF_EXTENSIONS = (".pdf",)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif")


def find_pdf_and_image_sources() -> list[Path]:
    """Find PDF and image files from sibling plugin output directories.

    Searches for outputs from: pdf, screenshot, responses, staticfile plugins.
    """
    search_patterns = [
        # PDF plugin output
        "pdf/output.pdf",
        "*_pdf/output.pdf",
        "pdf/*.pdf",
        "*_pdf/*.pdf",
        # Screenshot plugin output
        "screenshot/screenshot.png",
        "*_screenshot/screenshot.png",
        "screenshot/*.png",
        "*_screenshot/*.png",
        "screenshot/*.jpg",
        "*_screenshot/*.jpg",
        # Responses plugin output (PDFs and images served by the URL)
        "responses/**/*.pdf",
        "*_responses/**/*.pdf",
        "responses/**/*.png",
        "*_responses/**/*.png",
        "responses/**/*.jpg",
        "*_responses/**/*.jpg",
        "responses/**/*.jpeg",
        "*_responses/**/*.jpeg",
        "responses/**/*.webp",
        "*_responses/**/*.webp",
        "responses/**/*.tiff",
        "*_responses/**/*.tiff",
        "responses/**/*.tif",
        "*_responses/**/*.tif",
        "responses/**/*.bmp",
        "*_responses/**/*.bmp",
        "responses/**/*.gif",
        "*_responses/**/*.gif",
        # Staticfile plugin output
        "staticfile/**/*.pdf",
        "*_staticfile/**/*.pdf",
        "staticfile/**/*.png",
        "*_staticfile/**/*.png",
        "staticfile/**/*.jpg",
        "*_staticfile/**/*.jpg",
        "staticfile/**/*.jpeg",
        "*_staticfile/**/*.jpeg",
    ]

    found: list[Path] = []
    seen: set[str] = set()

    for base in (Path.cwd(), Path.cwd().parent):
        for pattern in search_patterns:
            for match in base.glob(pattern):
                resolved = str(match.resolve())
                if resolved in seen:
                    continue
                if match.is_file() and match.stat().st_size > 0:
                    suffix = match.suffix.lower()
                    if suffix in PDF_EXTENSIONS or suffix in IMAGE_EXTENSIONS:
                        found.append(match)
                        seen.add(resolved)

    return found


def _run_opendataloader(binary: str, source_file: Path, fmt: str, out_dir: Path, timeout: int, extra_args: list[str]) -> Path | None:
    """Run opendataloader-pdf on a single file with a given format, return output path or None."""
    cmd = [binary, "-f", fmt, "-o", str(out_dir), "-q", *extra_args, str(source_file)]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        text=True,
    )
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.returncode != 0:
        return None

    # opendataloader-pdf writes {input_stem}.{ext} in the output dir
    stem = source_file.stem
    ext_map = {"markdown": ".md", "text": ".txt", "json": ".json"}
    expected = out_dir / f"{stem}{ext_map.get(fmt, '.md')}"
    if expected.is_file() and expected.stat().st_size > 0:
        return expected
    # Fallback: find any new file in out_dir
    for f in sorted(out_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.is_file() and f.stat().st_size > 0:
            return f
    return None


def extract_opendataloader(url: str, binary: str) -> tuple[str, str]:
    """
    OCR PDFs and images using opendataloader-pdf.

    Returns: (status, output_str)
    """
    config = load_config()
    timeout = config.OPENDATALOADER_TIMEOUT
    opendataloader_args = config.OPENDATALOADER_ARGS
    opendataloader_args_extra = config.OPENDATALOADER_ARGS_EXTRA
    extra_args = [*opendataloader_args, *opendataloader_args_extra]

    # Find PDF/image sources from sibling plugins
    sources = find_pdf_and_image_sources()
    if not sources:
        return "noresults", "No PDF or image sources found"

    output_dir = Path(OUTPUT_DIR)
    all_md_parts: list[str] = []
    all_text_parts: list[str] = []
    metadata_records: list[dict] = []

    for source_file in sources:
        try:
            # Run markdown extraction
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                md_out = _run_opendataloader(binary, source_file, "markdown", tmp, timeout, extra_args)
                md_content = md_out.read_text(errors="ignore") if md_out else ""

            # Run text extraction
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                txt_out = _run_opendataloader(binary, source_file, "text", tmp, timeout, extra_args)
                text_content = txt_out.read_text(errors="ignore") if txt_out else ""

            if not md_content and not text_content:
                print(
                    f"[opendataloader] No content extracted from {source_file.name}",
                    file=sys.stderr,
                )
                continue

            if md_content:
                all_md_parts.append(f"<!-- source: {source_file.name} -->\n{md_content}")
            if text_content:
                all_text_parts.append(text_content)

            metadata_records.append({
                "source_file": str(source_file.name),
                "source_path": str(source_file),
                "chars_extracted": len(md_content or text_content),
            })

        except subprocess.TimeoutExpired:
            print(
                f"[opendataloader] Timed out on {source_file.name} after {timeout}s",
                file=sys.stderr,
            )
            continue
        except Exception as e:
            print(
                f"[opendataloader] Error on {source_file.name}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            continue

    if not all_md_parts and not all_text_parts:
        return "noresults", "No content extracted from sources"

    # Write combined output files
    if all_md_parts:
        combined_md = "\n\n---\n\n".join(all_md_parts)
        write_text_atomic(output_dir / OUTPUT_FILE, combined_md)

    if all_text_parts:
        combined_text = "\n\n---\n\n".join(all_text_parts)
        write_text_atomic(output_dir / TEXT_FILE, combined_text)

    write_text_atomic(
        output_dir / METADATA_FILE,
        json.dumps(
            {
                "sources_processed": len(metadata_records),
                "total_sources_found": len(sources),
                "files": metadata_records,
            },
            indent=2,
        ),
    )

    return "succeeded", OUTPUT_FILE


@click.command()
@click.option("--url", required=True, help="URL being archived")
@click.option("--snapshot-id", required=True, help="Snapshot UUID")
def main(url: str, snapshot_id: str):
    """OCR PDFs and images using opendataloader-pdf."""

    try:
        config = load_config()

        if not config.OPENDATALOADER_ENABLED:
            print("Skipping opendataloader (OPENDATALOADER_ENABLED=False)", file=sys.stderr)
            emit_archive_result("skipped", "OPENDATALOADER_ENABLED=False")
            sys.exit(0)

        # Get binary from environment
        binary = config.OPENDATALOADER_BINARY

        # Run extraction
        status, output = extract_opendataloader(url, binary)
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
