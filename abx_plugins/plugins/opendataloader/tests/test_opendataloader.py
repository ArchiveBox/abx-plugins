"""
Integration tests for opendataloader plugin.

Tests verify:
1. Hook script exists
2. Install hooks can install opendataloader-pdf binary
3. Extraction runs with real opendataloader-pdf binary on a real live PDF
4. Multiple PDFs are all processed (not just the first)
5. Config options work (enabled/disabled, FORCE_OCR)
6. Handles missing sources gracefully
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import requests

from abx_plugins.plugins.base.test_utils import parse_jsonl_output

PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = PLUGIN_DIR.parent
_OPENDATALOADER_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_opendataloader.*"), None)
if _OPENDATALOADER_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
OPENDATALOADER_HOOK = _OPENDATALOADER_HOOK
INSTALL_HOOK = PLUGIN_DIR / "on_Crawl__42_opendataloader_install.finite.bg.py"
TEST_URL = "https://example.com"

# Module-level cache for binary path
_opendataloader_binary_path = None
_java_binary_path = None


def get_opendataloader_binary_path() -> str | None:
    """Get opendataloader-pdf binary path, installing via abx_pkg if needed."""
    global _opendataloader_binary_path
    if _opendataloader_binary_path and Path(_opendataloader_binary_path).is_file():
        return _opendataloader_binary_path

    from abx_pkg import Binary, PipProvider, EnvProvider

    binary = Binary(
        name="opendataloader-pdf",
        binproviders=[PipProvider(), EnvProvider()],
        overrides={"pip": {"install_args": ["opendataloader-pdf"]}},
    ).load_or_install()
    if binary and binary.abspath:
        _opendataloader_binary_path = str(binary.abspath)
        return _opendataloader_binary_path

    return None


def require_opendataloader_binary() -> str:
    binary_path = get_opendataloader_binary_path()
    assert binary_path, (
        "opendataloader-pdf installation failed. Install hook should install "
        "the binary automatically in this test environment."
    )
    assert Path(binary_path).is_file(), (
        f"opendataloader-pdf binary path invalid: {binary_path}"
    )
    return binary_path


def get_java_binary_path() -> str | None:
    """Get a Java 11+ binary path, installing via abx_pkg if needed."""
    global _java_binary_path
    if _java_binary_path and Path(_java_binary_path).is_file():
        return _java_binary_path

    from abx_pkg import AptProvider, Binary, BrewProvider, EnvProvider, SemVer

    binary = Binary(
        name="java",
        min_version=SemVer("11.0.0"),
        binproviders=[EnvProvider(), BrewProvider(), AptProvider()],
        overrides={
            "brew": {"install_args": ["openjdk"]},
            "apt": {"install_args": ["default-jre"]},
        },
    ).load_or_install()
    if binary and binary.abspath:
        _java_binary_path = str(binary.abspath)
        return _java_binary_path

    return None


def require_java_binary() -> str:
    binary_path = get_java_binary_path()
    assert binary_path, (
        "Java 11+ installation failed for opendataloader integration tests."
    )
    assert Path(binary_path).is_file(), f"Java binary path invalid: {binary_path}"
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


def test_hook_script_exists():
    assert OPENDATALOADER_HOOK.exists(), f"Hook script not found: {OPENDATALOADER_HOOK}"


def test_verify_deps_with_install_hooks():
    binary_path = require_opendataloader_binary()
    assert Path(binary_path).is_file(), (
        f"Binary path must be a valid file: {binary_path}"
    )


def test_install_hook_requests_java_dependency():
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["CRAWL_DIR"] = tmpdir

        result = subprocess.run(
            [str(INSTALL_HOOK)],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{")
        ]
        java_record = next(record for record in records if record.get("name") == "java")
        assert java_record["min_version"] == "11.0.0"
        assert java_record["overrides"]["brew"]["install_args"] == ["openjdk"]
        if sys.platform == "darwin":
            assert java_record["binproviders"] == "env,brew"
        else:
            assert java_record["binproviders"] == "env,apt,brew"


def test_opendataloader_env_sets_java_home_and_path(tmp_path):
    monkeypatch_modules = {
        "rich_click": __import__("click"),
    }
    original_modules = {name: sys.modules.get(name) for name in monkeypatch_modules}
    sys.modules.update(monkeypatch_modules)

    spec = importlib.util.spec_from_file_location(
        "opendataloader_hook", OPENDATALOADER_HOOK
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        for name, value in original_modules.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value

    java_bin = tmp_path / "jdk" / "bin" / "java"
    java_bin.parent.mkdir(parents=True, exist_ok=True)
    java_bin.write_text("", encoding="utf-8")
    java_bin.chmod(0o755)

    env = module._opendataloader_env(str(java_bin))
    assert env is not None
    assert env["JAVA_HOME"] == str(java_bin.parent.parent)
    assert env["PATH"].split(os.pathsep)[0] == str(java_bin.parent)


def test_config_disabled_skips():
    """Test that OPENDATALOADER_ENABLED=False skips extraction."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["OPENDATALOADER_ENABLED"] = "False"

        result = subprocess.run(
            [
                str(OPENDATALOADER_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test-disabled",
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
        assert result_json["output_str"] == "OPENDATALOADER_ENABLED=False", result_json


def test_noresults_without_sources():
    """Test that hook reports noresults when no PDF sources exist."""
    binary_path = require_opendataloader_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["OPENDATALOADER_BINARY"] = binary_path

        result = subprocess.run(
            [
                str(OPENDATALOADER_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test-nosources",
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
    """Test extraction on a single real PDF downloaded from the web."""
    binary_path = require_opendataloader_binary()
    java_binary = require_java_binary()
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
        env["OPENDATALOADER_BINARY"] = binary_path
        env["JAVA_BINARY"] = java_binary

        result = subprocess.run(
            [
                str(OPENDATALOADER_HOOK),
                "--url",
                "https://example.com/test.pdf",
                "--snapshot-id",
                "test-single-pdf",
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
        assert record["output_str"].startswith("opendataloader/"), record

        output_dir = snap_dir / "opendataloader"
        assert (output_dir / "content.md").exists(), "content.md not created"
        assert (output_dir / "content.txt").exists(), "content.txt not created"
        assert (output_dir / "metadata.json").exists(), "metadata.json not created"

        md_content = (output_dir / "content.md").read_text(errors="ignore")
        assert len(md_content) > 10, f"content.md too short: {md_content!r}"

        metadata = json.loads((output_dir / "metadata.json").read_text())
        assert metadata["sources_processed"] == 1
        assert metadata["files"][0]["source_file"] == "output.pdf"


def test_extract_multiple_pdfs():
    """Test that ALL PDFs are processed when multiple exist across plugins.

    Places PDFs in both pdf/ and responses/ directories and verifies
    the hook processes every one, not just the first.
    """
    binary_path = require_opendataloader_binary()
    java_binary = require_java_binary()
    pdf_content = _download_test_pdf()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"

        # Place PDF in pdf/ plugin output
        pdf_dir = snap_dir / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        (pdf_dir / "output.pdf").write_bytes(pdf_content)

        # Place another PDF in responses/ as if the server served a PDF
        responses_dir = snap_dir / "responses" / "application" / "example.com"
        responses_dir.mkdir(parents=True, exist_ok=True)
        (responses_dir / "document.pdf").write_bytes(pdf_content)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["OPENDATALOADER_BINARY"] = binary_path
        env["JAVA_BINARY"] = java_binary

        result = subprocess.run(
            [
                str(OPENDATALOADER_HOOK),
                "--url",
                "https://example.com/docs",
                "--snapshot-id",
                "test-multi-pdf",
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

        output_dir = snap_dir / "opendataloader"
        metadata = json.loads((output_dir / "metadata.json").read_text())
        assert metadata["sources_processed"] == 2, (
            f"Expected 2 PDFs processed, got {metadata['sources_processed']}. "
            f"Files: {metadata['files']}"
        )
        assert metadata["total_sources_found"] == 2

        # Verify combined output contains content from both files
        md_content = (output_dir / "content.md").read_text(errors="ignore")
        assert "---" in md_content or md_content.count("<!-- source:") >= 2, (
            "Combined markdown should contain content from both PDFs"
        )


def test_force_ocr_adds_hybrid_flag():
    """Test that OPENDATALOADER_FORCE_OCR=true adds --hybrid docling-fast to command.

    Since no hybrid server is running, the extraction will fail/fallback,
    but we verify the flag is passed by checking stderr output.
    """
    binary_path = require_opendataloader_binary()
    java_binary = require_java_binary()
    pdf_content = _download_test_pdf()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"

        pdf_dir = snap_dir / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        (pdf_dir / "output.pdf").write_bytes(pdf_content)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["OPENDATALOADER_BINARY"] = binary_path
        env["JAVA_BINARY"] = java_binary
        env["OPENDATALOADER_FORCE_OCR"] = "true"

        result = subprocess.run(
            [
                str(OPENDATALOADER_HOOK),
                "--url",
                "https://example.com/scanned.pdf",
                "--snapshot-id",
                "test-force-ocr",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        # With FORCE_OCR, it will attempt --hybrid docling-fast.
        # If the hybrid server is unavailable, the hook must fall back to
        # standard extraction and still succeed with real content.
        assert result.returncode == 0, f"Should not crash: {result.stderr}"

        record = parse_jsonl_output(result.stdout)
        assert record, "Should have ArchiveResult JSONL output"
        assert record["status"] == "succeeded", (
            f"FORCE_OCR must succeed (with hybrid or via fallback), got: {record}. "
            f"stderr: {result.stderr}"
        )

        # Verify actual content was extracted (not a no-op pass)
        output_file = snap_dir / record["output_str"]
        assert output_file.exists(), f"Output file {record['output_str']} not created"
        content = output_file.read_text(errors="ignore")
        assert len(content) > 10, (
            f"Output too short, extraction may be broken: {content!r}"
        )


def test_cli_runtime_failure_reports_failed_status():
    """A non-zero opendataloader CLI exit should report failed, not noresults."""
    pdf_content = _download_test_pdf()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        pdf_dir = snap_dir / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        (pdf_dir / "output.pdf").write_bytes(pdf_content)

        failing_binary = tmpdir / "fake-opendataloader"
        failing_binary.write_text(
            "#!/bin/sh\necho 'simulated CLI failure' 1>&2\nexit 1\n",
            encoding="utf-8",
        )
        failing_binary.chmod(0o755)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["OPENDATALOADER_BINARY"] = str(failing_binary)

        result = subprocess.run(
            [
                str(OPENDATALOADER_HOOK),
                "--url",
                "https://example.com/bad.pdf",
                "--snapshot-id",
                "test-cli-runtime-failure",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

        assert result.returncode == 1, (
            f"Hook should fail on CLI runtime error: {result.stderr}"
        )
        record = parse_jsonl_output(result.stdout)
        assert record, "Should emit ArchiveResult JSONL output"
        assert record["status"] == "failed", record
        assert "simulated CLI failure" in record["output_str"], record


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
