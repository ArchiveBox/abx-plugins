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
import uuid
from pathlib import Path
import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
from base.test_utils import parse_jsonl_output

PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = PLUGIN_DIR.parent
_GALLERYDL_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_gallerydl.*"), None)
if _GALLERYDL_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
GALLERYDL_HOOK = _GALLERYDL_HOOK
TEST_URL = "https://example.com"

# Module-level cache for binary path
_gallerydl_binary_path = None
_gallerydl_lib_root = None


def require_gallerydl_binary() -> str:
    """Return gallery-dl binary path or fail with actionable context."""
    binary_path = get_gallerydl_binary_path()
    assert binary_path, (
        "gallery-dl installation failed. Install hook should install gallery-dl "
        "automatically in this test environment."
    )
    assert Path(binary_path).is_file(), f"gallery-dl binary path invalid: {binary_path}"
    return binary_path


def get_gallerydl_binary_path():
    """Get gallery-dl binary path from cache or by running install hooks."""
    global _gallerydl_binary_path
    if _gallerydl_binary_path and Path(_gallerydl_binary_path).is_file():
        return _gallerydl_binary_path

    # Try loading from existing providers first
    from abx_pkg import Binary, PipProvider, EnvProvider

    try:
        binary = Binary(
            name="gallery-dl", binproviders=[PipProvider(), EnvProvider()]
        ).load()
        if binary and binary.abspath:
            _gallerydl_binary_path = str(binary.abspath)
            return _gallerydl_binary_path
    except Exception:
        pass

    # Install via real plugin hooks
    pip_hook = PLUGINS_ROOT / "pip" / "on_Binary__11_pip_install.py"
    crawl_hook = next(PLUGIN_DIR.glob("on_Crawl__20_gallerydl_install*.py"), None)
    if not pip_hook.exists():
        return None

    binary_id = str(uuid.uuid4())
    machine_id = str(uuid.uuid4())
    overrides = None

    if crawl_hook and crawl_hook.exists():
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
            if record.get("type") == "Binary" and record.get("name") == "gallery-dl":
                overrides = record.get("overrides")
                break

    global _gallerydl_lib_root
    if not _gallerydl_lib_root:
        _gallerydl_lib_root = tempfile.mkdtemp(prefix="gallerydl-lib-")

    env = os.environ.copy()
    env["HOME"] = str(_gallerydl_lib_root)
    env["SNAP_DIR"] = str(Path(_gallerydl_lib_root) / "data")
    env.pop("LIB_DIR", None)

    cmd = [
        sys.executable,
        str(pip_hook),
        "--binary-id",
        binary_id,
        "--machine-id",
        machine_id,
        "--name",
        "gallery-dl",
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
        if record.get("type") == "Binary" and record.get("name") == "gallery-dl":
            _gallerydl_binary_path = record.get("abspath")
            return _gallerydl_binary_path

    return None


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert GALLERYDL_HOOK.exists(), f"Hook not found: {GALLERYDL_HOOK}"


def test_verify_deps_with_abx_pkg():
    """Verify gallery-dl is installed by real plugin install hooks."""
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
    """Test that gallery-dl can extract an image from a live public Wikimedia page."""
    binary_path = require_gallerydl_binary()

    # Reddit aggressively rate-limits GitHub runners. Wikimedia Commons is public, stable,
    # and gallery-dl can resolve the canonical image filename without site credentials.
    gallery_url = "https://commons.wikimedia.org/wiki/File:Example.jpg"

    max_attempts = 3
    last_error = ""

    for attempt in range(1, max_attempts + 1):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            env = os.environ.copy()
            env["GALLERYDL_TIMEOUT"] = "60"
            env["GALLERYDL_BINARY"] = binary_path
            env["SNAP_DIR"] = str(tmpdir)

            start_time = time.time()
            result = subprocess.run(
                [
                    sys.executable,
                    str(GALLERYDL_HOOK),
                    "--url",
                    gallery_url,
                    "--snapshot-id",
                    f"testreddit{attempt}",
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

            result_json = parse_jsonl_output(result.stdout)

            if not result_json or result_json.get("status") != "succeeded":
                last_error = f"attempt={attempt} invalid ArchiveResult stdout={result.stdout} stderr={result.stderr}"
                continue

            output_str = (result_json.get("output_str") or "").strip()
            if not output_str:
                last_error = f"attempt={attempt} empty output_str stdout={result.stdout} stderr={result.stderr}"
                continue

            output_path = tmpdir / PLUGIN_DIR.name / output_str
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
        f"Live gallery download did not yield an image after {max_attempts} attempts. Last error: {last_error}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
