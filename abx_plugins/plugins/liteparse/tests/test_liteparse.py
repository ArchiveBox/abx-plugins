"""
Integration tests for liteparse plugin.

Tests verify:
1. Hook scripts exist
2. Crawl hook emits correct Binary record for lit
3. Install hooks can install lit binary via npm
4. Extraction runs with real lit binary on a real live PDF
5. Config options work (enabled/disabled)
6. Handles missing sources gracefully
7. Multiple PDFs are all processed
"""

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
import requests

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
from base.test_utils import parse_jsonl_output

PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = PLUGIN_DIR.parent

_LITEPARSE_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_liteparse.*"), None)
if _LITEPARSE_HOOK is None:
    raise FileNotFoundError(f"Snapshot hook not found in {PLUGIN_DIR}")
LITEPARSE_HOOK = _LITEPARSE_HOOK

_LITEPARSE_CRAWL_HOOK = next(PLUGIN_DIR.glob("on_Crawl__*_liteparse_install.*"), None)
if _LITEPARSE_CRAWL_HOOK is None:
    raise FileNotFoundError(f"Crawl hook not found in {PLUGIN_DIR}")
LITEPARSE_CRAWL_HOOK = _LITEPARSE_CRAWL_HOOK

TEST_URL = "https://example.com"

# Module-level cache for binary path
_liteparse_binary_path = None
_liteparse_lib_root = None


