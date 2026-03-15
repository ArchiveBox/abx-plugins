"""
Integration tests for ytdlp plugin

Tests verify:
1. Hook script exists
2. Verify deps with abx-pkg
3. YT-DLP extraction works on video URLs
4. JSONL output is correct
5. Config options work (YTDLP_ENABLED, YTDLP_TIMEOUT)
6. Handles non-video URLs gracefully
"""

import json
import io
import os
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from pathlib import Path
import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
from base.test_utils import parse_jsonl_output, parse_jsonl_records

PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = PLUGIN_DIR.parent
_YTDLP_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_ytdlp.*"), None)
if _YTDLP_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
YTDLP_HOOK = _YTDLP_HOOK
TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

# Module-level cache for binary path
_ytdlp_binary_path = None
_ytdlp_lib_root = None


def _has_ssl_cert_error(result: subprocess.CompletedProcess[str]) -> bool:
    combined = f"{result.stdout}\n{result.stderr}"
    return "CERTIFICATE_VERIFY_FAILED" in combined


def _build_test_wav_bytes() -> bytes:
    """Build a short deterministic WAV payload for local-media extractor tests."""
    sample_rate = 8000
    duration_seconds = 1
    num_frames = sample_rate * duration_seconds

    wav_io = io.BytesIO()
    with wave.open(wav_io, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * num_frames)

    return wav_io.getvalue()


@pytest.fixture
def non_video_test_url(httpserver):
    """Serve deterministic non-media content for failure-path ytdlp tests."""
    httpserver.expect_request("/").respond_with_data(
        """
        <!doctype html>
        <html>
          <head><title>Not a media URL</title></head>
          <body><h1>No downloadable media here</h1></body>
        </html>
        """.strip(),
        content_type="text/html; charset=utf-8",
    )
    return httpserver.url_for("/")


@pytest.fixture
def media_test_url(httpserver):
    """Serve deterministic media bytes for end-to-end ytdlp extraction tests."""
    httpserver.expect_request("/sample.wav").respond_with_data(
        _build_test_wav_bytes(),
        content_type="audio/wav",
    )
    return httpserver.url_for("/sample.wav")


def require_ytdlp_binary() -> str:
    """Return yt-dlp binary path or fail with actionable context."""
    binary_path = get_ytdlp_binary_path()
    assert binary_path, (
        "yt-dlp installation failed. Install hook should install yt-dlp "
        "automatically in this test environment."
    )
    assert Path(binary_path).is_file(), f"yt-dlp binary path invalid: {binary_path}"
    return binary_path


