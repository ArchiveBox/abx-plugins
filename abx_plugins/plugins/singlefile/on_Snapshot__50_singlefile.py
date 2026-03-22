#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic-settings",
#     "rich-click",
#     "abx-plugins",
# ]
# [tool.uv.sources]
# abx-plugins = { path = "../../..", editable = true }
# ///
"""
Archive a URL using SingleFile.

Usage: on_Snapshot__singlefile.py --url=<url>
Output: Writes singlefile.html to $PWD

Environment variables:
    SINGLEFILE_ENABLED: Enable SingleFile archiving (default: True)
    SINGLEFILE_BINARY: Path to SingleFile binary (default: single-file)
    SINGLEFILE_CHROME_BINARY: Path to Chrome binary (x-fallback: CHROME_BINARY) [unused; shared Chrome session required]
    SINGLEFILE_TIMEOUT: Timeout in seconds (x-fallback: TIMEOUT)
    SINGLEFILE_USER_AGENT: User agent string (x-fallback: USER_AGENT)
    SINGLEFILE_COOKIES_FILE: Path to cookies file (x-fallback: COOKIES_FILE)
    SINGLEFILE_CHECK_SSL_VALIDITY: Whether to verify SSL certs (x-fallback: CHECK_SSL_VALIDITY)
    SINGLEFILE_CHROME_ARGS: Chrome command-line arguments (x-fallback: CHROME_ARGS) [unused; shared Chrome session required]
    SINGLEFILE_ARGS: Default SingleFile arguments (JSON array)
    SINGLEFILE_ARGS_EXTRA: Extra arguments to append (JSON array)
"""

import os
import subprocess
import sys
import threading
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    load_config,
    emit_archive_result_record,
    has_staticfile_output,
)

import rich_click as click


# Extractor metadata
PLUGIN_NAME = "singlefile"
BIN_NAME = "single-file"
BIN_PROVIDERS = "env,npm"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
OUTPUT_FILE = "singlefile.html"
EXTENSION_SAVE_SCRIPT = Path(__file__).parent / "singlefile_extension_save.js"
CHROME_UTILS_SCRIPT = Path(__file__).parent.parent / "chrome" / "chrome_utils.js"


