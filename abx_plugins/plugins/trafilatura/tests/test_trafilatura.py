"""
Integration tests for trafilatura plugin.

Tests verify:
1. Hook script exists
2. required_binaries can resolve the trafilatura binary
3. Extraction runs with real trafilatura binary on local HTML sourced from pytest-httpserver
"""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import requests

from abx_plugins.plugins.base.test_utils import (
    get_hook_script,
    get_plugin_dir,
    install_required_binary_from_config,
    parse_jsonl_output,
)

PLUGIN_DIR = get_plugin_dir(__file__)
_TRAFILATURA_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__[0-9]*_trafilatura.*")
if _TRAFILATURA_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
TRAFILATURA_HOOK = _TRAFILATURA_HOOK
TEST_URL = "https://example.com"

_trafilatura_binary_path = None


def get_trafilatura_binary_path() -> str | None:
    """Install trafilatura using abxpkg and return installed binary path."""
    global _trafilatura_binary_path
    if _trafilatura_binary_path and Path(_trafilatura_binary_path).is_file():
        return _trafilatura_binary_path

    loaded = install_required_binary_from_config(PLUGIN_DIR, "trafilatura")
    _trafilatura_binary_path = str(loaded.loaded_abspath or "")
    return _trafilatura_binary_path or None


def require_trafilatura_binary() -> str:
    binary_path = get_trafilatura_binary_path()
    assert binary_path, (
        "trafilatura dependency resolution failed. required_binaries should resolve "
        "the binary automatically in this test environment."
    )
    assert Path(binary_path).is_file(), (
        f"trafilatura binary path invalid: {binary_path}"
    )
    return binary_path


def test_hook_script_exists():
    assert TRAFILATURA_HOOK.exists(), f"Hook script not found: {TRAFILATURA_HOOK}"


def test_verify_deps_with_install_hooks():
    binary_path = require_trafilatura_binary()
    assert Path(binary_path).is_file(), (
        f"Binary path must be a valid file: {binary_path}"
    )


