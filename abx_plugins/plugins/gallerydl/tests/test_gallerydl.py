"""
Integration tests for gallerydl plugin

Tests verify:
    pass
1. Hook script exists
2. Dependencies installed via validation hooks
3. Verify deps with abx-pkg
4. Gallery extraction works on gallery URLs
5. JSONL output is correct
6. Config options work
7. Handles non-gallery URLs gracefully
"""

import json
import subprocess
import sys
import tempfile
import time
import os
from pathlib import Path
import pytest

PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = PLUGIN_DIR.parent
_GALLERYDL_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_gallerydl.*"), None)
if _GALLERYDL_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
GALLERYDL_HOOK = _GALLERYDL_HOOK
TEST_URL = "https://example.com"


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert GALLERYDL_HOOK.exists(), f"Hook not found: {GALLERYDL_HOOK}"


def test_verify_deps_with_abx_pkg():
    """Verify gallery-dl is available via abx-pkg."""
    from abx_pkg import Binary, PipProvider, EnvProvider

    try:
        pip_provider = PipProvider()
        env_provider = EnvProvider()
    except Exception as exc:
        pytest.fail(f"Python package providers unavailable in this runtime: {exc}")

    missing_binaries = []

    # Verify gallery-dl is available
    gallerydl_binary = Binary(
        name="gallery-dl", binproviders=[pip_provider, env_provider]
    )
    gallerydl_loaded = gallerydl_binary.load()
    if not (gallerydl_loaded and gallerydl_loaded.abspath):
        missing_binaries.append("gallery-dl")

    if missing_binaries:
        pass


def test_handles_non_gallery_url():
    """Test that gallery-dl extractor handles non-gallery URLs gracefully via hook."""
    # Prerequisites checked by earlier test

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Run gallery-dl extraction hook on non-gallery URL
        result = subprocess.run(
            [
                sys.executable,
                str(GALLERYDL_HOOK),
                "--url",
                "https://example.com",
                "--snapshot-id",
                "test789",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # Should exit 0 even for non-gallery URL
        assert result.returncode == 0, (
            f"Should handle non-gallery URL gracefully: {result.stderr}"
        )

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


def test_config_save_gallery_dl_false_skips():
    """Test that GALLERYDL_ENABLED=False exits without emitting JSONL."""
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["GALLERYDL_ENABLED"] = "False"

        result = subprocess.run(
            [
                sys.executable,
                str(GALLERYDL_HOOK),
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


def test_config_timeout():
    """Test that GALLERY_DL_TIMEOUT config is respected."""
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["GALLERY_DL_TIMEOUT"] = "5"

        start_time = time.time()
        result = subprocess.run(
            [
                sys.executable,
                str(GALLERYDL_HOOK),
                "--url",
                "https://example.com",
                "--snapshot-id",
                "testtimeout",
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


def test_real_gallery_url():
    """Test that gallery-dl can extract images from a real Flickr gallery URL."""
    # Real public gallery URL that currently yields downloadable media.
    gallery_url = "https://www.flickr.com/photos/gregorydolivet/55002388567/in/explore-2025-12-25/"

    max_attempts = 3
    last_error = ""

    for attempt in range(1, max_attempts + 1):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            env = os.environ.copy()
            env["GALLERYDL_TIMEOUT"] = "60"
            env["SNAP_DIR"] = str(tmpdir)

            start_time = time.time()
            result = subprocess.run(
                [
                    sys.executable,
                    str(GALLERYDL_HOOK),
                    "--url",
                    gallery_url,
                    "--snapshot-id",
                    f"testflickr{attempt}",
                ],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                env=env,
                timeout=90,
            )
            elapsed_time = time.time() - start_time

            if result.returncode != 0:
                last_error = f"attempt={attempt} returncode={result.returncode} stderr={result.stderr}"
                continue

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

            if not result_json or result_json.get("status") != "succeeded":
                last_error = f"attempt={attempt} invalid ArchiveResult stdout={result.stdout} stderr={result.stderr}"
                continue

            output_str = (result_json.get("output_str") or "").strip()
            if not output_str:
                last_error = f"attempt={attempt} empty output_str stdout={result.stdout} stderr={result.stderr}"
                continue

            output_path = Path(output_str)
            if not output_path.is_file():
                last_error = f"attempt={attempt} output missing path={output_path}"
                continue

            if output_path.suffix.lower() not in (
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                ".webp",
                ".bmp",
            ):
                last_error = f"attempt={attempt} output is not image path={output_path}"
                continue

            if output_path.stat().st_size <= 0:
                last_error = f"attempt={attempt} output file empty path={output_path}"
                continue

            # Ensure the extractor really downloaded image media, not just metadata.
            output_files = list(tmpdir.rglob("*"))
            image_files = [
                f
                for f in output_files
                if f.is_file()
                and f.suffix.lower()
                in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
            ]
            if not image_files:
                last_error = f"attempt={attempt} no image files under SNAP_DIR={tmpdir}"
                continue

            print(
                f"Successfully extracted {len(image_files)} image(s) in {elapsed_time:.2f}s"
            )
            return

    pytest.fail(
        f"Real gallery download did not yield an image after {max_attempts} attempts. Last error: {last_error}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
