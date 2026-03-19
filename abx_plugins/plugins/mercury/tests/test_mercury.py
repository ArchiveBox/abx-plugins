"""
Integration tests for mercury plugin

Tests verify:
1. Hook script exists
2. Dependencies installed via validation hooks
3. Verify deps with abx-pkg
4. Mercury extraction works on deterministic local fixture HTML
5. JSONL output is correct
6. Filesystem output contains extracted content
7. Config options work
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    get_plugin_dir,
    get_hook_script,
    parse_jsonl_output,
)


PLUGIN_DIR = get_plugin_dir(__file__)
PLUGINS_ROOT = PLUGIN_DIR.parent
_MERCURY_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_mercury.*")
if _MERCURY_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
MERCURY_HOOK = _MERCURY_HOOK
TEST_URL = "https://example.com"

# Module-level cache for binary path
_mercury_binary_path = None


def require_mercury_binary() -> str:
    """Return postlight-parser binary path or fail with actionable context."""
    binary_path = get_mercury_binary_path()
    assert binary_path, (
        "postlight-parser installation failed. Install hook should install "
        "the binary automatically in this test environment."
    )
    assert Path(binary_path).is_file(), (
        f"postlight-parser binary path invalid: {binary_path}"
    )
    return binary_path


def get_mercury_binary_path() -> str | None:
    """Get postlight-parser binary path, installing via abx_pkg if needed."""
    global _mercury_binary_path
    if _mercury_binary_path and Path(_mercury_binary_path).is_file():
        return _mercury_binary_path

    from abx_pkg import Binary, NpmProvider, EnvProvider

    binary = Binary(
        name="postlight-parser",
        binproviders=[NpmProvider(), EnvProvider()],
        overrides={"npm": {"install_args": ["@postlight/parser"]}},
    ).load_or_install()
    if binary and binary.abspath:
        _mercury_binary_path = str(binary.abspath)
        return _mercury_binary_path

    return None


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert MERCURY_HOOK.exists(), f"Hook not found: {MERCURY_HOOK}"


def test_verify_deps_with_abx_pkg():
    """Verify postlight-parser is installed by real plugin install hooks."""
    binary_path = require_mercury_binary()
    assert Path(binary_path).is_file(), (
        f"Binary path must be a valid file: {binary_path}"
    )


def test_extracts_with_mercury_parser(httpserver):
    """Test full workflow: extract with postlight-parser from local fixture HTML."""
    binary_path = require_mercury_binary()
    test_url = httpserver.url_for("/mercury-article")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir
        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["MERCURY_BINARY"] = binary_path

        # Serve deterministic HTML source that mercury can parse.
        httpserver.expect_request("/mercury-article").respond_with_data(
            "<html><head><title>Test Article</title></head><body>"
            "<article><h1>Example Article</h1><p>This is test content for mercury parser.</p></article>"
            "</body></html>",
            content_type="text/html; charset=utf-8",
        )

        # Run mercury extraction hook
        result = subprocess.run(
            [str(MERCURY_HOOK),
                "--url",
                test_url,
                "--snapshot-id",
                "test789",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        # Parse clean JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "succeeded", f"Should succeed: {result_json}"

        # Verify filesystem output (hook writes to current directory)
        output_file = snap_dir / "mercury" / "content.html"
        assert output_file.exists(), "content.html not created"

        content = output_file.read_text()
        assert len(content) > 0, "Output should not be empty"


def test_extracts_with_local_html_source_present(httpserver):
    """Test real mercury extraction when local singlefile source is present."""
    binary_path = require_mercury_binary()
    test_url = httpserver.url_for("/mercury-with-local-source")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        httpserver.expect_request("/mercury-with-local-source").respond_with_data(
            "<html><head><title>Remote Source</title></head><body>"
            "<article><h1>Remote Source Marker</h1><p>Fetched URL content for mercury parser.</p></article>"
            "</body></html>",
            content_type="text/html; charset=utf-8",
        )

        # Create local singlefile source to cover the 'local source exists' path.
        singlefile_dir = tmpdir / "singlefile"
        singlefile_dir.mkdir(parents=True, exist_ok=True)
        (singlefile_dir / "singlefile.html").write_text(
            "<html><head><title>Local Source</title></head><body>"
            "<article><h1>Local Source Marker</h1><p>Local singlefile fixture content.</p></article>"
            "</body></html>",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmpdir)
        env["MERCURY_BINARY"] = binary_path

        result = subprocess.run(
            [str(MERCURY_HOOK),
                "--url",
                test_url,
                "--snapshot-id",
                "test-local-source",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "succeeded", f"Should succeed: {result_json}"

        output_file = tmpdir / "mercury" / "content.html"
        assert output_file.exists(), "content.html not created"

        extracted_html = output_file.read_text(errors="ignore")
        extracted_lower = extracted_html.lower()
        assert len(extracted_html) > 50, "Extracted HTML should not be trivially short"
        assert "<" in extracted_lower and ">" in extracted_lower, (
            f"Extracted HTML does not look like HTML. Output: {extracted_html[:500]}"
        )

        content_txt = tmpdir / "mercury" / "content.txt"
        assert content_txt.exists(), "content.txt not created"
        extracted_text = content_txt.read_text(errors="ignore").strip()
        assert len(extracted_text) > 10, "Extracted text should not be empty"

        article_json = tmpdir / "mercury" / "article.json"
        assert article_json.exists(), "article.json not created"
        metadata = json.loads(article_json.read_text())
        assert metadata.get("title"), (
            f"Expected non-empty title in metadata: {metadata}"
        )


def test_config_save_mercury_false_skips():
    """Test that MERCURY_ENABLED=False exits with skipped JSONL."""
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        snap_dir = Path(tmpdir)
        env = os.environ.copy()
        env["MERCURY_ENABLED"] = "False"
        env["SNAP_DIR"] = str(snap_dir)

        result = subprocess.run(
            [str(MERCURY_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test999",
            ],
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

        record = parse_jsonl_output(result.stdout)
        assert record, "Should emit JSONL when disabled"
        assert record["type"] == "ArchiveResult"
        assert record["status"] == "skipped"


def test_extracts_without_local_html_source(httpserver):
    """Test real mercury extraction from fetched HTML when no local source file exists."""
    binary_path = require_mercury_binary()
    test_url = httpserver.url_for("/mercury-no-html-source")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        httpserver.expect_request("/mercury-no-html-source").respond_with_data(
            "<html><head><title>No Local HTML Source</title></head><body>"
            "<article><h1>Remote Article</h1><p>Fetched directly by mercury parser.</p></article>"
            "</body></html>",
            content_type="text/html; charset=utf-8",
        )

        # Ensure this path tests remote fetch extraction (no local singlefile source exists).
        assert not (tmpdir / "singlefile" / "singlefile.html").exists()

        env = os.environ.copy()
        env["MERCURY_BINARY"] = binary_path
        env["SNAP_DIR"] = str(tmpdir)
        result = subprocess.run(
            [str(MERCURY_HOOK),
                "--url",
                test_url,
                "--snapshot-id",
                "test999",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        assert result.returncode == 0, f"Mercury fetch/parse failed: {result.stderr}"

        # Mercury fetches URL directly with postlight-parser, doesn't need local HTML source
        # Parse clean JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should emit ArchiveResult"
        assert result_json["status"] == "succeeded", f"Should succeed: {result_json}"

        output_file = tmpdir / "mercury" / "content.html"
        assert output_file.exists(), "content.html not created"

        extracted_html = output_file.read_text(errors="ignore")
        extracted_lower = extracted_html.lower()
        assert len(extracted_html) > 50, "Extracted HTML should not be trivially short"
        assert (
            "remote article" in extracted_lower or "fetched directly" in extracted_lower
        ), f"Expected extracted article content missing. Output: {extracted_html[:500]}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