def test_extracts_local_html_outputs_with_real_binary(httpserver):
    binary_path = require_trafilatura_binary()
    test_url = httpserver.url_for("/trafilatura-article")

    httpserver.expect_request("/trafilatura-article").respond_with_data(
        "<html><head><title>Trafilatura Test Article</title>"
        '<meta property="article:tag" content="alpha">'
        '<meta property="article:tag" content="beta"></head><body>'
        "<article><h1>Example Domain</h1>"
        "<p>This domain is for use in illustrative examples in documents.</p>"
        "<p>More information can be found in the docs.</p>"
        "</article></body></html>",
        content_type="text/html; charset=utf-8",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        singlefile_dir = snap_dir / "singlefile"
        singlefile_dir.mkdir(parents=True, exist_ok=True)

        response = requests.get(test_url, timeout=10)
        response.raise_for_status()
        (singlefile_dir / "singlefile.html").write_text(response.text, encoding="utf-8")

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["TRAFILATURA_BINARY"] = binary_path
        env["TRAFILATURA_OUTPUT_FORMATS"] = "txt,markdown,html,json"

        result = subprocess.run(
            [
                str(TRAFILATURA_HOOK),
                "--url",
                test_url,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "succeeded", f"Should succeed: {result_json}"

        output_dir = snap_dir / "trafilatura"
        txt_file = output_dir / "content.txt"
        md_file = output_dir / "content.md"
        html_file = output_dir / "content.html"
        json_file = output_dir / "content.json"

        assert txt_file.exists(), "content.txt not created"
        assert md_file.exists(), "content.md not created"
        assert html_file.exists(), "content.html not created"
        assert json_file.exists(), "content.json not created"

        txt_content = txt_file.read_text(errors="ignore").lower()
        md_content = md_file.read_text(errors="ignore").lower()
        html_content = html_file.read_text(errors="ignore").lower()
        json_content = json_file.read_text(errors="ignore").lower()

        assert "example domain" in txt_content, (
            "Expected article content in text output"
        )
        assert "example domain" in md_content, (
            "Expected article content in markdown output"
        )
        assert "example domain" in html_content, (
            "Expected article content in html output"
        )
        assert "example domain" in json_content, (
            "Expected article content in json output"
        )


def test_output_format_toggles_map_to_expected_files(httpserver):
    binary_path = require_trafilatura_binary()
    test_url = httpserver.url_for("/trafilatura-format-test")

    httpserver.expect_request("/trafilatura-format-test").respond_with_data(
        "<html><head><title>Trafilatura Format Test</title></head><body>"
        "<article><h1>Format Coverage</h1>"
        "<p>This article is used to verify output format toggles.</p>"
        "<p>It should produce csv, xml, and xmltei when enabled.</p>"
        "</article></body></html>",
        content_type="text/html; charset=utf-8",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        singlefile_dir = snap_dir / "singlefile"
        singlefile_dir.mkdir(parents=True, exist_ok=True)

        response = requests.get(test_url, timeout=10)
        response.raise_for_status()
        (singlefile_dir / "singlefile.html").write_text(response.text, encoding="utf-8")

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["TRAFILATURA_BINARY"] = binary_path
        env["TRAFILATURA_OUTPUT_FORMATS"] = "csv,xml,xmltei"

        result = subprocess.run(
            [
                str(TRAFILATURA_HOOK),
                "--url",
                test_url,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        output_dir = snap_dir / "trafilatura"
        assert (output_dir / "content.csv").exists(), "content.csv not created"
        assert (output_dir / "content.xml").exists(), "content.xml not created"
        assert (output_dir / "content.xmltei").exists(), "content.xmltei not created"
        assert not (output_dir / "content.txt").exists(), (
            "content.txt should be disabled"
        )
        assert not (output_dir / "content.md").exists(), "content.md should be disabled"
        assert not (output_dir / "content.html").exists(), (
            "content.html should be disabled"
        )
        assert not (output_dir / "content.json").exists(), (
            "content.json should be disabled"
        )

        assert (
            "format coverage"
            in (output_dir / "content.csv").read_text(errors="ignore").lower()
        )
        assert "<doc" in (output_dir / "content.xml").read_text(errors="ignore").lower()
        assert (
            "<tei" in (output_dir / "content.xmltei").read_text(errors="ignore").lower()
        )


def test_outputs_all_supported_formats_together(httpserver):
    binary_path = require_trafilatura_binary()
    test_url = httpserver.url_for("/trafilatura-all-formats")

    httpserver.expect_request("/trafilatura-all-formats").respond_with_data(
        "<html><head><title>Trafilatura All Formats</title></head><body>"
        "<article><h1>All Format Coverage</h1>"
        "<p>This article verifies all supported output format toggles together.</p>"
        "</article></body></html>",
        content_type="text/html; charset=utf-8",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        singlefile_dir = snap_dir / "singlefile"
        singlefile_dir.mkdir(parents=True, exist_ok=True)

        response = requests.get(test_url, timeout=10)
        response.raise_for_status()
        (singlefile_dir / "singlefile.html").write_text(response.text, encoding="utf-8")

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["TRAFILATURA_BINARY"] = binary_path
        env["TRAFILATURA_OUTPUT_FORMATS"] = "txt,markdown,html,csv,json,xml,xmltei"

        result = subprocess.run(
            [
                str(TRAFILATURA_HOOK),
                "--url",
                test_url,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"
        output_dir = snap_dir / "trafilatura"
        assert (output_dir / "content.txt").exists(), "content.txt not created"
        assert (output_dir / "content.md").exists(), "content.md not created"
        assert (output_dir / "content.html").exists(), "content.html not created"
        assert (output_dir / "content.csv").exists(), "content.csv not created"
        assert (output_dir / "content.json").exists(), "content.json not created"
        assert (output_dir / "content.xml").exists(), "content.xml not created"
        assert (output_dir / "content.xmltei").exists(), "content.xmltei not created"


def test_fails_without_html_source():
    binary_path = require_trafilatura_binary()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["TRAFILATURA_BINARY"] = binary_path
        result = subprocess.run(
            [
                str(TRAFILATURA_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, "Should exit 0 without HTML source"
        assert "no html source" in (result.stdout + result.stderr).lower()
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "noresults"


def test_prefers_dom_output_over_singlefile_when_both_exist():
    binary_path = require_trafilatura_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)

        dom_dir = snap_dir / "dom"
        dom_dir.mkdir(parents=True, exist_ok=True)
        (dom_dir / "output.html").write_text(
            "<html><head><title>DOM Version</title></head><body>"
            "<article><h1>DOM Version</h1>"
            "<p>Prefer this dom article content for trafilatura extraction.</p>"
            "<p>This text is long enough for extraction.</p>"
            "</article></body></html>",
            encoding="utf-8",
        )

        singlefile_dir = snap_dir / "singlefile"
        singlefile_dir.mkdir(parents=True, exist_ok=True)
        (singlefile_dir / "singlefile.html").write_text(
            "<html><head><title>SingleFile Version</title></head><body>"
            "<article><h1>SingleFile Version</h1>"
            "<p>Do not prefer this singlefile content.</p>"
            "<p>This text is long enough for extraction.</p>"
            "</article></body></html>",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["TRAFILATURA_BINARY"] = binary_path

        result = subprocess.run(
            [
                str(TRAFILATURA_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        content_txt = (
            (snap_dir / "trafilatura" / "content.txt")
            .read_text(
                errors="ignore",
            )
            .lower()
        )
        assert "prefer this dom article content" in content_txt
        assert "do not prefer this singlefile content" not in content_txt


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
