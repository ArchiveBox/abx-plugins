#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic-settings",
#     "jambo",
#     "rich-click",
#     "abx-plugins",
# ]
# ///
"""
Download video/audio from a URL using yt-dlp.

Usage: on_Snapshot__02_ytdlp.finite.bg.py --url=<url>
Output: Downloads video/audio files to SNAP_DIR/ytdlp/

Environment variables:
    YTDLP_ENABLED: Enable yt-dlp extraction (default: True)
    YTDLP_BINARY: Path to yt-dlp binary (default: yt-dlp)
    NODE_BINARY: Path to Node.js binary
    FFMPEG_BINARY: Path to ffmpeg binary
    YTDLP_TIMEOUT: Timeout in seconds (x-fallback: TIMEOUT)
    YTDLP_COOKIES_FILE: Path to cookies file (x-fallback: COOKIES_FILE)
    YTDLP_MAX_SIZE: Maximum file size (default: 750m)
    YTDLP_CHECK_SSL_VALIDITY: Whether to verify SSL certs (x-fallback: CHECK_SSL_VALIDITY)
    YTDLP_ARGS: Default yt-dlp arguments (JSON array)
    YTDLP_ARGS_EXTRA: Extra arguments to append (JSON array)
"""

import os
import subprocess
import sys
import threading
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    has_staticfile_output,
    load_config,
)

import rich_click as click


PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
EXECUTOR_ARTIFACT_SUFFIXES = (
    ".stdout.log",
    ".stderr.log",
    ".pid",
    ".sh",
    ".meta.json",
)


def rel_output(path_str: str | None) -> str | None:
    if not path_str:
        return path_str
    path = Path(path_str)
    resolved = path.resolve()
    if not resolved.exists():
        return path_str
    try:
        return str(resolved.relative_to(SNAP_DIR.resolve()))
    except Exception:
        return path.name or path_str


