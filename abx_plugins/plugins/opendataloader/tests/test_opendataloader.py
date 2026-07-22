"""
Integration tests for opendataloader plugin.

Tests verify:
1. Hook script exists
2. required_binaries can resolve the opendataloader-pdf binary
3. Extraction runs with real opendataloader-pdf binary on a real live PDF
4. Multiple PDFs are all processed (not just the first)
5. Config options work (enabled/disabled, FORCE_OCR)
6. Handles missing sources gracefully
"""

import json
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
import requests

from abx_plugins.plugins.base.testing import get_hydrated_required_binary
from abx_plugins.plugins.base.testing import install_required_binary_from_config
from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.base.utils import load_required_binary

PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = PLUGIN_DIR.parent
_OPENDATALOADER_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_opendataloader.*"), None)
if _OPENDATALOADER_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
OPENDATALOADER_HOOK = _OPENDATALOADER_HOOK
TEST_URL = "https://example.com"
HYBRID_BINARY_RECORD = {
    "name": "opendataloader-pdf-hybrid",
    "binproviders": "env,uv",
    "min_version": "2.0.0",
    "overrides": {
        "uv": {
            "install_root": "{ABXPKG_LIB_DIR}/uv/packages/opendataloader-hybrid",
            "install_args": ["opendataloader-pdf[hybrid]"],
            "postinstall_scripts": True,
        },
    },
}

# Module-level cache for binary path
_opendataloader_binary_path = None
_opendataloader_hybrid_binary_path = None
_java_binary_path = None


def get_opendataloader_binary_path() -> str | None:
    """Get opendataloader-pdf binary path, installing via abxpkg if needed."""
    global _opendataloader_binary_path
    if _opendataloader_binary_path and Path(_opendataloader_binary_path).is_file():
        return _opendataloader_binary_path

    binary = install_required_binary_from_config(PLUGIN_DIR, "opendataloader-pdf")
    if binary and binary.loaded_abspath:
        _opendataloader_binary_path = str(binary.loaded_abspath)
        return _opendataloader_binary_path

    return None


def require_opendataloader_binary() -> str:
    binary_path = get_opendataloader_binary_path()
    assert binary_path, (
        "opendataloader-pdf dependency resolution failed. required_binaries should resolve "
        "the binary automatically in this test environment."
    )
    assert Path(binary_path).is_file(), (
        f"opendataloader-pdf binary path invalid: {binary_path}"
    )
    return binary_path


def require_opendataloader_hybrid_binary() -> str:
    """Resolve the real hybrid server through the plugin's abxpkg contract."""
    global _opendataloader_hybrid_binary_path
    if (
        _opendataloader_hybrid_binary_path
        and Path(
            _opendataloader_hybrid_binary_path,
        ).is_file()
    ):
        return _opendataloader_hybrid_binary_path

    binary = load_required_binary(
        HYBRID_BINARY_RECORD,
        config=os.environ,
        environ=os.environ,
        install=True,
    )
    assert binary.loaded_abspath is not None, (
        "opendataloader-pdf-hybrid dependency resolution failed"
    )
    _opendataloader_hybrid_binary_path = str(binary.loaded_abspath)
    assert Path(_opendataloader_hybrid_binary_path).is_file()
    return _opendataloader_hybrid_binary_path


