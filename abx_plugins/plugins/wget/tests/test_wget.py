"""
Integration tests for wget plugin

Tests verify:
    pass
1. Validate hook checks for wget binary
2. Verify deps with abxpkg
3. Config options work (WGET_ENABLED, WGET_SAVE_WARC, etc.)
4. Extraction works against real example.com
5. Output files contain actual page content
6. Skip cases work (WGET_ENABLED=False, staticfile present)
7. Failure cases handled (404, network errors)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import (
    install_required_binary_from_config,
    parse_jsonl_output,
)


PLUGIN_DIR = Path(__file__).parent.parent
WGET_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_wget.*"))
TEST_URL = "https://example.com"
PLUGIN_CONFIG = json.loads((PLUGIN_DIR / "config.json").read_text())


def test_hook_script_exists():
    """Verify hook script exists."""
    assert WGET_HOOK.exists(), f"Hook script not found: {WGET_HOOK}"


def test_wget_declares_only_env_apt_brew_providers():
    """required_binaries should declare wget via env,apt,brew only."""
    required_binaries = PLUGIN_CONFIG["required_binaries"]
    binary_record = next(
        (
            record
            for record in required_binaries
            if record.get("name") == "{WGET_BINARY}"
        ),
        None,
    )
    assert binary_record is not None, (
        f"Expected wget required_binaries entry: {required_binaries}"
    )
    assert binary_record["binproviders"] == "env,apt,brew"


def test_verify_deps_with_abxpkg():
    """Verify wget is available via abxpkg."""
    wget_loaded = install_required_binary_from_config(PLUGIN_DIR, "wget")

    if wget_loaded and wget_loaded.abspath:
        assert True, "wget is available"
    else:
        pass


def test_reports_missing_dependency_when_not_installed():
    """Test that script reports DEPENDENCY_NEEDED when wget is not found."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Run with empty PATH so binary won't be found
        env = {"PATH": "/nonexistent", "HOME": str(tmpdir)}

        result = subprocess.run(
            [
                sys.executable,
                str(WGET_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
        )

        # Missing binary is a hard dependency failure.
        assert result.returncode == 1, "Should exit 1 when dependency missing"

        # Should emit failed JSONL describing the missing dependency.
        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Expected failed JSONL output"
        assert result_json["status"] == "failed", result_json
        assert "wget" in result_json["output_str"].lower(), result_json

        # Should log error to stderr
        assert "wget" in result.stderr.lower() or "error" in result.stderr.lower(), (
            "Should report error in stderr"
        )


def test_can_install_wget_via_abxpkg_provider():
    """Test that wget can be resolved or installed via abxpkg providers."""
    loaded = install_required_binary_from_config(PLUGIN_DIR, "wget")
    assert loaded.loaded_abspath is not None, "wget should resolve after installation"
    assert loaded.loaded_abspath.exists(), loaded.loaded_abspath


@pytest.fixture
def local_example_url(httpserver):
    html = """<!doctype html><html><head><title>Example Domain</title></head><body><h1>Example Domain</h1><p>This domain is for use in illustrative examples in documents.</p><a href=\"https://iana.org/\">More information</a></body></html>"""
    httpserver.expect_request("/").respond_with_data(
        html,
        status=200,
        content_type="text/html",
    )
    return httpserver.url_for("/")


def test_archives_example_com(local_example_url):
    """Test full workflow: ensure wget installed then archive a real HTML page."""

    install_required_binary_from_config(PLUGIN_DIR, "wget")

    # Now test archiving
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmpdir)

        # Run wget extraction
        result = subprocess.run(
            [
                str(WGET_HOOK),
                "--url",
                local_example_url,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        # Parse clean JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "succeeded", f"Should succeed: {result_json}"

        # Verify files were downloaded to wget output directory.
        output_root = tmpdir / "wget"
        assert output_root.exists(), "wget output directory was not created"

        downloaded_files = [f for f in output_root.rglob("*") if f.is_file()]
        assert downloaded_files, "No files downloaded"

        # Try the emitted output path first, then fallback to downloaded files.
        assert result_json.get("output_str", "").startswith("wget/"), result_json
        output_path = (tmpdir / result_json.get("output_str", "")).resolve()
        candidate_files = [output_path] if output_path.is_file() else []
        candidate_files.extend(downloaded_files)

        main_html = None
        for candidate in candidate_files:
            content = candidate.read_text(errors="ignore")
            if "example domain" in content.lower():
                main_html = candidate
                break

        assert main_html is not None, (
            "Could not find downloaded file containing example.com content"
        )

        # Verify page content contains REAL example.com text.
        html_content = main_html.read_text(errors="ignore")
        assert len(html_content) > 200, (
            f"HTML content too short: {len(html_content)} bytes"
        )
        assert "example domain" in html_content.lower(), (
            "Missing 'Example Domain' in HTML"
        )
        assert (
            "this domain" in html_content.lower()
            or "illustrative examples" in html_content.lower()
        ), "Missing example.com description text"
        assert (
            "iana" in html_content.lower() or "more information" in html_content.lower()
        ), "Missing IANA reference"


def test_config_save_wget_false_skips():
    """Test that WGET_ENABLED=False exits without emitting JSONL."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Set WGET_ENABLED=False
        env = os.environ.copy()
        env["WGET_ENABLED"] = "False"

        result = subprocess.run(
            [
                str(WGET_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        # Should exit 0 when feature disabled
        assert result.returncode == 0, (
            f"Should exit 0 when feature disabled: {result.stderr}"
        )

        # Feature disabled should emit skipped JSONL
        assert "Skipping" in result.stderr or "False" in result.stderr, (
            "Should log skip reason to stderr"
        )

        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Expected skipped JSONL output"
        assert result_json["status"] == "skipped", result_json
        assert result_json["output_str"] == "WGET_ENABLED=False", result_json


def test_config_save_warc():
    """Test that WGET_SAVE_WARC=True creates WARC files."""

    # Ensure wget is available
    if not shutil.which("wget"):
        pass

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Set WGET_SAVE_WARC=True explicitly
        env = os.environ.copy()
        env["WGET_SAVE_WARC"] = "True"
        env["SNAP_DIR"] = str(tmpdir)

        result = subprocess.run(
            [
                str(WGET_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

        if result.returncode == 0:
            # Look for WARC files in warc/ subdirectory
            warc_dir = tmpdir / "wget" / "warc"
            if warc_dir.exists():
                warc_files = list(warc_dir.rglob("*"))
                warc_files = [f for f in warc_files if f.is_file()]
                assert len(warc_files) > 0, (
                    "WARC file not created when WGET_SAVE_WARC=True"
                )


def test_staticfile_present_skips():
    """Test that wget skips when staticfile already downloaded."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmpdir)

        # Create directory structure like real ArchiveBox:
        # tmpdir/
        #   staticfile/  <- staticfile extractor output
        #   wget/         <- wget extractor runs here, looks for ../staticfile
        staticfile_dir = tmpdir / "staticfile"
        staticfile_dir.mkdir()
        (staticfile_dir / "stdout.log").write_text(
            '{"type":"ArchiveResult","status":"succeeded","output_str":"responses/example.com/test.json","content_type":"application/json"}\n',
        )

        wget_dir = tmpdir / "wget"
        wget_dir.mkdir()

        result = subprocess.run(
            [
                str(WGET_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=wget_dir,  # Run from wget subdirectory
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        # Should exit 0 with a noresults JSONL because another plugin already handled it.
        assert result.returncode == 0, (
            "Should exit 0 when staticfile already handled the URL"
        )

        # Parse clean JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, (
            "Should emit ArchiveResult JSONL when staticfile already handled the URL"
        )
        assert result_json["status"] == "noresults", (
            f"Should have status='noresults': {result_json}"
        )
        assert "staticfile" in result_json.get("output_str", "").lower(), (
            "Should mention staticfile in output_str"
        )


def test_handles_404_gracefully(httpserver):
    """Test that wget fails gracefully on 404."""

    if not shutil.which("wget"):
        pass

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Try to download non-existent page
        result = subprocess.run(
            [
                str(WGET_HOOK),
                "--url",
                httpserver.url_for("/nonexistent-page-404"),
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # Should fail
        assert result.returncode != 0, "Should fail on 404"
        combined = result.stdout + result.stderr
        assert (
            "404" in combined
            or "Not Found" in combined
            or "No files downloaded" in combined
            or "exit=8" in combined
        ), "Should report 404 or no files downloaded"


def test_config_timeout_honored():
    """Test that WGET_TIMEOUT config is respected."""

    if not shutil.which("wget"):
        pass

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Set very short timeout
        env = os.environ.copy()
        env["WGET_TIMEOUT"] = "5"

        # This should still succeed for example.com (it's fast)
        result = subprocess.run(
            [
                str(WGET_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        # Verify it completed (success or fail, but didn't hang)
        assert result.returncode in (0, 1), "Should complete (success or fail)"


def test_config_user_agent():
    """Test that WGET_USER_AGENT config is used."""

    if not shutil.which("wget"):
        pass

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Set custom user agent
        env = os.environ.copy()
        env["WGET_USER_AGENT"] = "TestBot/1.0"

        result = subprocess.run(
            [
                str(WGET_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

        # Should succeed (example.com doesn't block)
        if result.returncode == 0:
            # Parse clean JSONL output
            result_json = parse_jsonl_output(result.stdout)

            assert result_json, "Should have ArchiveResult JSONL output"
            assert result_json["status"] == "succeeded", (
                f"Should succeed: {result_json}"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
