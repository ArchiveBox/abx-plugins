"""
Integration tests for ytdlp plugin

Tests verify:
1. Hook script exists
2. Verify deps with abxpkg
3. YT-DLP extraction works on video URLs
4. JSONL output is correct
5. Config options work (YTDLP_ENABLED, YTDLP_TIMEOUT)
6. Handles non-video URLs gracefully
"""

import io
import os
import subprocess
import tempfile
import time
import wave
from pathlib import Path
import pytest

from abx_plugins.plugins.base.testing import (
    install_required_binary_from_config,
    parse_jsonl_output,
)

PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = PLUGIN_DIR.parent
_YTDLP_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_ytdlp.*"), None)
if _YTDLP_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
YTDLP_HOOK = _YTDLP_HOOK
TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


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


@pytest.fixture(scope="session")
def installed_ytdlp_runtime_env(tmp_path_factory) -> dict[str, str]:
    """Install every declared yt-dlp dependency once through abxpkg."""
    lib_dir = Path(
        os.environ.get(
            "ABXPKG_LIB_DIR",
            str(tmp_path_factory.mktemp("ytdlp_test_lib")),
        ),
    )
    expected_bin_dir = lib_dir / "env" / "bin"
    install_env = os.environ.copy()
    install_env["ABXPKG_LIB_DIR"] = str(lib_dir)

    resolved = {
        "ABXPKG_LIB_DIR": str(lib_dir),
        "PATH": os.pathsep.join(
            (str(expected_bin_dir), install_env.get("PATH", "")),
        ).rstrip(os.pathsep),
    }
    for config_name, env_name in (
        ("yt-dlp", "YTDLP_BINARY"),
        ("node", "NODE_BINARY"),
        ("ffmpeg", "FFMPEG_BINARY"),
    ):
        binary = install_required_binary_from_config(
            PLUGIN_DIR,
            config_name,
            env=install_env,
        )
        assert binary and binary.loaded_abspath, (
            f"{config_name} dependency resolution failed via abxpkg"
        )
        binary_path = Path(binary.loaded_abspath)
        assert binary_path.is_file(), (
            f"{config_name} binary path invalid: {binary_path}"
        )
        assert binary.loaded_binprovider is not None, (
            f"{config_name} must identify the abxpkg provider that resolved it"
        )
        if binary.loaded_binprovider.name == "env":
            assert binary_path.parent == expected_bin_dir, (
                f"host {config_name} must be projected through {expected_bin_dir}: "
                f"{binary_path}"
            )
        resolved[env_name] = str(binary_path)

    return resolved


@pytest.fixture
def ytdlp_runtime_env(installed_ytdlp_runtime_env) -> dict[str, str]:
    """Return the resolved runtime for process-scoped hook environments."""
    return installed_ytdlp_runtime_env.copy()


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert YTDLP_HOOK.exists(), f"Hook not found: {YTDLP_HOOK}"


def test_card_template_loads_browser_media_on_click():
    """Card template should not fetch archived media until the user asks to play it."""
    template = (PLUGIN_DIR / "templates" / "card.html").read_text()

    assert "ytdlp-load-player" in template
    assert 'data-src="{{ file.url|default:file.path|urlencode }}"' in template
    assert "media.src = src" in template
    assert "<video" not in template
    assert "<audio" not in template


def test_card_template_links_non_browser_media_without_player():
    """Non-browser-playable yt-dlp outputs should stay as regular file links."""
    template = (PLUGIN_DIR / "templates" / "card.html").read_text()

    assert "{% if file.is_browser_playable %}" in template
    assert "{% else %}" in template
    assert "Download file" in template
    assert 'href="{{ file.url|default:file.path|urlencode }}"' in template


def test_verify_deps_with_abxpkg(ytdlp_runtime_env):
    """Verify the complete yt-dlp runtime resolves through the real preflight."""
    assert {
        "YTDLP_BINARY",
        "NODE_BINARY",
        "FFMPEG_BINARY",
    } < ytdlp_runtime_env.keys()