@contextmanager
def running_hybrid_server(binary: str):
    """Run the real OCR backend and gate on Uvicorn's startup event."""
    process = subprocess.Popen(
        [
            binary,
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--force-ocr",
            "--device",
            "cpu",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    output_stream = process.stdout
    assert output_stream is not None
    startup_result: queue.Queue[str | None] = queue.Queue(maxsize=1)
    output_lines: list[str] = []

    def consume_output() -> None:
        startup_complete = False
        server_url = None
        published = False
        for line in output_stream:
            output_lines.append(line)
            startup_complete = (
                startup_complete or "Application startup complete" in line
            )
            match = re.search(r"Uvicorn running on (http://127\.0\.0\.1:\d+)", line)
            if match:
                server_url = match.group(1)
            if startup_complete and server_url and not published:
                startup_result.put(server_url)
                published = True
        if not published:
            startup_result.put(None)

    output_thread = threading.Thread(target=consume_output, daemon=True)
    output_thread.start()

    try:
        try:
            server_url = startup_result.get(timeout=120)
        except queue.Empty as error:
            raise AssertionError(
                "Hybrid server did not publish its startup event:\n"
                + "".join(output_lines),
            ) from error
        assert server_url, (
            f"Hybrid server exited with {process.poll()} before startup:\n"
            + "".join(output_lines)
        )
        health = requests.get(f"{server_url}/health", timeout=10)
        assert health.status_code == 200, health.text
        assert health.json() == {"status": "ok"}, health.text
        yield server_url
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        output_thread.join(timeout=5)


def get_java_binary_path() -> str | None:
    """Get a Java 11+ binary path, installing via abxpkg if needed."""
    global _java_binary_path
    if _java_binary_path and Path(_java_binary_path).is_file():
        return _java_binary_path

    binary = install_required_binary_from_config(PLUGIN_DIR, "java")
    if binary and binary.loaded_abspath:
        _java_binary_path = str(binary.loaded_abspath)
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
    """Download the canonical public PDF fixture."""
    url = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    assert response.content.startswith(b"%PDF-"), response.content[:8]
    return response.content


def test_hook_script_exists():
    assert OPENDATALOADER_HOOK.exists(), f"Hook script not found: {OPENDATALOADER_HOOK}"


def test_verify_deps_with_install_hooks():
    binary_path = require_opendataloader_binary()
    assert Path(binary_path).is_file(), (
        f"Binary path must be a valid file: {binary_path}"
    )
    hybrid_binary_path = require_opendataloader_hybrid_binary()
    assert Path(hybrid_binary_path).is_file()


def test_install_hook_requests_java_dependency():
    java_record = get_hydrated_required_binary(PLUGIN_DIR, "java")
    assert java_record["min_version"] == "11.0.0"
    assert java_record["overrides"]["brew"]["install_args"] == ["openjdk"]
    assert java_record["binproviders"] == "env,apt,brew"


def test_opendataloader_env_executes_exact_abxpkg_selected_java():
    from abx_plugins.plugins.opendataloader.on_Snapshot__60_opendataloader import (
        _opendataloader_env,
    )

    loaded_java = install_required_binary_from_config(PLUGIN_DIR, "java")
    java_path = Path(str(loaded_java.loaded_abspath or ""))
    assert java_path.is_absolute()
    assert java_path.is_file()

    env = _opendataloader_env(str(java_path))

    assert env is not None
    selected_java = Path(env["PATH"].split(os.pathsep)[0]) / "java"
    assert selected_java.samefile(java_path)
    resolved_java = java_path.resolve()
    java_home = resolved_java.parent.parent
    if (java_home / "release").is_file():
        assert env["JAVA_HOME"] == str(java_home)
        assert (Path(env["JAVA_HOME"]) / "bin" / "java").samefile(resolved_java)
    else:
        assert env.get("JAVA_HOME") == os.environ.get("JAVA_HOME")

    version = subprocess.run(
        [str(java_path), "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert version.returncode == 0, version.stderr


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
        env["OPENDATALOADER_ENABLED"] = "True"

        result = subprocess.run(
            [
                str(OPENDATALOADER_HOOK),
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
    """Test extraction on a single real PDF downloaded from the web."""
    binary_path = require_opendataloader_binary()
    java_binary = require_java_binary()
    pdf_content = _download_test_pdf()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"

        # Place PDF as if the responses plugin saved an original PDF response.
        responses_dir = snap_dir / "responses" / "application" / "example.com"
        responses_dir.mkdir(parents=True, exist_ok=True)
        (responses_dir / "output.pdf").write_bytes(pdf_content)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["OPENDATALOADER_BINARY"] = binary_path
        env["JAVA_BINARY"] = java_binary
        env["OPENDATALOADER_ENABLED"] = "True"

        result = subprocess.run(
            [
                str(OPENDATALOADER_HOOK),
                "--url",
                "https://example.com/test.pdf",
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

    Places PDFs in both responses/ and staticfile/ directories and verifies
    the hook processes every one, not just the first.
    """
    binary_path = require_opendataloader_binary()
    java_binary = require_java_binary()
    pdf_content = _download_test_pdf()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"

        # Place PDF in responses/ as if the server served a PDF
        responses_dir = snap_dir / "responses" / "application" / "example.com"
        responses_dir.mkdir(parents=True, exist_ok=True)
        (responses_dir / "document.pdf").write_bytes(pdf_content)

        # Place another PDF in staticfile/ as if a linked PDF was downloaded
        staticfile_dir = snap_dir / "staticfile" / "example.com"
        staticfile_dir.mkdir(parents=True, exist_ok=True)
        (staticfile_dir / "linked.pdf").write_bytes(pdf_content)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["OPENDATALOADER_BINARY"] = binary_path
        env["JAVA_BINARY"] = java_binary
        env["OPENDATALOADER_ENABLED"] = "True"

        result = subprocess.run(
            [
                str(OPENDATALOADER_HOOK),
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
    """Test FORCE_OCR through the real hybrid extraction lifecycle."""
    binary_path = require_opendataloader_binary()
    hybrid_binary = require_opendataloader_hybrid_binary()
    java_binary = require_java_binary()
    pdf_content = _download_test_pdf()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"

        responses_dir = snap_dir / "responses" / "application" / "example.com"
        responses_dir.mkdir(parents=True, exist_ok=True)
        (responses_dir / "output.pdf").write_bytes(pdf_content)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["OPENDATALOADER_BINARY"] = binary_path
        env["JAVA_BINARY"] = java_binary
        env["OPENDATALOADER_ENABLED"] = "True"
        env["OPENDATALOADER_FORCE_OCR"] = "true"

        with running_hybrid_server(hybrid_binary) as hybrid_url:
            env["OPENDATALOADER_HYBRID_URL"] = hybrid_url
            result = subprocess.run(
                [
                    str(OPENDATALOADER_HOOK),
                    "--url",
                    "https://example.com/scanned.pdf",
                ],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )

        assert result.returncode == 0, result.stderr

        record = parse_jsonl_output(result.stdout)
        assert record, "Should have ArchiveResult JSONL output"
        assert record["status"] == "succeeded", (
            f"FORCE_OCR must succeed through the hybrid backend, got: {record}. "
            f"stderr: {result.stderr}"
        )

        # Verify actual content was extracted (not a no-op pass)
        output_file = snap_dir / record["output_str"]
        assert output_file.exists(), f"Output file {record['output_str']} not created"
        content = output_file.read_text(errors="ignore")
        assert len(content) > 10, (
            f"Output too short, extraction may be broken: {content!r}"
        )

        assert "opendataloader" in record["output_str"].lower(), record


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
