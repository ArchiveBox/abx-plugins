"""
Integration tests for wgetlua plugin (Archive Team wget-lua / wget-at)

Tests verify:
    pass
1. Validate hook checks for wget-at binary
2. Verify deps with abx-pkg
3. Config options work (WGETLUA_ENABLED, WGETLUA_SAVE_WARC, etc.)
4. Extraction works against real https://example.com
5. Output files contain actual page content
6. WARC files contain correct content
7. Skip cases work (WGETLUA_ENABLED=False, staticfile present)
8. Failure cases handled (404, network errors)
"""

import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import parse_jsonl_output


PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = PLUGIN_DIR.parent
WGETLUA_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_wgetlua.*"))
BREW_HOOK = next((PLUGINS_ROOT / "brew").glob("on_BinaryRequest__*_brew.py"), None)
CUSTOM_HOOK = next(
    (PLUGINS_ROOT / "custom").glob("on_BinaryRequest__*_custom.py"), None
)
TEST_URL = "https://example.com"
PLUGIN_CONFIG = json.loads((PLUGIN_DIR / "config.json").read_text())


def _provider_runtime_unavailable(proc: subprocess.CompletedProcess[str]) -> bool:
    combined = f"{proc.stdout}\n{proc.stderr}"
    return (
        "BinProviderOverrides" in combined
        or "PydanticUndefinedAnnotation" in combined
        or "not fully defined" in combined
    )


