"""
Integration tests for wget plugin

Tests verify:
1. Validate hook checks for wget binary
2. Verify deps with abxpkg
3. Config options work (WGET_ENABLED, WGET_SAVE_WARC, etc.)
4. Extraction works against real example.com
5. Output files contain actual page content
6. Skip cases work (WGET_ENABLED=False, staticfile present)
7. Failure cases handled (404, network errors)
"""

import json
import os
import shlex
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import (
    install_required_binary_from_config,
    parse_jsonl_output,
)


PLUGIN_DIR = Path(__file__).parent.parent
WGET_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_wget.*"))
TEST_URL = "https://example.com"
PLUGIN_CONFIG = json.loads((PLUGIN_DIR / "config.json").read_text())


def test_hook_script_exists():
    """Verify hook script exists."""
    assert WGET_HOOK.exists(), f"Hook script not found: {WGET_HOOK}"


def test_wget_declares_only_env_apt_brew_providers():
    """required_binaries should declare wget via env,apt,brew only."""
    required_binaries = PLUGIN_CONFIG["required_binaries"]
    binary_record = next(
        (
            record
            for record in required_binaries
            if record.get("name") == "{WGET_BINARY}"
        ),
        None,
    )
    assert binary_record is not None, (
        f"Expected wget required_binaries entry: {required_binaries}"
    )
    assert binary_record["binproviders"] == "env,apt,brew"


def test_verify_deps_with_abxpkg():
    """Verify wget is available via abxpkg."""
    wget_loaded = install_required_binary_from_config(PLUGIN_DIR, "wget")
    assert wget_loaded and wget_loaded.abspath, "wget is required for wget plugin tests"
    assert Path(wget_loaded.abspath).is_file(), wget_loaded.abspath


def test_resolves_wget_with_provider_managed_binary_path(local_example_url):
    """The hook should use the real hydrated binary resolution path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        loaded = install_required_binary_from_config(PLUGIN_DIR, "wget")
        assert loaded.loaded_abspath is not None, "wget should resolve through abxpkg"

        env = os.environ.copy()
        env.update(
            {
                "HOME": str(tmpdir),
                "SNAP_DIR": str(tmpdir),
                "WGET_BINARY": str(loaded.loaded_abspath),
            },
        )

        result = subprocess.run(
            [str(WGET_HOOK), "--url", local_example_url],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Expected ArchiveResult JSONL output"
        assert result_json["type"] == "ArchiveResult", result_json
        assert result_json["status"] == "succeeded", result_json
        assert result_json["output_str"].startswith("wget/"), result_json
        assert (tmpdir / result_json["output_str"]).is_file(), result_json


def test_can_install_wget_via_abxpkg_provider():
    """Test that wget can be resolved or installed via abxpkg providers."""
    loaded = install_required_binary_from_config(PLUGIN_DIR, "wget")
    assert loaded.loaded_abspath is not None, "wget should resolve after installation"
    assert loaded.loaded_abspath.exists(), loaded.loaded_abspath


@pytest.fixture
def local_example_url(httpserver):
    html = """<!doctype html><html><head><title>Example Domain</title></head><body><h1>Example Domain</h1><p>This domain is for use in illustrative examples in documents.</p><a href=\"https://iana.org/\">More information</a></body></html>"""
    httpserver.expect_request("/").respond_with_data(
        html,
        status=200,
        content_type="text/html",
    )
    return httpserver.url_for("/")