def temp_path_for(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.tmp")


# Chrome session directory (relative to extractor output dir)
# Note: Chrome binary is obtained via CHROME_BINARY env var, not searched for.
# The centralized Chrome binary search is in chrome_utils.js findChromium().
CHROME_SESSION_DIR = "../chrome"


def summarize_error(detail: str) -> str:
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    if not lines:
        return ""

    non_debug_lines = [line for line in lines if not line.startswith("[singlefile]")]
    preferred_lines = non_debug_lines or lines

    for line in reversed(preferred_lines):
        for prefix in ("ERROR:", "Error:", "[❌]"):
            if line.startswith(prefix):
                return line.removeprefix(prefix).strip()

    return preferred_lines[-1]


def run_chrome_utils(
    command: str,
    *args: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CHROME_UTILS_SCRIPT), command, *args],
        cwd=str(OUTPUT_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def get_browser_server_url(
    timeout: int,
    require_target: bool = True,
) -> tuple[str | None, str]:
    timeout_ms = max(1000, timeout * 1000)
    result = run_chrome_utils(
        "getBrowserServerUrl",
        CHROME_SESSION_DIR,
        str(timeout_ms),
        "true" if require_target else "false",
        timeout=max(timeout + 5, 10),
    )
    if result.returncode != 0:
        detail = summarize_error(result.stderr or result.stdout)
        return None, detail or "No Chrome session found (chrome plugin must run first)"
    return result.stdout.strip() or None, ""


def save_singlefile(url: str, binary: str) -> tuple[bool, str | None, str]:
    """
    Archive URL using SingleFile.

    Requires a Chrome session (from chrome plugin) and connects to it via CDP.

    Returns: (success, output_path, error_message)
    """
    print(f"[singlefile] CLI mode start url={url}", file=sys.stderr)
    # Load config from config.json (auto-resolves x-aliases and x-fallback from env)
    config = load_config()
    timeout = config.SINGLEFILE_TIMEOUT
    user_agent = config.SINGLEFILE_USER_AGENT
    check_ssl = config.SINGLEFILE_CHECK_SSL_VALIDITY
    cookies_file = config.SINGLEFILE_COOKIES_FILE
    singlefile_args = config.SINGLEFILE_ARGS
    singlefile_args_extra = config.SINGLEFILE_ARGS_EXTRA
    # Chrome args/binary are intentionally ignored because we require a shared Chrome session

    cmd = [binary, *singlefile_args]

    cdp_remote_url, error = get_browser_server_url(
        timeout=min(10, max(1, timeout // 10)),
        require_target=True,
    )
    if not cdp_remote_url:
        return (
            False,
            None,
            error or "No Chrome session found (chrome plugin must run first)",
        )

    print(
        f"[singlefile] Using existing Chrome session: {cdp_remote_url}",
        file=sys.stderr,
    )
    cmd.extend(["--browser-server", cdp_remote_url])

    # SSL handling
    if not check_ssl:
        cmd.append("--browser-ignore-insecure-certs")

    if user_agent:
        cmd.extend(["--user-agent", user_agent])

    if cookies_file and Path(cookies_file).is_file():
        cmd.extend(["--browser-cookies-file", cookies_file])

    # Add extra args from config
    if singlefile_args_extra:
        cmd.extend(singlefile_args_extra)

    # Output directory is current directory (hook already runs in output dir)
    output_dir = Path(OUTPUT_DIR)
    output_path = output_dir / OUTPUT_FILE
    temp_output_path = temp_path_for(output_path)

    cmd.extend([url, str(temp_output_path)])
    print(f"[singlefile] CLI command: {' '.join(cmd[:6])} ...", file=sys.stderr)

    try:
        output_lines: list[str] = []
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
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

        if temp_output_path.exists() and temp_output_path.stat().st_size > 0:
            temp_output_path.replace(output_path)
            return True, f"{PLUGIN_DIR}/{OUTPUT_FILE}", ""
        else:
            stderr = combined_output
            if "ERR_NAME_NOT_RESOLVED" in stderr:
                return False, None, "DNS resolution failed"
            if "ERR_CONNECTION_REFUSED" in stderr:
                return False, None, "Connection refused"
            detail = (stderr or "").strip()
            if len(detail) > 2000:
                detail = detail[:2000]
            cmd_preview = list(cmd)
            if "--browser-args" in cmd_preview:
                idx = cmd_preview.index("--browser-args")
                if idx + 1 < len(cmd_preview):
                    cmd_preview[idx + 1] = "<json>"
            cmd_str = " ".join(cmd_preview)
            return False, None, f"SingleFile failed (cmd={cmd_str}): {detail}"

    except subprocess.TimeoutExpired:
        return False, None, f"Timed out after {timeout} seconds"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


def save_singlefile_with_extension(
    url: str,
    timeout: int,
) -> tuple[bool, str | None, str]:
    """Save using the SingleFile Chrome extension via existing Chrome session."""
    print(f"[singlefile] Extension mode start url={url}", file=sys.stderr)

    if not EXTENSION_SAVE_SCRIPT.exists():
        print(
            f"[singlefile] Missing helper script: {EXTENSION_SAVE_SCRIPT}",
            file=sys.stderr,
        )
        return False, None, "SingleFile extension helper script missing"

    output_path = Path(OUTPUT_DIR) / OUTPUT_FILE
    temp_output_path = temp_path_for(output_path)
    cmd = [
        str(EXTENSION_SAVE_SCRIPT),
        f"--url={url}",
        f"--output-path={temp_output_path}",
    ]
    print(f"[singlefile] helper_cmd={' '.join(cmd)}", file=sys.stderr)

    try:
        output_lines: list[str] = []
        error_lines: list[str] = []
        process = subprocess.Popen(
            cmd,
            cwd=str(OUTPUT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        def _read_stream(stream, sink, label: str) -> None:
            if not stream:
                return
            for line in stream:
                sink.append(line)
                sys.stderr.write(line)
                sys.stderr.flush()

        stdout_thread = threading.Thread(
            target=_read_stream,
            args=(process.stdout, output_lines, "stdout"),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_read_stream,
            args=(process.stderr, error_lines, "stderr"),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            print(
                f"[singlefile] Extension helper timed out after {timeout}s",
                file=sys.stderr,
            )
            return False, None, f"Timed out after {timeout} seconds"

        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)

        result_stdout = "".join(output_lines).encode("utf-8", errors="replace")
        result_stderr = "".join(error_lines).encode("utf-8", errors="replace")
        result_returncode = process.returncode
    except Exception as e:
        print(
            f"[singlefile] Extension helper error: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return False, None, f"{type(e).__name__}: {e}"

    print(f"[singlefile] helper_returncode={result_returncode}", file=sys.stderr)
    print(
        f"[singlefile] helper_stdout_len={len(result_stdout or b'')}",
        file=sys.stderr,
    )
    print(
        f"[singlefile] helper_stderr_len={len(result_stderr or b'')}",
        file=sys.stderr,
    )

    if result_returncode == 0:
        # Prefer explicit stdout path, fallback to local output file
        out_text = result_stdout.decode("utf-8", errors="replace").strip()
        if out_text and Path(out_text).exists():
            temp_output_path = Path(out_text)
        if temp_output_path.exists() and temp_output_path.stat().st_size > 0:
            temp_output_path.replace(output_path)
            print(f"[singlefile] Extension output: {output_path}", file=sys.stderr)
            return True, f"{PLUGIN_DIR}/{OUTPUT_FILE}", ""
        return False, None, "SingleFile extension completed but no output file found"

    stderr = result_stderr.decode("utf-8", errors="replace").strip()
    stdout = result_stdout.decode("utf-8", errors="replace").strip()
    detail = stderr or stdout
    return False, None, summarize_error(detail) or "SingleFile extension failed"


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="URL to archive")
def main(url: str):
    """Archive a URL using SingleFile."""

    print(f"[singlefile] Hook starting pid={os.getpid()} url={url}", file=sys.stderr)
    output = None
    status = "failed"
    error = ""

    try:
        config = load_config()

        # Check if SingleFile is enabled
        if not config.SINGLEFILE_ENABLED:
            print("Skipping SingleFile (SINGLEFILE_ENABLED=False)", file=sys.stderr)
            emit_archive_result_record("skipped", "SINGLEFILE_ENABLED=False")
            sys.exit(0)

        # Check if staticfile extractor already handled this (permanent skip)
        if has_staticfile_output():
            print(
                "Skipping SingleFile - staticfile extractor already downloaded this",
                file=sys.stderr,
            )
            emit_archive_result_record("noresults", "staticfile already handled")
            sys.exit(0)

        # Prefer SingleFile extension via existing Chrome session
        timeout = config.SINGLEFILE_TIMEOUT
        success, output, error = save_singlefile_with_extension(url, timeout)
        status = "succeeded" if success else "failed"

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        status = "failed"

    if error:
        print(f"ERROR: {error}", file=sys.stderr)

    emit_archive_result_record(status, output or error or "")

    sys.exit(0 if status != "failed" else 1)


if __name__ == "__main__":
    main()