def get_liteparse_binary_path():
    """Get lit binary path using abx_pkg or install hooks."""
    global _liteparse_binary_path
    if _liteparse_binary_path and Path(_liteparse_binary_path).is_file():
        return _liteparse_binary_path

    # Try loading via abx_pkg Binary API first
    from abx_pkg import Binary, NpmProvider, EnvProvider

    try:
        binary = Binary(
            name="lit",
            binproviders=[NpmProvider(), EnvProvider()],
            overrides={"npm": {"install_args": ["@llamaindex/liteparse"]}},
        ).load()
        if binary and binary.abspath:
            _liteparse_binary_path = str(binary.abspath)
            return _liteparse_binary_path
    except Exception:
        pass

    # Fall back to install via real plugin hooks
    npm_hook = PLUGINS_ROOT / "npm" / "on_Binary__10_npm_install.py"
    if not npm_hook.exists():
        return None

    binproviders = "*"
    overrides = None

    if LITEPARSE_CRAWL_HOOK.exists():
        crawl_result = subprocess.run(
            [str(LITEPARSE_CRAWL_HOOK)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        for line in crawl_result.stdout.strip().split("\n"):
            if not line.strip().startswith("{"):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") == "Binary" and record.get("name") == "lit":
                binproviders = record.get("binproviders", "*")
                overrides = record.get("overrides")
                break

    global _liteparse_lib_root
    if not _liteparse_lib_root:
        _liteparse_lib_root = tempfile.mkdtemp(prefix="liteparse-lib-")

    env = os.environ.copy()
    env["LIB_DIR"] = str(Path(_liteparse_lib_root) / ".config" / "abx" / "lib")
    env["SNAP_DIR"] = str(Path(_liteparse_lib_root) / "data")
    env["CRAWL_DIR"] = str(Path(_liteparse_lib_root) / "crawl")

    cmd = [
        str(npm_hook),
        "--binary-id", str(uuid.uuid4()),
        "--machine-id", str(uuid.uuid4()),
        "--name", "lit",
        f"--binproviders={binproviders}",
    ]
    if overrides:
        cmd.append(f"--overrides={json.dumps(overrides)}")

    install_result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    for line in install_result.stdout.strip().split("\n"):
        if not line.strip().startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "Binary" and record.get("name") == "lit":
            _liteparse_binary_path = record.get("abspath")
            return _liteparse_binary_path

    return None


def require_liteparse_binary() -> str:
    """Return lit binary path or fail with actionable context."""
    binary_path = get_liteparse_binary_path()
    assert binary_path, (
        "lit (LiteParse) installation failed. Install hook should install "
        "the binary automatically in this test environment."
    )
    assert Path(binary_path).is_file(), f"lit binary path invalid: {binary_path}"
    return binary_path


def _download_test_pdf() -> bytes:
    """Download a small public PDF for testing. Tries multiple sources."""
    pdf_urls = [
        "https://unec.edu.az/application/uploads/2014/12/pdf-sample.pdf",
        "https://www.orimi.com/pdf-test.pdf",
    ]
    for url in pdf_urls:
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                return resp.content
        except Exception:
            continue
    pytest.fail("Could not download any test PDF from the web")


def test_hook_scripts_exist():
    assert LITEPARSE_HOOK.exists(), f"Snapshot hook not found: {LITEPARSE_HOOK}"
    assert LITEPARSE_CRAWL_HOOK.exists(), f"Crawl hook not found: {LITEPARSE_CRAWL_HOOK}"


def test_crawl_hook_emits_lit_binary_record():
    result = subprocess.run(
        [str(LITEPARSE_CRAWL_HOOK)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    binary = parse_jsonl_output(result.stdout, record_type="Binary")
    assert binary, "Expected crawl hook to emit Binary record"
    assert binary.get("type") == "Binary"
    assert binary.get("name") == "lit"
    assert binary.get("overrides", {}).get("npm", {}).get("install_args") == ["@llamaindex/liteparse"]


def test_verify_deps_with_install_hooks():
    """Verify lit binary can be installed and resolved via abx_pkg and hooks."""
    binary_path = require_liteparse_binary()
    assert Path(binary_path).is_file(), f"Binary path must be a valid file: {binary_path}"


def test_config_disabled_skips():
    """Test that LITEPARSE_ENABLED=False skips extraction."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["LITEPARSE_ENABLED"] = "False"

        result = subprocess.run(
            [str(LITEPARSE_HOOK),
                "--url", TEST_URL,
                "--snapshot-id", "test-disabled",
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
            [str(LITEPARSE_HOOK),
                "--url", TEST_URL,
                "--snapshot-id", "test-nosources",
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

    Downloads a real PDF, places it as pdf plugin output, runs the
    liteparse snapshot hook, and asserts the output contains expected
    text content from the actual PDF document.
    """
    binary_path = require_liteparse_binary()
    pdf_content = _download_test_pdf()

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
            [str(LITEPARSE_HOOK),
                "--url", "https://example.com/test.pdf",
                "--snapshot-id", "test-single-pdf",
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
        assert record["status"] == "succeeded", f"Should succeed: {record}. stderr: {result.stderr}"

        output_dir = snap_dir / "liteparse"
        assert (output_dir / "content.txt").exists(), "content.txt not created"
        assert (output_dir / "metadata.json").exists(), "metadata.json not created"

        text_content = (output_dir / "content.txt").read_text(errors="ignore")
        assert len(text_content) > 10, f"content.txt too short: {text_content!r}"

        # The test PDFs contain known text - verify real content was extracted
        # pdf-sample.pdf contains "Adobe Acrobat" or similar known strings
        text_lower = text_content.lower()
        assert any(word in text_lower for word in ["pdf", "sample", "adobe", "acrobat", "document", "page", "file", "test"]), (
            f"Extracted text does not contain expected PDF content keywords. Got: {text_content[:500]!r}"
        )

        metadata = json.loads((output_dir / "metadata.json").read_text())
        assert metadata["sources_processed"] == 1
        assert metadata["files"][0]["source_file"] == "output.pdf"


def test_extract_multiple_pdfs():
    """Test that ALL PDFs are processed when multiple exist across plugins."""
    binary_path = require_liteparse_binary()
    pdf_content = _download_test_pdf()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"

        # Place PDF in pdf/ plugin output
        pdf_dir = snap_dir / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        (pdf_dir / "output.pdf").write_bytes(pdf_content)

        # Place another PDF in responses/
        responses_dir = snap_dir / "responses" / "application" / "example.com"
        responses_dir.mkdir(parents=True, exist_ok=True)
        (responses_dir / "document.pdf").write_bytes(pdf_content)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["LITEPARSE_BINARY"] = binary_path

        result = subprocess.run(
            [str(LITEPARSE_HOOK),
                "--url", "https://example.com/docs",
                "--snapshot-id", "test-multi-pdf",
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

        # Verify combined output contains content from both files
        text_content = (output_dir / "content.txt").read_text(errors="ignore")
        assert "---" in text_content or text_content.count("<!-- source:") >= 2, (
            "Combined text should contain content from both PDFs"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
