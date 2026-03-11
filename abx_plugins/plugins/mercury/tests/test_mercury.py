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
import uuid
from pathlib import Path
import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    get_plugin_dir,
    get_hook_script,
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
_mercury_lib_root = None


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


def get_mercury_binary_path():
    """Get postlight-parser path from cache or by running install hooks."""
    global _mercury_binary_path
    if _mercury_binary_path and Path(_mercury_binary_path).is_file():
        return _mercury_binary_path

    from abx_pkg import Binary, NpmProvider, EnvProvider

    try:
        binary = Binary(
            name="postlight-parser",
            binproviders=[NpmProvider(), EnvProvider()],
            overrides={"npm": {"packages": ["@postlight/parser"]}},
        ).load()
        if binary and binary.abspath:
            _mercury_binary_path = str(binary.abspath)
            return _mercury_binary_path
    except Exception:
        pass

    npm_hook = PLUGINS_ROOT / "npm" / "on_Binary__10_npm_install.py"
    crawl_hook = PLUGIN_DIR / "on_Crawl__40_mercury_install.py"
    if not npm_hook.exists():
        return None

    binary_id = str(uuid.uuid4())
    machine_id = str(uuid.uuid4())
    binproviders = "*"
    overrides = None

    if crawl_hook.exists():
        crawl_result = subprocess.run(
            [sys.executable, str(crawl_hook)],
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
            if (
                record.get("type") == "Binary"
                and record.get("name") == "postlight-parser"
            ):
                binproviders = record.get("binproviders", "*")
                overrides = record.get("overrides")
                break

    global _mercury_lib_root
    if not _mercury_lib_root:
        _mercury_lib_root = tempfile.mkdtemp(prefix="mercury-lib-")

    env = os.environ.copy()
    env["HOME"] = str(_mercury_lib_root)
    env["SNAP_DIR"] = str(Path(_mercury_lib_root) / "data")
    env["CRAWL_DIR"] = str(Path(_mercury_lib_root) / "crawl")
    env.pop("LIB_DIR", None)

    cmd = [
        sys.executable,
        str(npm_hook),
        "--binary-id",
        binary_id,
        "--machine-id",
        machine_id,
        "--name",
        "postlight-parser",
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
        if record.get("type") == "Binary" and record.get("name") == "postlight-parser":
            _mercury_binary_path = record.get("abspath")
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
            [
                sys.executable,
                str(MERCURY_HOOK),
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
        result_json = None
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.startswith("{"):
                pass
                try:
                    record = json.loads(line)
                    if record.get("type") == "ArchiveResult":
                        result_json = record
                        break
                except json.JSONDecodeError:
                    pass

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
            [
                sys.executable,
                str(MERCURY_HOOK),
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

        result_json = None
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.startswith("{"):
                try:
                    record = json.loads(line)
                    if record.get("type") == "ArchiveResult":
                        result_json = record
                        break
                except json.JSONDecodeError:
                    pass

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
            [
                sys.executable,
                str(MERCURY_HOOK),
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

        records = [
            json.loads(line)
            for line in result.stdout.strip().split("\n")
            if line.strip().startswith("{")
        ]
        assert records, "Should emit JSONL when disabled"
        assert records[-1]["type"] == "ArchiveResult"
        assert records[-1]["status"] == "skipped"


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
            [
                sys.executable,
                str(MERCURY_HOOK),
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
        result_json = None
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.startswith("{"):
                try:
                    record = json.loads(line)
                    if record.get("type") == "ArchiveResult":
                        result_json = record
                        break
                except json.JSONDecodeError:
                    pass

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
