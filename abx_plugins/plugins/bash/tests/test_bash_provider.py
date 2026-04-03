"""
Tests for the custom binary provider plugin.

Tests the custom bash binary installer with safe commands.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest


# Get the path to the custom provider hook
PLUGIN_DIR = Path(__file__).parent.parent
INSTALL_HOOK = next(PLUGIN_DIR.glob("on_BinaryRequest__*_bash.py"), None)


class TestCustomProviderHook:
    """Test the custom binary provider hook."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self, _method=None):
        """Clean up."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_hook_script_exists(self):
        """Hook script should exist."""
        assert INSTALL_HOOK and INSTALL_HOOK.exists(), f"Hook not found: {INSTALL_HOOK}"

    def test_hook_skips_when_custom_not_allowed(self):
        """Hook should skip when custom not in allowed binproviders."""
        env = os.environ.copy()
        env["SNAP_DIR"] = self.temp_dir
        overrides = json.dumps({"custom": {"install": "echo hello"}})

        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=echo",
                "--binproviders=pip,apt",  # custom not allowed
                f"--overrides={overrides}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        # Should exit cleanly (code 0) when custom not allowed
        assert result.returncode == 0
        assert "custom provider not allowed" in result.stderr

    def test_hook_runs_custom_command_and_finds_binary(self):
        """Hook should run custom command and find the installed binary."""
        env = os.environ.copy()
        env["SNAP_DIR"] = self.temp_dir

        # Create a fake binary inside the BashProvider install dir so the
        # provider can resolve it after running the custom install command.
        bash_bin_dir = Path(env.get("HOME", Path.home())) / ".cache" / "abx-pkg" / "bash" / "bin"
        bash_bin_dir.mkdir(parents=True, exist_ok=True)
        fake_bin = bash_bin_dir / "mybin"
        fake_bin.write_text("#!/bin/sh\necho ok\n")
        fake_bin.chmod(0o755)

        overrides = json.dumps(
            {"custom": {"install": 'echo "custom install simulation"'}},
        )

        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=mybin",
                f"--overrides={overrides}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, f"Hook failed: {result.stderr}"

        # Parse JSONL output
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("{"):
                try:
                    record = json.loads(line)
                    if record.get("type") == "Binary" and record.get("name") == "mybin":
                        assert record["binprovider"] == "custom"
                        assert record["abspath"]
                        return
                except json.JSONDecodeError:
                    continue

        pytest.fail("No Binary JSONL record found in output")

    def test_hook_fails_for_missing_binary_after_command(self):
        """Hook should fail if binary not found after running custom command."""
        env = os.environ.copy()
        env["SNAP_DIR"] = self.temp_dir
        overrides = json.dumps({"custom": {"install": 'echo "failed install"'}})

        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=nonexistent_binary_xyz123",
                f"--overrides={overrides}",  # Doesn't actually install
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        # Should fail since binary not found after command
        assert result.returncode == 1
        assert "not found" in result.stderr.lower() or "unable to" in result.stderr.lower()

    def test_hook_fails_for_failing_command(self):
        """Hook should fail if custom command returns non-zero exit code."""
        env = os.environ.copy()
        env["SNAP_DIR"] = self.temp_dir
        overrides = json.dumps({"custom": {"install": "exit 1"}})

        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=echo",
                f"--overrides={overrides}",  # Command that fails
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        # Should fail with exit code 1
        assert result.returncode == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
