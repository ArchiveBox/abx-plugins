"""
Integration tests for trafilatura plugin.

Tests verify:
1. Hook script exists
2. Install hooks can install trafilatura binary
3. Extraction runs with real trafilatura binary on local HTML sourced from pytest-httpserver
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
import requests

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    get_hook_script,
    get_plugin_dir,
    parse_jsonl_output,
)

PLUGIN_DIR = get_plugin_dir(__file__)
PLUGINS_ROOT = PLUGIN_DIR.parent
_TRAFILATURA_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__[0-9]*_trafilatura.*")
if _TRAFILATURA_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
TRAFILATURA_HOOK = _TRAFILATURA_HOOK
TEST_URL = "https://example.com"

_trafilatura_binary_path = None
_trafilatura_lib_root = None


def _script_cmd(script: Path) -> list[str]:
    if shutil.which("uv"):
        return ["uv", "run", str(script)]
    return [sys.executable, str(script)]


def get_trafilatura_binary_path() -> str | None:
    """Install trafilatura using real plugin hooks and return installed binary path."""
    global _trafilatura_binary_path
    if _trafilatura_binary_path and Path(_trafilatura_binary_path).is_file():
        return _trafilatura_binary_path

    pip_hook = PLUGINS_ROOT / "pip" / "on_Binary__11_pip_install.py"
    crawl_hook = PLUGIN_DIR / "on_Crawl__41_trafilatura_install.finite.bg.py"
    if not pip_hook.exists():
        return None

    binproviders = "*"
    overrides = None

    if crawl_hook.exists():
        crawl_result = subprocess.run(
            _script_cmd(crawl_hook),
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
            if record.get("type") == "Binary" and record.get("name") == "trafilatura":
                binproviders = record.get("binproviders", "*")
                overrides = record.get("overrides")
                break

    global _trafilatura_lib_root
    if not _trafilatura_lib_root:
        _trafilatura_lib_root = tempfile.mkdtemp(prefix="trafilatura-lib-")

    env = os.environ.copy()
    env["LIB_DIR"] = str(Path(_trafilatura_lib_root) / "lib")
    env["SNAP_DIR"] = str(Path(_trafilatura_lib_root) / "data")
    env["CRAWL_DIR"] = str(Path(_trafilatura_lib_root) / "crawl")

    cmd = [
        *_script_cmd(pip_hook),
        "--binary-id",
        str(uuid.uuid4()),
        "--machine-id",
        str(uuid.uuid4()),
        "--name",
        "trafilatura",
        f"--binproviders={binproviders}",
    ]
    if overrides:
        cmd.append(f"--overrides={json.dumps(overrides)}")

    install_result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    for line in install_result.stdout.strip().split("\n"):
        if not line.strip().startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "Binary" and record.get("name") == "trafilatura":
            _trafilatura_binary_path = record.get("abspath")
            return _trafilatura_binary_path

    return None


def require_trafilatura_binary() -> str:
    binary_path = get_trafilatura_binary_path()
    assert binary_path, (
        "trafilatura installation failed. Install hook should install "
        "the binary automatically in this test environment."
    )
    assert Path(binary_path).is_file(), f"trafilatura binary path invalid: {binary_path}"
    return binary_path


def test_hook_script_exists():
    assert TRAFILATURA_HOOK.exists(), f"Hook script not found: {TRAFILATURA_HOOK}"


def test_verify_deps_with_install_hooks():
    binary_path = require_trafilatura_binary()
    assert Path(binary_path).is_file(), f"Binary path must be a valid file: {binary_path}"


def test_extracts_local_html_outputs_with_real_binary(httpserver):
    binary_path = require_trafilatura_binary()
    test_url = httpserver.url_for("/trafilatura-article")

    httpserver.expect_request("/trafilatura-article").respond_with_data(
        "<html><head><title>Trafilatura Test Article</title></head><body>"
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
        env["TRAFILATURA_OUTPUT_JSON"] = "true"

        result = subprocess.run(
            [
                sys.executable,
                str(TRAFILATURA_HOOK),
                "--url",
                test_url,
                "--snapshot-id",
                "test123",
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

        assert "example domain" in txt_content, "Expected article content in text output"
        assert "example domain" in md_content, "Expected article content in markdown output"
        assert "example domain" in html_content, "Expected article content in html output"
        assert "example domain" in json_content, "Expected article content in json output"


def test_extracts_local_html_with_binary_resolved_from_path(chrome_test_url):
    binary_path = require_trafilatura_binary()
    test_url = chrome_test_url

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
        env["TRAFILATURA_BINARY"] = "trafilatura"
        env["PATH"] = f"{Path(binary_path).parent}:{env.get('PATH', '')}"

        result = subprocess.run(
            [
                sys.executable,
                str(TRAFILATURA_HOOK),
                "--url",
                test_url,
                "--snapshot-id",
                "test-path-resolution",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"
        assert (snap_dir / "trafilatura" / "content.txt").exists(), "content.txt not created"
        assert "example domain" in (snap_dir / "trafilatura" / "content.txt").read_text(
            errors="ignore"
        ).lower()


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
        env["TRAFILATURA_OUTPUT_TXT"] = "false"
        env["TRAFILATURA_OUTPUT_MARKDOWN"] = "false"
        env["TRAFILATURA_OUTPUT_HTML"] = "false"
        env["TRAFILATURA_OUTPUT_JSON"] = "false"
        env["TRAFILATURA_OUTPUT_CSV"] = "true"
        env["TRAFILATURA_OUTPUT_XML"] = "true"
        env["TRAFILATURA_OUTPUT_XMLTEI"] = "true"

        result = subprocess.run(
            [
                sys.executable,
                str(TRAFILATURA_HOOK),
                "--url",
                test_url,
                "--snapshot-id",
                "test-formats",
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
        assert not (output_dir / "content.txt").exists(), "content.txt should be disabled"
        assert not (output_dir / "content.md").exists(), "content.md should be disabled"
        assert not (output_dir / "content.html").exists(), "content.html should be disabled"
        assert not (output_dir / "content.json").exists(), "content.json should be disabled"

        assert "format coverage" in (output_dir / "content.csv").read_text(
            errors="ignore"
        ).lower()
        assert "<doc" in (output_dir / "content.xml").read_text(errors="ignore").lower()
        assert "<tei" in (output_dir / "content.xmltei").read_text(
            errors="ignore"
        ).lower()


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
        env["TRAFILATURA_OUTPUT_TXT"] = "true"
        env["TRAFILATURA_OUTPUT_MARKDOWN"] = "true"
        env["TRAFILATURA_OUTPUT_HTML"] = "true"
        env["TRAFILATURA_OUTPUT_CSV"] = "true"
        env["TRAFILATURA_OUTPUT_JSON"] = "true"
        env["TRAFILATURA_OUTPUT_XML"] = "true"
        env["TRAFILATURA_OUTPUT_XMLTEI"] = "true"

        result = subprocess.run(
            [
                sys.executable,
                str(TRAFILATURA_HOOK),
                "--url",
                test_url,
                "--snapshot-id",
                "test-all-formats",
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
                sys.executable,
                str(TRAFILATURA_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test999",
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
