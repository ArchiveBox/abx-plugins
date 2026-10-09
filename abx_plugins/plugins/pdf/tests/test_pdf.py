"""
Integration tests for pdf plugin

Tests verify:
1. Hook script exists
2. Dependencies installed via chrome validation hooks
3. Verify deps with abxpkg
4. PDF extraction works on https://example.com
5. JSONL output is correct
6. Filesystem output is valid PDF file
7. Config options work
"""

import json
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest
from werkzeug.wrappers import Response

from abx_plugins.plugins.base.testing import (
    get_hook_script,
    get_plugin_dir,
    install_binary_with_abxpkg,
    parse_jsonl_output,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    chrome_session,
    get_test_env,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


PLUGIN_DIR = get_plugin_dir(__file__)
_PDF_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_pdf.*")
if _PDF_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
PDF_HOOK = _PDF_HOOK
TEST_URL = "https://example.com"


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert PDF_HOOK.exists(), f"Hook not found: {PDF_HOOK}"


def test_verify_deps_with_abxpkg():
    """Verify dependencies are available via abxpkg after hook installation."""
    node_loaded = install_binary_with_abxpkg(
        "node",
        binproviders="env,node,brew,apt",
    )
    assert node_loaded and node_loaded.abspath, "Node.js required for pdf plugin"


def test_extracts_pdf_from_example_com(chrome_test_url):
    """Test full workflow: extract PDF from deterministic local fixture via hook."""
    # Prerequisites checked by earlier test

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        with chrome_session(tmpdir, test_url=chrome_test_url, timeout=30) as (
            _process,
            _pid,
            snapshot_chrome_dir,
            env,
        ):
            pdf_dir = snapshot_chrome_dir.parent / "pdf"
            pdf_dir.mkdir(exist_ok=True)

            # Run PDF extraction hook
            result = subprocess.run(
                [
                    str(PDF_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=test789",
                ],
                cwd=pdf_dir,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )

        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "succeeded", result_json
        assert result.returncode == 0, f"Should exit 0 on success: {result.stderr}"

        # Verify filesystem output (hook writes to current directory)
        pdf_file = pdf_dir / "output.pdf"
        assert pdf_file.exists(), "output.pdf not created"

        # Verify file is valid PDF
        file_size = pdf_file.stat().st_size
        assert file_size > 500, f"PDF too small: {file_size} bytes"
        assert file_size < 10 * 1024 * 1024, (
            f"PDF suspiciously large: {file_size} bytes"
        )

        # Check PDF magic bytes
        pdf_data = pdf_file.read_bytes()
        assert pdf_data[:4] == b"%PDF", "Should be valid PDF file"
        preview_file = pdf_dir / "preview.png"
        assert preview_file.is_file(), "First PDF page should have a card preview"
        assert preview_file.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_pdf_render_honors_configured_timeout(tmp_path, httpserver, chrome_test_url):
    """A real pending web font must use PDF_TIMEOUT, not Puppeteer's 30s default."""
    font_started = threading.Event()
    release_font = threading.Event()

    def pending_font(_request):
        font_started.set()
        release_font.wait(timeout=60)
        return Response(status=404)

    httpserver.expect_request("/pending-font.woff2").respond_with_handler(pending_font)
    with chrome_session(tmp_path, test_url=chrome_test_url, timeout=30) as (
        _process,
        _pid,
        snapshot_chrome_dir,
        env,
    ):
        env = env | {"PDF_TIMEOUT": "45"}
        pdf_dir = snapshot_chrome_dir.parent / "pdf"
        pdf_dir.mkdir(exist_ok=True)
        command = [str(PDF_HOOK), f"--url={chrome_test_url}"]
        first = subprocess.run(
            command,
            cwd=pdf_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert first.returncode == 0, first.stderr
        pdf_file = pdf_dir / "output.pdf"
        original = pdf_file.read_bytes()
        assert original.startswith(b"%PDF")

        script = (
            f"const utils = require({json.dumps(str(CHROME_UTILS))});\n"
            + """
        (async () => {
          const { browser, page } = await utils.connectToPage({chromeSessionDir: process.argv[1]});
          try {
            const status = await page.evaluate((url) => {
              const font = new FontFace('PendingCaptureFont', `url(${url})`);
              document.fonts.add(font);
              font.load().catch(() => {});
              return document.fonts.status;
            }, process.argv[2]);
            process.stdout.write(status);
          } finally {
            browser.disconnect();
          }
        })().catch(error => { console.error(error); process.exit(1); });
        """
        )
        font_timer = threading.Timer(35, release_font.set)
        try:
            loading = subprocess.run(
                [
                    env["NODE_BINARY"],
                    "-e",
                    script,
                    str(snapshot_chrome_dir),
                    httpserver.url_for("/pending-font.woff2"),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert loading.returncode == 0, loading.stderr
            assert loading.stdout == "loading"
            assert font_started.wait(timeout=5), "Chrome never requested the web font"
            # Keep the font pending beyond Puppeteer's implicit 30s, but inside
            # the configured PDF/CDP limit; a shorter limit tests CDP instead.
            font_timer.start()
            completed = subprocess.run(
                command,
                cwd=pdf_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=55,
            )
            assert completed.returncode == 0, completed.stderr
            record = parse_jsonl_output(completed.stdout)
            assert record and record["status"] == "succeeded", completed.stdout
            assert pdf_file.read_bytes().startswith(b"%PDF")
        finally:
            font_timer.cancel()
            release_font.set()


def test_config_save_pdf_false_skips():
    """Test that PDF_ENABLED=False exits with skipped JSONL."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        env = get_test_env() | {"SNAP_DIR": str(snap_dir)}
        env["PDF_ENABLED"] = "False"

        result = subprocess.run(
            [str(PDF_HOOK), f"--url={TEST_URL}", "--snapshot-id=test999"],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, (
            f"Should exit 0 when feature disabled: {result.stderr}"
        )

        assert "Skipping" in result.stderr or "False" in result.stderr, (
            "Should log skip reason to stderr"
        )

        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Should emit JSONL when disabled"
        assert result_json["type"] == "ArchiveResult"
        assert result_json["status"] == "skipped"


def test_reports_missing_chrome():
    """Test that script reports error when Chrome session is missing."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        pdf_dir = snap_dir / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        env = get_test_env() | {"SNAP_DIR": str(snap_dir)}

        result = subprocess.run(
            [str(PDF_HOOK), f"--url={TEST_URL}", "--snapshot-id=test123"],
            cwd=pdf_dir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode != 0, "Should fail without shared Chrome session"
        combined = result.stdout + result.stderr
        assert (
            "chrome session" in combined.lower() or "chrome plugin" in combined.lower()
        )
        result_json = parse_jsonl_output(result.stdout)
        assert result_json and result_json["status"] == "failed"


def test_runs_with_shared_chrome_session(chrome_test_url):
    """Test that a shared Chrome session produces a valid PDF result."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        with chrome_session(tmpdir, test_url=chrome_test_url, timeout=30) as (
            _process,
            _pid,
            snapshot_chrome_dir,
            env,
        ):
            pdf_dir = snapshot_chrome_dir.parent / "pdf"
            pdf_dir.mkdir(exist_ok=True)

            result = subprocess.run(
                [
                    str(PDF_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=testtimeout",
                ],
                cwd=pdf_dir,
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )

        assert result.returncode == 0, result.stderr
        result_json = parse_jsonl_output(result.stdout)
        assert result_json is not None, result.stdout
        assert result_json["status"] == "succeeded", result_json
        pdf_file = pdf_dir / "output.pdf"
        assert pdf_file.exists(), "output.pdf not created"
        pdf_data = pdf_file.read_bytes()
        assert pdf_data.startswith(b"%PDF"), "output.pdf is not a PDF"
        assert len(pdf_data) > 500, "output.pdf is unexpectedly small"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