def get_ytdlp_binary_path():
    """Get yt-dlp path from cache or by running install hooks."""
    global _ytdlp_binary_path
    if _ytdlp_binary_path and Path(_ytdlp_binary_path).is_file():
        return _ytdlp_binary_path

    from abx_pkg import Binary, PipProvider, EnvProvider

    try:
        binary = Binary(
            name="yt-dlp",
            binproviders=[PipProvider(), EnvProvider()],
            overrides={"pip": {"packages": ["yt-dlp[default]"]}},
        ).load()
        if binary and binary.abspath:
            _ytdlp_binary_path = str(binary.abspath)
            return _ytdlp_binary_path
    except Exception:
        pass

    pip_hook = PLUGINS_ROOT / "pip" / "on_Binary__11_pip_install.py"
    crawl_hook = next(PLUGIN_DIR.glob("on_Crawl__15_ytdlp_install*.py"), None)
    if not pip_hook.exists():
        return None

    binary_id = str(uuid.uuid4())
    machine_id = str(uuid.uuid4())
    binproviders = "*"
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
            if record.get("type") == "Binary" and record.get("name") == "yt-dlp":
                binproviders = record.get("binproviders", "*")
                overrides = record.get("overrides")
                break

    global _ytdlp_lib_root
    if not _ytdlp_lib_root:
        _ytdlp_lib_root = tempfile.mkdtemp(prefix="ytdlp-lib-")

    env = os.environ.copy()
    env["HOME"] = str(_ytdlp_lib_root)
    env["SNAP_DIR"] = str(Path(_ytdlp_lib_root) / "data")
    env["CRAWL_DIR"] = str(Path(_ytdlp_lib_root) / "crawl")
    env.pop("LIB_DIR", None)

    cmd = [
        sys.executable,
        str(pip_hook),
        "--binary-id",
        binary_id,
        "--machine-id",
        machine_id,
        "--name",
        "yt-dlp",
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
        if record.get("type") == "Binary" and record.get("name") == "yt-dlp":
            _ytdlp_binary_path = record.get("abspath")
            return _ytdlp_binary_path

    return None


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert YTDLP_HOOK.exists(), f"Hook not found: {YTDLP_HOOK}"


def test_verify_deps_with_abx_pkg():
    """Verify yt-dlp is installed by real plugin install hooks."""
    binary_path = require_ytdlp_binary()
    assert Path(binary_path).is_file(), (
        f"Binary path must be a valid file: {binary_path}"
    )


def test_handles_non_video_url(non_video_test_url):
    """Test that ytdlp extractor handles non-video URLs gracefully via hook."""
    binary_path = require_ytdlp_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        env = os.environ.copy()
        env["YTDLP_BINARY"] = binary_path
        env["SNAP_DIR"] = str(tmpdir)

        # Run ytdlp extraction hook on non-video URL
        result = subprocess.run(
            [
                sys.executable,
                str(YTDLP_HOOK),
                "--url",
                non_video_test_url,
                "--snapshot-id",
                "test789",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        # Should exit 0 even for non-media URL
        assert result.returncode == 0, (
            f"Should handle non-media URL gracefully: {result.stderr}"
        )

        # Parse clean JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "noresults", (
            f"Non-media URL should report noresults: {result_json}"
        )
        assert result_json["output_str"] == "No media found", result_json


def test_config_ytdlp_enabled_false_skips():
    """Test that YTDLP_ENABLED=False exits without emitting JSONL."""
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["YTDLP_ENABLED"] = "False"

        result = subprocess.run(
            [
                sys.executable,
                str(YTDLP_HOOK),
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

        records = parse_jsonl_records(result.stdout)
        assert len(records) == 1, f"Expected exactly one JSONL record, got: {records}"
        result_json = records[0]
        assert result_json["status"] == "skipped", result_json
        assert result_json["output_str"] == "YTDLP_ENABLED=False", result_json


def test_config_timeout(non_video_test_url):
    """Test that YTDLP_TIMEOUT config is respected (also via MEDIA_TIMEOUT alias)."""
    binary_path = require_ytdlp_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["YTDLP_TIMEOUT"] = "5"
        env["YTDLP_BINARY"] = binary_path
        env["SNAP_DIR"] = str(tmpdir)

        start_time = time.time()
        result = subprocess.run(
            [
                sys.executable,
                str(YTDLP_HOOK),
                "--url",
                non_video_test_url,
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


def test_extracts_local_media_url(media_test_url):
    """Test yt-dlp extraction against deterministic local media served by httpserver."""
    binary_path = require_ytdlp_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env["YTDLP_TIMEOUT"] = "60"
        env["YTDLP_BINARY"] = binary_path
        env["SNAP_DIR"] = str(tmpdir)

        start_time = time.time()
        result = subprocess.run(
            [
                sys.executable,
                str(YTDLP_HOOK),
                "--url",
                media_test_url,
                "--snapshot-id",
                "testlocalmedia",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
        )
        elapsed_time = time.time() - start_time

        assert result.returncode == 0, (
            f"Should extract local media successfully: {result.stderr}"
        )

        # Parse JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, (
            f"Should have ArchiveResult JSONL output. stdout: {result.stdout}"
        )
        assert result_json["status"] == "succeeded", f"Should succeed: {result_json}"

        # Check that some video/audio files were downloaded
        output_files = list(tmpdir.glob("**/*"))
        media_files = [
            f
            for f in output_files
            if f.is_file()
            and f.suffix.lower()
            in (
                ".mp4",
                ".webm",
                ".mkv",
                ".m4a",
                ".mp3",
                ".wav",
                ".json",
                ".jpg",
                ".webp",
            )
        ]

        assert len(media_files) > 0, (
            f"Should have downloaded at least one video/audio file. Files: {output_files}"
        )

        print(
            f"Successfully extracted {len(media_files)} file(s) in {elapsed_time:.2f}s"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