def save_ytdlp(url: str, binary: str) -> tuple[bool, str | None, str]:
    """
    Download video/audio using yt-dlp.

    Returns: (success, output_path, error_message)
    """
    # Load config from config.json (auto-resolves x-aliases and x-fallback from env)
    config = load_config()
    timeout = config.YTDLP_TIMEOUT
    check_ssl = config.YTDLP_CHECK_SSL_VALIDITY
    cookies_file = config.YTDLP_COOKIES_FILE
    max_size = config.YTDLP_MAX_SIZE
    node_binary = config.NODE_BINARY
    ffmpeg_binary = (os.environ.get("FFMPEG_BINARY") or "").strip()
    ytdlp_args = config.YTDLP_ARGS
    ytdlp_args_extra = config.YTDLP_ARGS_EXTRA

    # Output directory is current directory (hook already runs in output dir)
    output_dir = Path(".")
    process_env = os.environ.copy()

    # Build command (later options take precedence)
    cmd = [
        binary,
        *ytdlp_args,
        # Format with max_size limit (appended after YTDLP_ARGS so it can be overridden by YTDLP_ARGS_EXTRA)
        f"--format=(bv*+ba/b)[filesize<={max_size}][filesize_approx<=?{max_size}]/(bv*+ba/b)",
        f"--js-runtimes=node:{node_binary}",
    ]

    ffmpeg_path = Path(ffmpeg_binary).expanduser()
    if ffmpeg_binary and ffmpeg_path.is_file():
        ffmpeg_dir = str(ffmpeg_path.parent.resolve())
        existing_path = process_env["PATH"] if "PATH" in process_env else ""
        process_env["PATH"] = os.pathsep.join(
            [ffmpeg_dir, *([existing_path] if existing_path else [])],
        )

    if not check_ssl:
        cmd.append("--no-check-certificate")

    if cookies_file and Path(cookies_file).is_file():
        cmd.extend(["--cookies", cookies_file])

    if ytdlp_args_extra:
        cmd.extend(ytdlp_args_extra)

    if "--newline" not in cmd:
        cmd.append("--newline")

    cmd.append(url)

    try:
        print(f"[ytdlp] Starting download (timeout={timeout}s)", file=sys.stderr)

        output_lines: list[str] = []
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=process_env,
        )

        def _read_output() -> None:
            if not process.stdout:
                return
            for line in process.stdout:
                output_lines.append(line)
                sys.stderr.write(line)

        reader = threading.Thread(target=_read_output, daemon=True)
        reader.start()

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            reader.join(timeout=1)
            return False, None, f"Timed out after {timeout} seconds"

        reader.join(timeout=1)
        combined_output = "".join(output_lines)

        # Check if any media files were downloaded
        media_extensions = (
            ".mp4",
            ".webm",
            ".mkv",
            ".avi",
            ".mov",
            ".flv",
            ".wmv",
            ".m4v",
            ".mp3",
            ".m4a",
            ".ogg",
            ".wav",
            ".flac",
            ".aac",
            ".opus",
            ".json",
            ".jpg",
            ".png",
            ".webp",
            ".jpeg",
            ".vtt",
            ".srt",
            ".ass",
            ".lrc",
            ".description",
        )

        downloaded_files = [
            f
            for f in output_dir.glob("*")
            if f.is_file()
            and f.suffix.lower() in media_extensions
            and not any(
                f.name.endswith(suffix) for suffix in EXECUTOR_ARTIFACT_SUFFIXES
            )
        ]

        if downloaded_files:
            # Return first video/audio file, or first file if no media
            video_audio = [
                f
                for f in downloaded_files
                if f.suffix.lower()
                in (
                    ".mp4",
                    ".webm",
                    ".mkv",
                    ".avi",
                    ".mov",
                    ".mp3",
                    ".m4a",
                    ".ogg",
                    ".wav",
                    ".flac",
                )
            ]
            output = str(video_audio[0]) if video_audio else str(downloaded_files[0])
            return True, output, ""
        else:
            stderr = combined_output

            # These are NOT errors - page simply has no downloadable media
            # Return success with no output (legitimate "nothing to download")
            if "ERROR: Unsupported URL" in stderr:
                return True, "No media found", ""
            if "URL could be a direct video link" in stderr:
                return True, "No media found", ""
            if process.returncode == 0:
                return True, "No media found", ""

            # These ARE errors - something went wrong
            if "HTTP Error 404" in stderr:
                return False, None, "404 Not Found"
            if "HTTP Error 403" in stderr:
                return False, None, "403 Forbidden"
            if "Unable to extract" in stderr:
                return False, None, "Unable to extract media info"

            return False, None, f"yt-dlp error: {stderr}"

    except subprocess.TimeoutExpired:
        return False, None, f"Timed out after {timeout} seconds"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="URL to download video/audio from")
def main(url: str):
    """Download video/audio from a URL using yt-dlp."""

    try:
        config = load_config()

        # Check if yt-dlp downloading is enabled
        if not config.YTDLP_ENABLED:
            print("Skipping ytdlp (YTDLP_ENABLED=False)", file=sys.stderr)
            emit_archive_result_record("skipped", "YTDLP_ENABLED=False")
            sys.exit(0)

        # Check if staticfile extractor already handled this (permanent skip)
        if has_staticfile_output():
            print(
                "Skipping ytdlp - staticfile extractor already downloaded this",
                file=sys.stderr,
            )
            emit_archive_result_record("succeeded", "staticfile already handled")
            sys.exit(0)

        # Get binary from environment
        binary = config.YTDLP_BINARY

        # Run extraction
        success, output, error = save_ytdlp(url, binary)

        if success:
            status = "noresults" if output == "No media found" else "succeeded"
            # Success - emit ArchiveResult
            emit_archive_result_record(status, rel_output(output) or "")
            sys.exit(0)
        else:
            print(f"ERROR: {error}", file=sys.stderr)
            emit_archive_result_record("failed", error or "")
            sys.exit(1)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result_record("failed", error)
        sys.exit(1)


if __name__ == "__main__":
    main()
