"""
Integration tests for mercury plugin

Tests verify:
1. Hook script exists
2. Dependencies installed via validation hooks
3. Verify deps with abx-pkg
4. Mercury extraction works on https://example.com
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
        if (
            record.get("type") == "Binary"
            and record.get("name") == "postlight-parser"
        ):
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


def test_extracts_with_mercury_parser():
    """Test full workflow: extract with postlight-parser from real HTML via hook."""
    binary_path = require_mercury_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir
        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["MERCURY_BINARY"] = binary_path

        # Create HTML source that mercury can parse
        (snap_dir / "singlefile").mkdir()
        (snap_dir / "singlefile" / "singlefile.html").write_text(
            "<html><head><title>Test Article</title></head><body>"
            "<article><h1>Example Article</h1><p>This is test content for mercury parser.</p></article>"
            "</body></html>"
        )

        # Run mercury extraction hook
        result = subprocess.run(
            [
                sys.executable,
                str(MERCURY_HOOK),
                "--url",
                TEST_URL,
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


def test_config_save_mercury_false_skips():
    """Test that MERCURY_ENABLED=False exits without emitting JSONL."""
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

        # Feature disabled - temporary failure, should NOT emit JSONL
        assert "Skipping" in result.stderr or "False" in result.stderr, (
            "Should log skip reason to stderr"
        )

        # Should NOT emit any JSONL
        jsonl_lines = [
            line
            for line in result.stdout.strip().split("\n")
            if line.strip().startswith("{")
        ]
        assert len(jsonl_lines) == 0, (
            f"Should not emit JSONL when feature disabled, but got: {jsonl_lines}"
        )


def test_fails_gracefully_without_html():
    """Test that mercury works even without HTML source (fetches URL directly)."""
    binary_path = require_mercury_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["MERCURY_BINARY"] = binary_path
        env["SNAP_DIR"] = str(tmpdir)
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

        # Mercury fetches URL directly with postlight-parser, doesn't need HTML source
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

        # Mercury should succeed or fail based on network, not based on HTML source
        assert result_json, "Should emit ArchiveResult"
        assert result_json["status"] in ["succeeded", "failed"], (
            f"Should succeed or fail: {result_json}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
