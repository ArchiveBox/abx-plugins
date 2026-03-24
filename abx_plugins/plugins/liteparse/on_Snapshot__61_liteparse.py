#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "rich-click",
#   "abx-plugins",
# ]
# ///
"""
Extract text and metadata from PDFs using LiteParse (lit CLI by LlamaIndex).

Finds all PDF files produced by other plugins (pdf, responses, staticfile)
and extracts text content from each one using the ``lit parse`` command.
Processes every PDF found, combining results into content.txt and metadata.json.

Usage: on_Snapshot__61_liteparse.py --url=<url> > events.jsonl

Environment variables:
    LITEPARSE_BINARY: Path to lit binary
    LITEPARSE_TIMEOUT: Timeout in seconds (default: 120)
    LITEPARSE_ARGS: Default lit arguments (JSON array)
    LITEPARSE_ARGS_EXTRA: Extra arguments to append (JSON array)
    TIMEOUT: Fallback timeout
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    load_config,
    emit_archive_result_record,
    write_text_atomic,
)

import rich_click as click


PLUGIN_NAME = "liteparse"
BIN_NAME = "lit"
BIN_PROVIDERS = "env,npm"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
TEXT_FILE = "content.txt"
JSON_FILE = "content.json"
METADATA_FILE = "metadata.json"


def find_pdf_sources() -> list[Path]:
    """Find all PDF files from sibling plugin output directories."""
    search_patterns = [
        "pdf/output.pdf",
        "*_pdf/output.pdf",
        "pdf/*.pdf",
        "*_pdf/*.pdf",
        "responses/**/*.pdf",
        "*_responses/**/*.pdf",
        "staticfile/**/*.pdf",
        "*_staticfile/**/*.pdf",
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
                    found.append(match)
                    seen.add(resolved)

    return found


def _run_liteparse(
    binary: str,
    source_file: Path,
    fmt: str,
    output_path: Path,
    timeout: int,
    extra_args: list[str],
) -> bool:
    """Run lit parse on a single file, return True on success."""
    cmd = [
        binary,
        "parse",
        str(source_file),
        "--format",
        fmt,
        "-o",
        str(output_path),
        *extra_args,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        text=True,
    )
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    return (
        result.returncode == 0
        and output_path.is_file()
        and output_path.stat().st_size > 0
    )


def _extract_single_pdf(
    binary: str,
    source_file: Path,
    timeout: int,
    extra_args: list[str],
) -> tuple[str, str]:
    """Run text + JSON extraction on a single PDF, return (text_content, json_content)."""
    text_content = ""
    json_content = ""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        text_out = tmp / "output.txt"
        if _run_liteparse(binary, source_file, "text", text_out, timeout, extra_args):
            text_content = text_out.read_text(errors="ignore")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        json_out = tmp / "output.json"
        if _run_liteparse(binary, source_file, "json", json_out, timeout, extra_args):
            json_content = json_out.read_text(errors="ignore")

    return text_content, json_content


def extract_liteparse(url: str, binary: str) -> tuple[str, str]:
    """
    Extract text from all PDFs found using lit (LiteParse).

    Returns: (status, output_str)
    """
    config = load_config()
    timeout = config.LITEPARSE_TIMEOUT
    liteparse_args = config.LITEPARSE_ARGS
    liteparse_args_extra = config.LITEPARSE_ARGS_EXTRA
    extra_args = [*liteparse_args, *liteparse_args_extra]

    sources = find_pdf_sources()
    if not sources:
        return "noresults", "No PDF sources found"

    print(f"[liteparse] Found {len(sources)} PDF(s) to process", file=sys.stderr)

    output_dir = Path(OUTPUT_DIR)
    all_text_parts: list[str] = []
    all_json_parts: list[str] = []
    metadata_records: list[dict] = []

    binary_failed = False

    for source_file in sources:
        print(f"[liteparse] Processing: {source_file.name}", file=sys.stderr)
        try:
            text_content, json_content = _extract_single_pdf(
                binary,
                source_file,
                timeout,
                extra_args,
            )

            if not text_content and not json_content:
                print(
                    f"[liteparse] No content extracted from {source_file.name}",
                    file=sys.stderr,
                )
                continue

            if text_content:
                all_text_parts.append(
                    f"<!-- source: {source_file.name} -->\n{text_content}",
                )
            if json_content:
                all_json_parts.append(json_content)

            metadata_records.append(
                {
                    "source_file": str(source_file.name),
                    "source_path": str(source_file),
                    "chars_extracted": len(text_content or json_content),
                },
            )

        except subprocess.TimeoutExpired:
            print(
                f"[liteparse] Timed out on {source_file.name} after {timeout}s",
                file=sys.stderr,
            )
            continue
        except (FileNotFoundError, PermissionError, OSError) as e:
            print(
                f"[liteparse] Binary execution failed: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            binary_failed = True
            break
        except Exception as e:
            print(
                f"[liteparse] Error on {source_file.name}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            continue

    if binary_failed:
        return "failed", f"Binary '{binary}' could not be executed"

    if not all_text_parts and not all_json_parts:
        return "noresults", "No content extracted from sources"

    if all_text_parts:
        combined_text = "\n\n---\n\n".join(all_text_parts)
        write_text_atomic(output_dir / TEXT_FILE, combined_text)

    if all_json_parts:
        # Combine JSON outputs: if single, write as-is; if multiple, wrap in array
        if len(all_json_parts) == 1:
            write_text_atomic(output_dir / JSON_FILE, all_json_parts[0])
        else:
            parsed_jsons = []
            for jp in all_json_parts:
                try:
                    parsed_jsons.append(json.loads(jp))
                except json.JSONDecodeError:
                    parsed_jsons.append(jp)
            write_text_atomic(
                output_dir / JSON_FILE,
                json.dumps(parsed_jsons, indent=2),
            )

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

    if all_text_parts:
        return "succeeded", f"{PLUGIN_DIR}/{TEXT_FILE}"
    return "succeeded", f"{PLUGIN_DIR}/{JSON_FILE}"


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="URL being archived")
def main(url: str):
    """Extract text from PDFs using LiteParse."""

    try:
        config = load_config()

        if not config.LITEPARSE_ENABLED:
            print("Skipping liteparse (LITEPARSE_ENABLED=False)", file=sys.stderr)
            emit_archive_result_record("skipped", "LITEPARSE_ENABLED=False")
            sys.exit(0)

        binary = config.LITEPARSE_BINARY

        status, output = extract_liteparse(url, binary)
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
