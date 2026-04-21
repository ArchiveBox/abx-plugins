"""
Tests for the bash binary provider plugin.

Tests the bash command installer with safe commands.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


# Get the path to the bash provider hook
PLUGIN_DIR = Path(__file__).parent.parent
INSTALL_HOOK = next(PLUGIN_DIR.glob("on_BinaryRequest__*_bash.py"), None)
BASH_ZX_INSTALL = (
    'npm install --quiet --prefix "$INSTALL_ROOT/npm" zx '
    '&& ln -sf "$INSTALL_ROOT/npm/node_modules/.bin/zx" "$BIN_DIR/bash-zx"'
)


class TestBashProviderHook:
    """Test the bash binary provider hook."""

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

    def test_hook_skips_when_bash_not_allowed(self):
        """Hook should skip when bash not in allowed binproviders."""
        env = os.environ.copy()
        env["SNAP_DIR"] = self.temp_dir
        overrides = json.dumps({"bash": {"install": "echo hello"}})

        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=echo",
                "--binproviders=pip,apt",  # bash not allowed
                f"--overrides={overrides}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        # Should exit cleanly (code 0) when bash not allowed
        assert result.returncode == 0
        assert "bash provider not allowed" in result.stderr

    def test_hook_runs_bash_command_and_finds_binary(self):
        """Hook should run a real bash install command and emit the installed binary."""
        if not shutil.which("npm"):
            pytest.skip("npm is required for the real bash provider install test")

        env = os.environ.copy()
        env["SNAP_DIR"] = self.temp_dir
        env["HOME"] = self.temp_dir
        overrides = json.dumps(
            {"bash": {"install": BASH_ZX_INSTALL}},
        )

        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=bash-zx",
                f"--overrides={overrides}",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, f"Hook failed: {result.stderr}"

        # Parse JSONL output
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("{"):
                try:
                    record = json.loads(line)
                    if (
                        record.get("type") == "Binary"
                        and record.get("name") == "bash-zx"
                    ):
                        assert record["binprovider"] == "bash"
                        assert record["abspath"]
                        assert Path(record["abspath"]).exists()
                        return
                except json.JSONDecodeError:
                    continue

        pytest.fail("No Binary JSONL record found in output")

    def test_hook_fails_for_missing_binary_after_command(self):
        """Hook should fail if binary not found after running bash command."""
        env = os.environ.copy()
        env["SNAP_DIR"] = self.temp_dir
        overrides = json.dumps({"bash": {"install": 'echo "failed install"'}})

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
        assert "unable to install binary nonexistent_binary_xyz123" in (
            result.stderr.lower()
        )

    def test_hook_fails_for_failing_command(self):
        """Hook should fail if bash command returns non-zero exit code."""
        env = os.environ.copy()
        env["SNAP_DIR"] = self.temp_dir
        overrides = json.dumps({"bash": {"install": "exit 1"}})

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
