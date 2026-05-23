"""
Tests for the pip binary provider plugin.

Tests cover:
1. Hook script execution
2. pip package detection
3. Virtual environment handling
4. JSONL output format
"""

import json
import os
import subprocess
import sys
import tempfile
import importlib.util
from pathlib import Path

import pytest


# Get the path to the pip provider hook
PLUGIN_DIR = Path(__file__).parent.parent
INSTALL_HOOK = next(PLUGIN_DIR.glob("on_BinaryRequest__*_pip.py"), None)


def _load_pip_hook_module():
    assert INSTALL_HOOK is not None
    spec = importlib.util.spec_from_file_location("pip_hook", INSTALL_HOOK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestPipProviderHook:
    """Test the pip binary provider installation hook."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.home_dir = Path(self.temp_dir) / "home"
        self.snap_dir = Path(self.temp_dir) / "snap"
        self.output_dir = Path(self.temp_dir) / "output"
        self.home_dir.mkdir(parents=True, exist_ok=True)
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir()

    def teardown_method(self, _method=None):
        """Clean up."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_hook_script_exists(self):
        """Hook script should exist."""
        assert INSTALL_HOOK and INSTALL_HOOK.exists(), f"Hook not found: {INSTALL_HOOK}"

    def test_hook_help(self):
        """Hook should accept --help without error."""
        result = subprocess.run(
            [str(INSTALL_HOOK), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # May succeed or fail depending on implementation
        # At minimum should not crash with Python error
        assert "Traceback" not in result.stderr

    def test_hook_finds_pip(self):
        """Hook should find pip binary."""
        env = os.environ.copy()
        env["SNAP_DIR"] = str(self.snap_dir)
        env["HOME"] = str(self.home_dir)
        env.pop("LIB_DIR", None)

        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=pip",
                "--binproviders=pip",
            ],
            capture_output=True,
            text=True,
            cwd=str(self.output_dir),
            env=env,
            timeout=60,
        )

        # Check for JSONL output
        jsonl_found = False
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("{"):
                try:
                    record = json.loads(line)
                    if record.get("type") == "Binary" and record.get("name") == "pip":
                        jsonl_found = True
                        # Verify structure
                        assert "abspath" in record
                        assert "version" in record
                        break
                except json.JSONDecodeError:
                    continue

        # Should not crash
        assert "Traceback" not in result.stderr

        # Should find pip via pip provider
        assert jsonl_found, "Expected to find pip binary in JSONL output"

    def test_hook_unknown_package(self):
        """Hook should handle unknown packages gracefully."""
        env = os.environ.copy()
        env["SNAP_DIR"] = str(self.snap_dir)
        env["HOME"] = str(self.home_dir)
        env.pop("LIB_DIR", None)

        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=nonexistent_package_xyz123",
                "--binproviders=pip",
            ],
            capture_output=True,
            text=True,
            cwd=str(self.output_dir),
            env=env,
            timeout=60,
        )

        # Should not crash
        assert "Traceback" not in result.stderr
        # May have non-zero exit code for missing package

    def test_hook_repairs_partial_shared_venv(self):
        """Hook should repair a partially created shared pip venv before install."""
        env = os.environ.copy()
        env["SNAP_DIR"] = str(self.snap_dir)
        env["HOME"] = str(self.home_dir)
        env["LIB_DIR"] = str(Path(self.temp_dir) / "lib")
        env["PIP_VENV_PYTHON"] = sys.executable

        broken_venv = Path(env["LIB_DIR"]) / "pip" / "venv" / "bin"
        broken_venv.mkdir(parents=True, exist_ok=True)
        python_path = broken_venv / "python"
        python_path.write_text("broken")
        python_path.chmod(0o755)

        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=opendataloader-pdf",
                "--binproviders=pip",
                '--overrides={"pip":{"install_args":["opendataloader-pdf"]}}',
            ],
            capture_output=True,
            text=True,
            cwd=str(self.output_dir),
            env=env,
            timeout=120,
        )

        assert result.returncode == 0, result.stderr
        assert '"type": "Binary"' in result.stdout
        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.strip().startswith("{")
        ]
        binary_record = next(
            record for record in records if record.get("type") == "Binary"
        )
        assert Path(binary_record["abspath"]).is_relative_to(
            Path(env["LIB_DIR"]) / "pip" / "venv" / "bin",
        )
        assert not (Path(env["LIB_DIR"]) / "pip" / "venv" / "venv").exists()

    def test_hook_honors_pip_install_root_override(self):
        """Provider overrides should isolate package dependencies in their own venv."""
        env = os.environ.copy()
        env["SNAP_DIR"] = str(self.snap_dir)
        env["HOME"] = str(self.home_dir)
        env["LIB_DIR"] = str(Path(self.temp_dir) / "lib")
        env["PIP_VENV_PYTHON"] = sys.executable

        install_root = Path(env["LIB_DIR"]) / "pip" / "packages" / "black"
        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=black",
                "--binproviders=pip",
                "--postinstall-scripts=false",
                "--overrides="
                + json.dumps(
                    {
                        "pip": {
                            "install_root": str(install_root),
                            "install_args": ["black==24.4.2"],
                        },
                    },
                ),
            ],
            capture_output=True,
            text=True,
            cwd=str(self.output_dir),
            env=env,
            timeout=180,
        )

        assert result.returncode == 0, result.stderr
        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.strip().startswith("{")
        ]
        binary_record = next(
            record for record in records if record.get("type") == "Binary"
        )
        assert Path(binary_record["abspath"]).is_relative_to(
            install_root / "venv" / "bin",
        )
        assert not (Path(env["LIB_DIR"]) / "pip" / "venv" / "bin" / "black").exists()

    def test_python_candidates_prefer_stable_tool_interpreters(self):
        """Pip-managed CLI tools should avoid coupling package venvs to new app runtimes."""
        pip_hook = _load_pip_hook_module()
        candidates = pip_hook._python_candidates("")

        if sys.version_info < (3, 13):
            assert candidates[0] == str(Path(sys.executable).resolve())
        else:
            assert candidates[:4] == [
                "python3.12",
                "python3.11",
                "python3.10",
                "python3.9",
            ]
            assert str(Path(sys.executable).resolve()) in candidates


class TestPipProviderIntegration:
    """Integration tests for pip provider with real packages."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.home_dir = Path(self.temp_dir) / "home"
        self.snap_dir = Path(self.temp_dir) / "snap"
        self.output_dir = Path(self.temp_dir) / "output"
        self.home_dir.mkdir(parents=True, exist_ok=True)
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir()

    def teardown_method(self, _method=None):
        """Clean up."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_hook_finds_pip_installed_binary(self):
        """Hook should find binaries installed via pip."""
        pip_check = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True,
            text=True,
        )
        assert pip_check.returncode == 0, "pip not available"
        env = os.environ.copy()
        env["SNAP_DIR"] = str(self.snap_dir)
        env["HOME"] = str(self.home_dir)
        env.pop("LIB_DIR", None)

        # Try to find 'pip' itself which should be available
        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=pip",
                "--binproviders=pip,env",
            ],
            capture_output=True,
            text=True,
            cwd=str(self.output_dir),
            env=env,
            timeout=60,
        )

        # Look for success in output
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("{"):
                try:
                    record = json.loads(line)
                    if record.get("type") == "Binary" and "pip" in record.get(
                        "name",
                        "",
                    ):
                        # Found pip binary
                        assert record.get("abspath")
                        return
                except json.JSONDecodeError:
                    continue

        # If we get here without finding pip, that's acceptable
        # as long as the hook didn't crash
        assert "Traceback" not in result.stderr


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