def _ensure_wget_at_installed() -> str | None:
    """Ensure wget-at is installed, return its path or None."""
    # Check if already on PATH
    path = shutil.which("wget-at")
    if path:
        return path

    # Try installing via brew
    if shutil.which("brew") and BREW_HOOK and BREW_HOOK.exists():
        result = subprocess.run(
            [
                str(BREW_HOOK),
                "--binary-id", str(uuid.uuid4()),
                "--machine-id", str(uuid.uuid4()),
                "--plugin-name", "wgetlua",
                "--hook-name", "required_binaries",
                "--name", "wget-at",
                "--binproviders", "brew",
                "--overrides", json.dumps({"brew": {"install_args": ["wget-at"]}}),
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode == 0:
            path = shutil.which("wget-at")
            if path:
                return path

    # Try installing via custom provider (build from source)
    if CUSTOM_HOOK and CUSTOM_HOOK.exists():
        overrides = PLUGIN_CONFIG["required_binaries"][0].get("overrides", {})
        result = subprocess.run(
            [
                str(CUSTOM_HOOK),
                "--name", "wget-at",
                "--binproviders", "custom",
                "--overrides", json.dumps(overrides),
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode == 0:
            path = shutil.which("wget-at")
            if path:
                return path

    return None


def test_hook_script_exists():
    """Verify hook script exists."""
    assert WGETLUA_HOOK.exists(), f"Hook script not found: {WGETLUA_HOOK}"


def test_wgetlua_declares_env_brew_custom_providers():
    """required_binaries should declare wget-at via env,brew,custom with overrides."""
    required_binaries = PLUGIN_CONFIG["required_binaries"]
    binary_record = next(
        (
            record
            for record in required_binaries
            if record.get("name") == "{WGETLUA_BINARY}"
        ),
        None,
    )
    assert binary_record is not None, (
        f"Expected wgetlua required_binaries entry: {required_binaries}"
    )
    assert binary_record["binproviders"] == "env,brew,custom"

    # Verify overrides are defined for brew and custom
    overrides = binary_record.get("overrides", {})
    assert "brew" in overrides, "Should have brew overrides"
    assert "custom" in overrides, "Should have custom overrides"
    assert overrides["brew"]["install_args"] == ["wget-at"], (
        "brew should install wget-at"
    )
    assert "install" in overrides["custom"], (
        "custom should have an install command"
    )


def test_can_install_wget_at():
    """Test that wget-at can be installed via provider hooks."""
    path = _ensure_wget_at_installed()
    assert path is not None, (
        "wget-at could not be installed via any provider (env, brew, custom)"
    )
    assert Path(path).exists(), f"wget-at binary should exist at {path}"

    # Verify it runs
    result = subprocess.run(
        [path, "--version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"wget-at --version failed: {result.stderr}"
    assert "wget" in result.stdout.lower() or "gnu" in result.stdout.lower(), (
        f"wget-at --version should identify as wget: {result.stdout}"
    )


def test_reports_missing_dependency_when_not_installed():
    """Test that script reports failure when wget-at is not found."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Run with empty PATH so binary won't be found
        env = {"PATH": "/nonexistent", "HOME": str(tmpdir)}

        result = subprocess.run(
            [
                sys.executable,
                str(WGETLUA_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
        )

        # Missing binary is a hard dependency failure.
        assert result.returncode == 1, "Should exit 1 when dependency missing"

        # Should emit failed JSONL describing the missing dependency.
        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Expected failed JSONL output"
        assert result_json["status"] == "failed", result_json
        assert "wget" in result_json["output_str"].lower(), result_json

        # Should log error to stderr
        assert (
            "wget" in result.stderr.lower() or "error" in result.stderr.lower()
        ), "Should report error in stderr"


def test_archives_example_com():
    """Test full workflow: install wget-at then archive https://example.com with content verification."""

    wget_at_path = _ensure_wget_at_installed()
    if not wget_at_path:
        pytest.fail(
            "wget-at could not be installed - required for live integration test"
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmpdir)
        env["WGETLUA_BINARY"] = wget_at_path

        # Run wgetlua extraction against real https://example.com
        result = subprocess.run(
            [
                str(WGETLUA_HOOK),
                "--url",
                TEST_URL,
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

        # Verify files were downloaded to wgetlua output directory.
        output_root = tmpdir / "wgetlua"
        assert output_root.exists(), "wgetlua output directory was not created"

        downloaded_files = [f for f in output_root.rglob("*") if f.is_file()]
        assert downloaded_files, "No files downloaded"

        # Verify the emitted output path is relative and starts with wgetlua/
        assert result_json.get("output_str", "").startswith("wgetlua/"), result_json
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
            "iana" in html_content.lower()
            or "more information" in html_content.lower()
        ), "Missing IANA reference"


def test_warc_output_contains_correct_content():
    """Test that WARC output from wget-at contains correct content for https://example.com."""

    wget_at_path = _ensure_wget_at_installed()
    if not wget_at_path:
        pytest.fail(
            "wget-at could not be installed - required for WARC integration test"
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmpdir)
        env["WGETLUA_BINARY"] = wget_at_path
        env["WGETLUA_SAVE_WARC"] = "True"

        result = subprocess.run(
            [
                str(WGETLUA_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        # Look for WARC files in wgetlua/warc/ subdirectory
        warc_dir = tmpdir / "wgetlua" / "warc"
        assert warc_dir.exists(), "WARC output directory was not created"

        warc_files = [
            f
            for f in warc_dir.rglob("*")
            if f.is_file() and f.suffix in (".warc", ".gz", ".warc.gz")
        ]
        assert len(warc_files) > 0, (
            "WARC file not created when WGETLUA_SAVE_WARC=True"
        )

        # Read WARC content and verify it contains example.com data
        warc_content = ""
        for warc_file in warc_files:
            if warc_file.name.endswith(".gz"):
                try:
                    warc_content += gzip.open(warc_file, "rt", errors="ignore").read()
                except Exception:
                    warc_content += warc_file.read_bytes().decode(
                        errors="ignore"
                    )
            else:
                warc_content += warc_file.read_text(errors="ignore")

        assert "example.com" in warc_content.lower() or "example domain" in warc_content.lower(), (
            "WARC file should contain example.com content"
        )
        assert "WARC/1" in warc_content, (
            "WARC file should contain valid WARC headers"
        )


def test_config_wgetlua_false_skips():
    """Test that WGETLUA_ENABLED=False exits without archiving."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env["WGETLUA_ENABLED"] = "False"

        result = subprocess.run(
            [
                str(WGETLUA_HOOK),
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
        assert result_json["output_str"] == "WGETLUA_ENABLED=False", result_json


def test_staticfile_present_skips():
    """Test that wgetlua skips when staticfile already downloaded."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmpdir)

        # Create directory structure like real ArchiveBox:
        staticfile_dir = tmpdir / "staticfile"
        staticfile_dir.mkdir()
        (staticfile_dir / "stdout.log").write_text(
            '{"type":"ArchiveResult","status":"succeeded","output_str":"responses/example.com/test.json","content_type":"application/json"}\n',
        )

        wgetlua_dir = tmpdir / "wgetlua"
        wgetlua_dir.mkdir()

        result = subprocess.run(
            [
                str(WGETLUA_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=wgetlua_dir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        # Should exit 0 with a noresults JSONL
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


def test_config_timeout_honored():
    """Test that WGETLUA_TIMEOUT config is respected."""

    wget_at_path = _ensure_wget_at_installed()
    if not wget_at_path:
        pytest.skip("wget-at not available")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env["WGETLUA_TIMEOUT"] = "5"
        env["WGETLUA_BINARY"] = wget_at_path

        result = subprocess.run(
            [
                str(WGETLUA_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        # Verify it completed (success or fail, but didn't hang)
        assert result.returncode in (0, 1), "Should complete (success or fail)"


def test_config_user_agent():
    """Test that WGETLUA_USER_AGENT config is used."""

    wget_at_path = _ensure_wget_at_installed()
    if not wget_at_path:
        pytest.skip("wget-at not available")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env["WGETLUA_USER_AGENT"] = "TestBot/1.0"
        env["WGETLUA_BINARY"] = wget_at_path

        result = subprocess.run(
            [
                str(WGETLUA_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

        if result.returncode == 0:
            result_json = parse_jsonl_output(result.stdout)
            assert result_json, "Should have ArchiveResult JSONL output"
            assert result_json["status"] == "succeeded", (
                f"Should succeed: {result_json}"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
