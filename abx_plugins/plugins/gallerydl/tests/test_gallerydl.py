"""
Integration tests for gallerydl plugin

Tests verify:
    pass
1. Hook script exists
2. Dependencies installed via validation hooks
3. Verify deps with abxpkg
4. Gallery extraction works on gallery URLs
5. JSONL output is correct
6. Config options work
7. Handles non-gallery URLs gracefully
"""

import subprocess
import tempfile
import time
import os
from pathlib import Path
import pytest

from abx_plugins.plugins.base.testing import (
    install_required_binary_from_config,
    parse_jsonl_output,
)

PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = PLUGIN_DIR.parent
_GALLERYDL_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_gallerydl.*"), None)
if _GALLERYDL_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
GALLERYDL_HOOK = _GALLERYDL_HOOK
TEST_URL = "https://example.com"

# Module-level cache for binary path
_gallerydl_binary_path = None


def require_gallerydl_binary() -> str:
    """Return gallery-dl binary path or fail with actionable context."""
    binary_path = get_gallerydl_binary_path()
    assert binary_path, (
        "gallery-dl dependency resolution failed. required_binaries should resolve gallery-dl "
        "automatically in this test environment."
    )
    assert Path(binary_path).is_file(), f"gallery-dl binary path invalid: {binary_path}"
    return binary_path


def get_gallerydl_binary_path() -> str | None:
    """Get gallery-dl binary path, installing via abxpkg if needed."""
    global _gallerydl_binary_path
    if _gallerydl_binary_path and Path(_gallerydl_binary_path).is_file():
        return _gallerydl_binary_path

    binary = install_required_binary_from_config(PLUGIN_DIR, "gallery-dl")
    if binary and binary.abspath:
        _gallerydl_binary_path = str(binary.abspath)
        return _gallerydl_binary_path

    return None


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert GALLERYDL_HOOK.exists(), f"Hook not found: {GALLERYDL_HOOK}"


def test_verify_deps_with_abxpkg():
    """Verify gallery-dl resolves through the real dependency preflight."""
    binary_path = require_gallerydl_binary()
    assert Path(binary_path).is_file(), (
        f"Binary path must be a valid file: {binary_path}"
    )


def test_handles_non_gallery_url():
    """Test that gallery-dl extractor handles non-gallery URLs gracefully via hook."""
    binary_path = require_gallerydl_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        env = os.environ.copy()
        env["GALLERYDL_BINARY"] = binary_path
        env["SNAP_DIR"] = str(tmpdir)

        # Run gallery-dl extraction hook on non-gallery URL
        result = subprocess.run(
            [
                str(GALLERYDL_HOOK),
                "--url",
                "https://example.com",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        # Should exit 0 even for non-gallery URL
        assert result.returncode == 0, (
            f"Should handle non-gallery URL gracefully: {result.stderr}"
        )

        # Parse clean JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "noresults", (
            f"Non-gallery URL should report noresults: {result_json}"
        )
        assert result_json["output_str"] == "No gallery found", result_json


def test_config_save_gallery_dl_false_skips():
    """Test that GALLERYDL_ENABLED=False exits without emitting JSONL."""
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["GALLERYDL_ENABLED"] = "False"

        result = subprocess.run(
            [
                str(GALLERYDL_HOOK),
                "--url",
                TEST_URL,
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

        # Feature disabled should emit skipped JSONL
        assert "Skipping" in result.stderr or "False" in result.stderr, (
            "Should log skip reason to stderr"
        )

        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Expected skipped JSONL output"
        assert result_json["status"] == "skipped", result_json
        assert result_json["output_str"] == "GALLERYDL_ENABLED=False", result_json


def test_config_timeout():
    """Test that GALLERY_DL_TIMEOUT config is respected."""
    import os

    binary_path = require_gallerydl_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["GALLERY_DL_TIMEOUT"] = "5"
        env["GALLERYDL_BINARY"] = binary_path
        env["SNAP_DIR"] = str(tmpdir)

        start_time = time.time()
        result = subprocess.run(
            [
                str(GALLERYDL_HOOK),
                "--url",
                "https://example.com",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,  # Should complete in 5s, use 10s as safety margin
        )
        elapsed_time = time.time() - start_time

        assert result.returncode == 0, (
            f"Should complete without hanging: {result.stderr}"
        )
        # Allow 1 second overhead for subprocess startup and Python interpreter
        assert elapsed_time <= 6.0, (
            f"Should complete within 6 seconds (5s timeout + 1s overhead), took {elapsed_time:.2f}s"
        )


def test_real_gallery_url(httpserver):
    """Test that gallery-dl can extract an image served from the Wikimedia upload path."""
    binary_path = require_gallerydl_binary()

    image_path = "/wikipedia/commons/a/a9/Example.jpg"
    image_bytes = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00"
        b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07"
        b"\x09\x09\x08\x0a\x0c\x14\x0d\x0c\x0b\x0b\x0c\x19\x12\x13"
        b"\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c"
        b"\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00"
        b"\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x14\x00\x01\x00"
        b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\x08\xff\xc4\x00\x14\x10\x01\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\x2a\xcf\xff\xd9"
    )
    httpserver.expect_request(image_path).respond_with_data(
        image_bytes,
        content_type="image/jpeg",
    )
    gallery_url = httpserver.url_for(image_path)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        env = os.environ.copy()
        env["GALLERYDL_TIMEOUT"] = "60"
        env["GALLERYDL_BINARY"] = binary_path
        env["SNAP_DIR"] = str(tmpdir)

        start_time = time.time()
        result = subprocess.run(
            [str(GALLERYDL_HOOK), "--url", gallery_url],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
        )
        elapsed_time = time.time() - start_time

        assert result.returncode == 0, result.stderr
        result_json = parse_jsonl_output(result.stdout)
        assert result_json and result_json.get("status") == "succeeded", (
            result.stdout,
            result.stderr,
        )

        output_str = (result_json.get("output_str") or "").strip()
        assert output_str, (result.stdout, result.stderr)
        assert output_str.startswith(f"{PLUGIN_DIR.name}/"), output_str

        output_path = tmpdir / output_str
        assert output_path.is_file(), output_path
        assert output_path.suffix.lower() in (
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".bmp",
        ), output_path
        assert output_path.stat().st_size > 0, output_path

        image_files = [
            path
            for path in tmpdir.rglob("*")
            if path.is_file()
            and path.suffix.lower()
            in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
        ]
        assert image_files, tmpdir
        print(
            f"Successfully extracted {len(image_files)} image(s) in {elapsed_time:.2f}s",
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
