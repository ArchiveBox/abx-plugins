"""
Integration tests for papersdl plugin

Tests verify:
1. Hook script exists
2. Dependencies installed via validation hooks
3. Verify deps with abx-pkg
4. Paper extraction works on paper URLs
5. JSONL output is correct
6. Config options work
7. Handles non-paper URLs gracefully
"""

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
import pytest

PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = PLUGIN_DIR.parent
_PAPERSDL_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_papersdl.*"), None)
if _PAPERSDL_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
PAPERSDL_HOOK = _PAPERSDL_HOOK
TEST_URL = "https://example.com"

# Module-level cache for binary path
_papersdl_binary_path = None
_papersdl_install_error = None
_papersdl_home_root = None


def require_papersdl_binary() -> str:
    """Return papers-dl binary path or fail with actionable context."""
    binary_path = get_papersdl_binary_path()
    assert binary_path, (
        "papers-dl installation failed. Install hook must install the real papers-dl package "
        f"from PyPI. {_papersdl_install_error or ''}".strip()
    )
    assert Path(binary_path).is_file(), f"papers-dl binary path invalid: {binary_path}"
    return binary_path


def get_papersdl_binary_path():
    """Get the installed papers-dl binary path from cache or by running installation."""
    global _papersdl_binary_path, _papersdl_install_error, _papersdl_home_root
    if _papersdl_binary_path:
        return _papersdl_binary_path

    # Always validate installation path by running the real pip hook.
    pip_hook = PLUGINS_ROOT / "pip" / "on_Binary__11_pip_install.py"
    if pip_hook and pip_hook.exists():
        binary_id = str(uuid.uuid4())
        machine_id = str(uuid.uuid4())
        if not _papersdl_home_root:
            _papersdl_home_root = tempfile.mkdtemp(prefix="papersdl-lib-")

        env = os.environ.copy()
        env["HOME"] = str(_papersdl_home_root)
        env["SNAP_DIR"] = str(Path(_papersdl_home_root) / "data")
        env.pop("LIB_DIR", None)

        cmd = [
            sys.executable,
            str(pip_hook),
            "--binary-id",
            binary_id,
            "--machine-id",
            machine_id,
            "--name",
            "papers-dl",
        ]

        install_result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )

        # Parse Binary from pip installation
        for install_line in install_result.stdout.strip().split("\n"):
            if install_line.strip():
                try:
                    install_record = json.loads(install_line)
                    if (
                        install_record.get("type") == "Binary"
                        and install_record.get("name") == "papers-dl"
                    ):
                        _papersdl_binary_path = install_record.get("abspath")
                        return _papersdl_binary_path
                except json.JSONDecodeError:
                    pass
        _papersdl_install_error = (
            f"pip hook failed with returncode={install_result.returncode}. "
            f"stderr={install_result.stderr.strip()[:400]} "
            f"stdout={install_result.stdout.strip()[:400]}"
        )
        return None

    _papersdl_install_error = f"pip hook not found: {pip_hook}"
    return None


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert PAPERSDL_HOOK.exists(), f"Hook not found: {PAPERSDL_HOOK}"


def test_verify_deps_with_abx_pkg():
    """Verify papers-dl is installed by calling the REAL installation hooks."""
    binary_path = require_papersdl_binary()
    assert Path(binary_path).is_file(), (
        f"Binary path must be a valid file: {binary_path}"
    )


def test_handles_non_paper_url():
    """Test that papers-dl extractor handles non-paper URLs gracefully via hook."""
    binary_path = require_papersdl_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env["PAPERSDL_BINARY"] = binary_path

        # Run papers-dl extraction hook on non-paper URL
        result = subprocess.run(
            [
                sys.executable,
                str(PAPERSDL_HOOK),
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

        # Should exit 0 even for non-paper URL
        assert result.returncode == 0, (
            f"Should handle non-paper URL gracefully: {result.stderr}"
        )

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

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "noresults", (
            f"Non-paper URL should report noresults: {result_json}"
        )
        assert result_json["output_str"] == "No papers found", result_json


def test_config_save_papersdl_false_skips():
    """Test that PAPERSDL_ENABLED=False exits without emitting JSONL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["PAPERSDL_ENABLED"] = "False"

        result = subprocess.run(
            [
                sys.executable,
                str(PAPERSDL_HOOK),
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

        jsonl_lines = [
            line
            for line in result.stdout.strip().split("\n")
            if line.strip().startswith("{")
        ]
        assert len(jsonl_lines) == 1, f"Expected skipped JSONL, got: {jsonl_lines}"
        result_json = json.loads(jsonl_lines[0])
        assert result_json["status"] == "skipped", result_json
        assert result_json["output_str"] == "PAPERSDL_ENABLED=False", result_json


def test_config_timeout():
    """Test that PAPERSDL_TIMEOUT config is respected."""
    binary_path = require_papersdl_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["PAPERSDL_BINARY"] = binary_path
        env["PAPERSDL_TIMEOUT"] = "5"

        result = subprocess.run(
            [
                sys.executable,
                str(PAPERSDL_HOOK),
                "--url",
                "https://example.com",
                "--snapshot-id",
                "testtimeout",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, "Should complete without hanging"


def test_real_doi_download():
    """Test that papers-dl downloads a real paper PDF from a DOI URL."""
    binary_path = require_papersdl_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Public DOI for an open-access arXiv paper.
        doi_url = "https://doi.org/10.48550/arXiv.1706.03762"

        env = os.environ.copy()
        env["PAPERSDL_BINARY"] = binary_path
        env["PAPERSDL_TIMEOUT"] = "120"
        env["SNAP_DIR"] = str(tmpdir)

        result = subprocess.run(
            [
                sys.executable,
                str(PAPERSDL_HOOK),
                "--url",
                doi_url,
                "--snapshot-id",
                "testrealdoi",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )

        assert result.returncode == 0, f"DOI download should succeed: {result.stderr}"

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

        assert result_json, f"Should emit ArchiveResult JSONL. stdout: {result.stdout}"
        assert result_json.get("status") == "succeeded", (
            f"DOI download should succeed: {result_json}"
        )

        output_str = (result_json.get("output_str") or "").strip()
        assert output_str.endswith(".pdf"), (
            f"ArchiveResult must name the downloaded PDF for a single-file result: {result_json}"
        )

        downloaded_files = [
            path for path in (tmpdir / "papersdl").iterdir()
            if path.is_file()
        ]
        assert downloaded_files, f"Downloaded paper path missing in {tmpdir / 'papersdl'}"
        output_path = tmpdir / "papersdl" / output_str
        assert output_path.is_file(), f"Downloaded paper path missing: {output_path}"
        assert output_path.stat().st_size > 0, f"Downloaded paper file is empty: {output_path}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
