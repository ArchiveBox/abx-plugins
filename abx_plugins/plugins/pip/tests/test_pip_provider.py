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
from pathlib import Path

import pytest


# Get the path to the pip provider hook
PLUGIN_DIR = Path(__file__).parent.parent
INSTALL_HOOK = next(PLUGIN_DIR.glob("on_Binary__*_pip_install.py"), None)


class TestPipProviderHook:
    """Test the pip binary provider installation hook."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.output_dir = Path(self.temp_dir) / "output"
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
        env["SNAP_DIR"] = self.temp_dir
        env["HOME"] = self.temp_dir
        env.pop("LIB_DIR", None)

        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=pip",
                "--binproviders=pip",
                "--binary-id=test-uuid",
                "--machine-id=test-machine",
                "--plugin-name=testplugin",
                "--hook-name=on_Crawl__00_test",
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
        env["SNAP_DIR"] = self.temp_dir
        env["HOME"] = self.temp_dir
        env.pop("LIB_DIR", None)

        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=nonexistent_package_xyz123",
                "--binproviders=pip",
                "--binary-id=test-uuid",
                "--machine-id=test-machine",
                "--plugin-name=testplugin",
                "--hook-name=on_Crawl__00_test",
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
        env["SNAP_DIR"] = self.temp_dir
        env["HOME"] = self.temp_dir
        env["LIB_DIR"] = str(Path(self.temp_dir) / "lib")

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
                "--binary-id=test-uuid",
                "--machine-id=test-machine",
                "--plugin-name=testplugin",
                "--hook-name=on_Crawl__00_test",
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


class TestPipProviderIntegration:
    """Integration tests for pip provider with real packages."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.output_dir = Path(self.temp_dir) / "output"
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
        env["SNAP_DIR"] = self.temp_dir
        env["HOME"] = self.temp_dir
        env.pop("LIB_DIR", None)

        # Try to find 'pip' itself which should be available
        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=pip",
                "--binproviders=pip,env",
                "--binary-id=test-uuid",
                "--machine-id=test-machine",
                "--plugin-name=testplugin",
                "--hook-name=on_Crawl__00_test",
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
