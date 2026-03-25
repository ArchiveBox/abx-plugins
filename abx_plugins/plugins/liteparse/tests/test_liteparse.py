"""
Integration tests for liteparse plugin.

Tests verify:
1. Hook scripts exist
2. Crawl hook emits correct BinaryRequest record for lit
3. required_binaries can resolve the lit binary via npm
4. Extraction runs with real lit binary on a real live PDF
5. Config options work (enabled/disabled)
6. Handles missing sources gracefully
7. Multiple PDFs are all processed
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import requests

from abx_plugins.plugins.base.test_utils import (
    get_hydrated_required_binaries,
    parse_jsonl_output,
)

PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = PLUGIN_DIR.parent

_LITEPARSE_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_liteparse.*"), None)
if _LITEPARSE_HOOK is None:
    raise FileNotFoundError(f"Snapshot hook not found in {PLUGIN_DIR}")
LITEPARSE_HOOK = _LITEPARSE_HOOK

TEST_URL = "https://example.com"

# Module-level cache for binary path
_liteparse_binary_path = None


def get_liteparse_binary_path() -> str | None:
    """Get lit binary path, installing via abx_pkg if needed."""
    global _liteparse_binary_path
    if _liteparse_binary_path and Path(_liteparse_binary_path).is_file():
        return _liteparse_binary_path

    from abx_pkg import Binary, NpmProvider, EnvProvider

    binary = Binary(
        name="lit",
        binproviders=[NpmProvider(), EnvProvider()],
        overrides={"npm": {"install_args": ["@llamaindex/liteparse"]}},
    ).load_or_install()
    if binary and binary.abspath:
        _liteparse_binary_path = str(binary.abspath)
        return _liteparse_binary_path

    return None


def require_liteparse_binary() -> str:
    """Return lit binary path or fail with actionable context."""
    binary_path = get_liteparse_binary_path()
    assert binary_path, (
        "lit (LiteParse) dependency resolution failed. required_binaries should resolve "
        "the binary automatically in this test environment."
    )
    assert Path(binary_path).is_file(), f"lit binary path invalid: {binary_path}"
    return binary_path


# Two public PDFs with known, distinct text content:
#
# PDF_URL_A (unec.edu.az/pdf-sample.pdf):
#   Contains "Adobe® Portable Document Format (PDF) is a universal file format
#   that preserves all of the fonts, formatting, colours and graphics"
#
# PDF_URL_B (pdfobject.com/pdf/sample.pdf):
#   Contains "This is a simple PDF file. Fun fun fun."
#   and "Lorem ipsum dolor sit amet, consectetuer adipiscing elit."
#
PDF_URL_A = "https://unec.edu.az/application/uploads/2014/12/pdf-sample.pdf"
PDF_URL_B = "https://pdfobject.com/pdf/sample.pdf"


def _download_pdf(url: str) -> bytes:
    """Download a single PDF by URL, fail if unavailable."""
    resp = requests.get(url, timeout=30)
    assert resp.status_code == 200, f"Failed to download {url}: HTTP {resp.status_code}"
    assert resp.content[:5] == b"%PDF-", f"Not a PDF: {url}"
    return resp.content


def test_hook_scripts_exist():
    assert LITEPARSE_HOOK.exists(), f"Snapshot hook not found: {LITEPARSE_HOOK}"


def test_crawl_hook_emits_lit_binary_request_record():
    binary = next(
        record
        for record in get_hydrated_required_binaries(PLUGIN_DIR)
        if record.get("name") == "lit"
    )
    assert binary.get("type", "BinaryRequest") == "BinaryRequest"
    assert binary.get("name") == "lit"
    assert binary.get("overrides", {}).get("npm", {}).get("install_args") == [
        "@llamaindex/liteparse",
    ]


def test_verify_deps_with_install_hooks():
    """Verify lit binary can be installed and resolved via abx_pkg and hooks."""
    binary_path = require_liteparse_binary()
    assert Path(binary_path).is_file(), (
        f"Binary path must be a valid file: {binary_path}"
    )


def test_config_disabled_skips():
    """Test that LITEPARSE_ENABLED=False skips extraction."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["LITEPARSE_ENABLED"] = "False"

        result = subprocess.run(
            [
                str(LITEPARSE_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, f"Should exit 0 when disabled: {result.stderr}"
        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Expected skipped JSONL output"
        assert result_json["status"] == "skipped", result_json
        assert result_json["output_str"] == "LITEPARSE_ENABLED=False", result_json


def test_noresults_without_sources():
    """Test that hook reports noresults when no PDF sources exist."""
    binary_path = require_liteparse_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["LITEPARSE_BINARY"] = binary_path

        result = subprocess.run(
            [
                str(LITEPARSE_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, "Should exit 0 without sources"
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "noresults"


def test_extract_single_pdf():
    """Test extraction on a single real PDF downloaded from the web.

    Uses PDF_URL_B (pdfobject.com sample) which contains the exact text:
      "This is a simple PDF file. Fun fun fun."
      "Lorem ipsum dolor sit amet, consectetuer adipiscing elit."
    Asserts these specific sentences appear in the extracted output.
    """
    binary_path = require_liteparse_binary()
    pdf_content = _download_pdf(PDF_URL_B)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"

        # Place PDF as if the pdf plugin produced it
        pdf_dir = snap_dir / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        (pdf_dir / "output.pdf").write_bytes(pdf_content)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["LITEPARSE_BINARY"] = binary_path

        result = subprocess.run(
            [
                str(LITEPARSE_HOOK),
                "--url",
                PDF_URL_B,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        record = parse_jsonl_output(result.stdout)
        assert record, "Should have ArchiveResult JSONL output"
        assert record["status"] == "succeeded", (
            f"Should succeed: {record}. stderr: {result.stderr}"
        )

        output_dir = snap_dir / "liteparse"
        assert (output_dir / "content.txt").exists(), "content.txt not created"
        assert (output_dir / "metadata.json").exists(), "metadata.json not created"

        text_content = (output_dir / "content.txt").read_text(errors="ignore")
        text_lower = text_content.lower()

        # Assert specific sentences from the actual PDF page content
        assert "this is a simple pdf file" in text_lower, (
            f"Expected exact text 'This is a simple PDF file' from {PDF_URL_B}. "
            f"Got: {text_content[:500]!r}"
        )
        assert "consectetuer adipiscing elit" in text_lower, (
            f"Expected exact text 'consectetuer adipiscing elit' from {PDF_URL_B}. "
            f"Got: {text_content[:500]!r}"
        )

        metadata = json.loads((output_dir / "metadata.json").read_text())
        assert metadata["sources_processed"] == 1
        assert metadata["files"][0]["source_file"] == "output.pdf"


def test_extract_multiple_pdfs():
    """Test that ALL PDFs are processed when multiple exist across plugins.

    Uses two different PDFs with distinct content:
      PDF_URL_A (unec.edu.az): contains "preserves all of the fonts, formatting, colours and graphics"
      PDF_URL_B (pdfobject.com): contains "This is a simple PDF file. Fun fun fun."

    Places them in pdf/ and responses/ directories and verifies the combined
    output contains unique text from BOTH documents.
    """
    binary_path = require_liteparse_binary()
    pdf_a = _download_pdf(PDF_URL_A)
    pdf_b = _download_pdf(PDF_URL_B)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"

        # Place PDF A in pdf/ plugin output
        pdf_dir = snap_dir / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        (pdf_dir / "output.pdf").write_bytes(pdf_a)

        # Place PDF B in responses/ as if the server served a PDF
        responses_dir = snap_dir / "responses" / "application" / "example.com"
        responses_dir.mkdir(parents=True, exist_ok=True)
        (responses_dir / "document.pdf").write_bytes(pdf_b)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["LITEPARSE_BINARY"] = binary_path

        result = subprocess.run(
            [
                str(LITEPARSE_HOOK),
                "--url",
                "https://example.com/docs",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        record = parse_jsonl_output(result.stdout)
        assert record, "Should have ArchiveResult JSONL output"
        assert record["status"] == "succeeded", f"Should succeed: {record}"

        output_dir = snap_dir / "liteparse"
        metadata = json.loads((output_dir / "metadata.json").read_text())
        assert metadata["sources_processed"] == 2, (
            f"Expected 2 PDFs processed, got {metadata['sources_processed']}. "
            f"Files: {metadata['files']}"
        )
        assert metadata["total_sources_found"] == 2

        text_content = (output_dir / "content.txt").read_text(errors="ignore")
        text_lower = text_content.lower()

        # Assert unique content from PDF A (unec.edu.az pdf-sample.pdf)
        # This PDF is about Adobe Acrobat and contains this exact phrase:
        assert "preserves all" in text_lower and "colours and graphics" in text_lower, (
            f"Expected text from PDF_URL_A about 'preserves all of the fonts, formatting, "
            f"colours and graphics'. Got: {text_content[:500]!r}"
        )

        # Assert unique content from PDF B (pdfobject.com sample.pdf)
        # This PDF contains a simple greeting and Lorem ipsum:
        assert "this is a simple pdf file" in text_lower, (
            f"Expected text from PDF_URL_B: 'This is a simple PDF file'. "
            f"Got: {text_content[:500]!r}"
        )
        assert "consectetuer adipiscing elit" in text_lower, (
            f"Expected text from PDF_URL_B: 'consectetuer adipiscing elit'. "
            f"Got: {text_content[:500]!r}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