def test_archives_example_com(local_example_url):
    """Test full workflow: ensure wget installed then archive a real HTML page."""

    install_required_binary_from_config(PLUGIN_DIR, "wget")

    # Now test archiving
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmpdir)

        # Run wget extraction
        result = subprocess.run(
            [
                str(WGET_HOOK),
                "--url",
                local_example_url,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        # Parse clean JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "succeeded", f"Should succeed: {result_json}"

        # Verify files were downloaded to wget output directory.
        output_root = tmpdir / "wget"
        assert output_root.exists(), "wget output directory was not created"

        downloaded_files = [f for f in output_root.rglob("*") if f.is_file()]
        assert downloaded_files, "No files downloaded"

        # Try the emitted output path first, then fallback to downloaded files.
        assert result_json.get("output_str", "").startswith("wget/"), result_json
        output_path = (tmpdir / result_json.get("output_str", "")).resolve()
        candidate_files = [output_path] if output_path.is_file() else []
        candidate_files.extend(downloaded_files)

        main_html = None
        for candidate in candidate_files:
            content = candidate.read_text(errors="ignore")
            if "example domain" in content.lower():
                main_html = candidate
                break

        assert main_html is not None, (
            "Could not find downloaded file containing example.com content"
        )

        # Verify page content contains REAL example.com text.
        html_content = main_html.read_text(errors="ignore")
        assert len(html_content) > 200, (
            f"HTML content too short: {len(html_content)} bytes"
        )
        assert "example domain" in html_content.lower(), (
            "Missing 'Example Domain' in HTML"
        )
        assert (
            "this domain" in html_content.lower()
            or "illustrative examples" in html_content.lower()
        ), "Missing example.com description text"
        assert (
            "iana" in html_content.lower() or "more information" in html_content.lower()
        ), "Missing IANA reference"


def test_config_save_wget_false_skips():
    """Test that WGET_ENABLED=False exits without emitting JSONL."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Set WGET_ENABLED=False
        env = os.environ.copy()
        env["WGET_ENABLED"] = "False"

        result = subprocess.run(
            [
                str(WGET_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        # Should exit 0 when feature disabled
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
        assert result_json["output_str"] == "WGET_ENABLED=False", result_json


def test_config_save_warc(local_example_url):
    """Test that WGET_SAVE_WARC=True creates WARC files."""

    install_required_binary_from_config(PLUGIN_DIR, "wget")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Set WGET_SAVE_WARC=True explicitly
        env = os.environ.copy()
        env["WGET_SAVE_WARC"] = "True"
        env["SNAP_DIR"] = str(tmpdir)

        result = subprocess.run(
            [
                str(WGET_HOOK),
                "--url",
                local_example_url,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

        assert result.returncode == 0, result.stderr
        result_json = parse_jsonl_output(result.stdout)
        assert result_json is not None, result.stdout
        assert result_json == {
            "type": "ArchiveResult",
            "status": "succeeded",
            "output_str": result_json["output_str"],
        }, result_json
        assert result_json["output_str"].startswith("wget/"), result_json
        assert (tmpdir / result_json["output_str"]).is_file(), result_json

        warc_dir = tmpdir / "wget" / "warc"
        assert warc_dir.is_dir(), "WARC output directory was not created"
        warc_files = [f for f in warc_dir.rglob("*") if f.is_file()]
        assert warc_files, "WARC file not created when WGET_SAVE_WARC=True"
        assert any(f.suffix == ".gz" and f.stat().st_size > 0 for f in warc_files), (
            f"Expected a non-empty compressed WARC file, got: {warc_files}"
        )


def test_staticfile_present_skips(real_staticfile_output):
    """Test that wget skips when staticfile already downloaded."""

    with tempfile.TemporaryDirectory() as tmpdir:
        test_url = "https://httpbin.org/json"
        snapshot_dir = real_staticfile_output(Path(tmpdir), test_url, "wget-static")
        env = os.environ.copy()
        env["SNAP_DIR"] = str(snapshot_dir)
        wget_dir = snapshot_dir / "wget"
        wget_dir.mkdir()

        result = subprocess.run(
            [
                str(WGET_HOOK),
                "--url",
                test_url,
            ],
            cwd=wget_dir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, (
            "Should exit 0 when staticfile already handled the URL"
        )

        result_json = parse_jsonl_output(result.stdout)

        assert result_json, (
            "Should emit ArchiveResult JSONL when staticfile already handled the URL"
        )
        assert result_json["status"] == "noresults", (
            f"Should have status='noresults': {result_json}"
        )
        assert "staticfile" in result_json.get("output_str", "").lower(), (
            "Should mention staticfile in output_str"
        )


def test_handles_404_gracefully(httpserver):
    """Test that wget fails gracefully on 404."""
    install_required_binary_from_config(PLUGIN_DIR, "wget")
    httpserver.expect_request("/nonexistent-page-404").respond_with_data(
        "Not Found",
        status=404,
        content_type="text/plain",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Try to download non-existent page
        result = subprocess.run(
            [
                str(WGET_HOOK),
                "--url",
                httpserver.url_for("/nonexistent-page-404"),
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # Should fail
        assert result.returncode == 1, "Should fail on 404"
        result_json = parse_jsonl_output(result.stdout)
        assert result_json == {
            "type": "ArchiveResult",
            "status": "failed",
            "output_str": "wget failed (exit=8)",
        }, result_json
        assert "ERROR: wget failed (exit=8)" in result.stderr


def test_connection_refused_ignores_executor_process_artifacts(tmp_path):
    """A failed download must not report abx-dl's process files as output."""
    loaded = install_required_binary_from_config(PLUGIN_DIR, "wget")
    assert loaded.loaded_abspath is not None, "wget should resolve through abxpkg"

    output_dir = tmp_path / "wget"
    output_dir.mkdir()
    process_id = "0123456789abcdef0123456789abcdef"
    artifact_stem = f"{WGET_HOOK.stem}.{process_id}"
    stdout_path = output_dir / f"{artifact_stem}.stdout.log"
    stderr_path = output_dir / f"{artifact_stem}.stderr.log"
    pid_path = output_dir / f"{artifact_stem}.pid"
    cmd_path = output_dir / f"{artifact_stem}.sh"

    with socket.socket() as refused_socket:
        refused_socket.bind(("127.0.0.1", 0))
        refused_url = f"http://127.0.0.1:{refused_socket.getsockname()[1]}/"
        env = os.environ.copy()
        env.update(
            {
                "SNAP_DIR": str(tmp_path),
                "WGET_BINARY": str(loaded.loaded_abspath),
                "WGET_WARC_ENABLED": "False",
            },
        )
        cmd = [str(WGET_HOOK), "--url", refused_url]
        cmd_path.write_text(
            f"#!/bin/bash\n{shlex.join(cmd)}\n",
            encoding="utf-8",
        )
        with (
            stdout_path.open("w", encoding="utf-8") as stdout_file,
            stderr_path.open(
                "w",
                encoding="utf-8",
            ) as stderr_file,
        ):
            process = subprocess.Popen(
                cmd,
                cwd=tmp_path,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                env=env,
            )
            pid_path.write_text(str(process.pid), encoding="utf-8")
            returncode = process.wait(timeout=30)

    stdout = stdout_path.read_text(encoding="utf-8")
    stderr = stderr_path.read_text(encoding="utf-8")
    assert returncode == 1, stderr
    result_json = parse_jsonl_output(stdout)
    assert result_json == {
        "type": "ArchiveResult",
        "status": "failed",
        "output_str": "wget failed (exit=4)",
    }, result_json


def test_archives_legitimate_downloaded_shell_script(httpserver, tmp_path):
    """The process-artifact filter must preserve downloaded shell content."""
    loaded = install_required_binary_from_config(PLUGIN_DIR, "wget")
    assert loaded.loaded_abspath is not None, "wget should resolve through abxpkg"
    shell_content = "#!/bin/sh\nprintf 'archived shell content\\n'\n"
    httpserver.expect_request("/release.sh").respond_with_data(
        shell_content,
        status=200,
        content_type="text/x-shellscript",
    )
    env = os.environ.copy()
    env.update(
        {
            "SNAP_DIR": str(tmp_path),
            "WGET_BINARY": str(loaded.loaded_abspath),
            "WGET_WARC_ENABLED": "False",
        },
    )

    result = subprocess.run(
        [str(WGET_HOOK), "--url", httpserver.url_for("/release.sh")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    result_json = parse_jsonl_output(result.stdout)
    assert result_json is not None, result.stdout
    assert result_json["status"] == "succeeded", result_json
    assert result_json["output_str"].endswith("/release.sh"), result_json
    downloaded_shell = tmp_path / result_json["output_str"]
    assert downloaded_shell.read_text(encoding="utf-8") == shell_content


def test_config_timeout_honored(local_example_url):
    """Test that WGET_TIMEOUT config is respected."""
    install_required_binary_from_config(PLUGIN_DIR, "wget")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Set very short timeout
        env = os.environ.copy()
        env["WGET_TIMEOUT"] = "5"
        env["SNAP_DIR"] = str(tmpdir)

        # This should still succeed for example.com (it's fast)
        result = subprocess.run(
            [
                str(WGET_HOOK),
                "--url",
                local_example_url,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, result.stderr
        result_json = parse_jsonl_output(result.stdout)
        assert result_json is not None, result.stdout
        assert result_json["type"] == "ArchiveResult", result_json
        assert result_json["status"] == "succeeded", result_json
        assert result_json["output_str"].startswith("wget/"), result_json
        assert (tmpdir / result_json["output_str"]).is_file(), result_json


def test_config_user_agent(httpserver):
    """Test that WGET_USER_AGENT config is used."""
    install_required_binary_from_config(PLUGIN_DIR, "wget")
    httpserver.expect_request(
        "/",
        headers={"User-Agent": "TestBot/1.0"},
    ).respond_with_data(
        "<!doctype html><html><body><h1>User Agent OK</h1></body></html>",
        status=200,
        content_type="text/html",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Set custom user agent
        env = os.environ.copy()
        env["WGET_USER_AGENT"] = "TestBot/1.0"
        env["SNAP_DIR"] = str(tmpdir)

        result = subprocess.run(
            [
                str(WGET_HOOK),
                "--url",
                httpserver.url_for("/"),
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

        assert result.returncode == 0, result.stderr
        result_json = parse_jsonl_output(result.stdout)
        assert result_json is not None, result.stdout
        assert result_json["type"] == "ArchiveResult", result_json
        assert result_json["status"] == "succeeded", result_json
        assert result_json["output_str"].startswith("wget/"), result_json
        assert (tmpdir / result_json["output_str"]).is_file(), result_json


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