def test_handles_non_video_url(non_video_test_url, ytdlp_runtime_env):
    """Test that ytdlp extractor handles non-video URLs gracefully via hook."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        env = os.environ.copy()
        env.update(ytdlp_runtime_env)
        env["SNAP_DIR"] = str(tmpdir)

        # Run ytdlp extraction hook on non-video URL
        result = subprocess.run(
            [
                str(YTDLP_HOOK),
                "--url",
                non_video_test_url,
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


def test_config_ytdlp_enabled_false_skips(ytdlp_runtime_env):
    """Test that YTDLP_ENABLED=False exits without emitting JSONL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env.update(ytdlp_runtime_env)
        env["YTDLP_ENABLED"] = "False"

        result = subprocess.run(
            [
                str(YTDLP_HOOK),
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
        assert result_json["output_str"] == "YTDLP_ENABLED=False", result_json


def test_config_timeout(non_video_test_url, ytdlp_runtime_env):
    """Test that YTDLP_TIMEOUT config is respected (also via MEDIA_TIMEOUT alias)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env.update(ytdlp_runtime_env)
        env["MEDIA_TIMEOUT"] = "30"
        env["SNAP_DIR"] = str(tmpdir)

        start_time = time.time()
        result = subprocess.run(
            [
                str(YTDLP_HOOK),
                "--url",
                non_video_test_url,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=35,
        )
        elapsed_time = time.time() - start_time

        assert result.returncode == 0, (
            f"Should complete without hanging: {result.stderr}"
        )
        assert elapsed_time <= 10.0, (
            f"Non-media URL should still fail quickly even with a valid configured timeout, took {elapsed_time:.2f}s"
        )
        result_json = parse_jsonl_output(result.stdout)
        assert result_json == {
            "type": "ArchiveResult",
            "status": "noresults",
            "output_str": "No media found",
        }, result_json


def test_extracts_local_media_url(media_test_url, ytdlp_runtime_env):
    """Test yt-dlp extraction against deterministic local media served by httpserver."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env.update(ytdlp_runtime_env)
        env["YTDLP_TIMEOUT"] = "60"
        env["SNAP_DIR"] = str(tmpdir)

        start_time = time.time()
        result = subprocess.run(
            [
                str(YTDLP_HOOK),
                "--url",
                media_test_url,
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
        assert result_json["type"] == "ArchiveResult", result_json
        assert result_json["status"] == "succeeded", result_json

        # Check that some video/audio files were downloaded
        output_files = sorted(tmpdir.glob("**/*"))
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
        output_path = tmpdir / result_json["output_str"]
        assert output_path.is_file(), f"ArchiveResult output missing: {output_path}"
        assert output_path in media_files, (
            f"ArchiveResult should point at a downloaded media artifact: {result_json}"
        )
        assert output_path.stat().st_size > 0, (
            f"Downloaded media is empty: {output_path}"
        )

        print(
            f"Successfully extracted {len(media_files)} file(s) in {elapsed_time:.2f}s",
        )


def test_uses_real_ffmpeg_binary_from_env_when_not_on_path(
    media_test_url,
    ytdlp_runtime_env,
):
    """Hook should use FFMPEG_BINARY explicitly instead of relying on PATH probing."""
    ffmpeg_binary = ytdlp_runtime_env["FFMPEG_BINARY"]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        env = os.environ.copy()
        env.update(ytdlp_runtime_env)
        env["SNAP_DIR"] = str(tmpdir_path)
        env["YTDLP_ARGS_EXTRA"] = '["--downloader","ffmpeg"]'
        uv_dir = next(
            (
                path
                for path in env.get("PATH", "").split(os.pathsep)
                if path and (Path(path) / "uv").exists()
            ),
            "",
        )
        ffmpeg_dirs = {
            str(Path(ffmpeg_binary).parent.resolve()),
            str(Path(ffmpeg_binary).resolve().parent),
        }
        filtered_paths = [
            path
            for path in env.get("PATH", "").split(os.pathsep)
            if path and str(Path(path).resolve()) not in ffmpeg_dirs
        ]
        if uv_dir and uv_dir not in filtered_paths:
            filtered_paths.insert(0, uv_dir)
        env["PATH"] = os.pathsep.join(filtered_paths)

        result = subprocess.run(
            [
                str(YTDLP_HOOK),
                "--url",
                media_test_url,
            ],
            cwd=tmpdir_path,
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
        )

        assert result.returncode == 0, result.stderr
        result_json = parse_jsonl_output(result.stdout)
        assert result_json, (
            f"Should have ArchiveResult JSONL output. stdout: {result.stdout}"
        )
        assert result_json["type"] == "ArchiveResult", result_json
        assert result_json["status"] == "succeeded", result_json
        output_path = tmpdir_path / result_json["output_str"]
        assert output_path.is_file(), f"ArchiveResult output missing: {output_path}"
        assert output_path.stat().st_size > 0, (
            f"Downloaded media is empty: {output_path}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
