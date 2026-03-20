"""
Tests for the env binary provider plugin.

Tests the real env provider hook with actual system binaries.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


# Get the path to the env provider hook
PLUGIN_DIR = Path(__file__).parent.parent
INSTALL_HOOK = next(PLUGIN_DIR.glob("on_Binary__*_env_discover.py"), None)


class TestEnvProviderHook:
    """Test the env binary provider hook."""

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

    def test_hook_runs_before_other_binary_provider_hooks(self):
        """Env discovery should sort before install-capable provider hooks."""
        other_provider_hooks = [
            next((PLUGIN_DIR.parent / provider).glob("on_Binary__*.py"), None)
            for provider in ("npm", "pip", "brew", "apt", "custom")
        ]

        assert INSTALL_HOOK is not None, "Env hook should exist"
        for hook in other_provider_hooks:
            assert hook is not None and hook.exists(), f"Missing provider hook: {hook}"
            assert INSTALL_HOOK.name < hook.name, (
                f"{INSTALL_HOOK.name} should sort before {hook.name}"
            )
        assert INSTALL_HOOK.name.startswith("on_Binary__00_"), INSTALL_HOOK.name

    def test_hook_finds_python(self):
        """Hook should find python3 binary in PATH."""
        env = os.environ.copy()
        env["SNAP_DIR"] = self.temp_dir

        result = subprocess.run(
            [str(INSTALL_HOOK),
                "--name=python3",
                "--binary-id=test-uuid",
                "--machine-id=test-machine",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        # Should succeed and output JSONL
        assert result.returncode == 0, f"Hook failed: {result.stderr}"

        # Parse JSONL output
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("{"):
                try:
                    record = json.loads(line)
                    if (
                        record.get("type") == "Binary"
                        and record.get("name") == "python3"
                    ):
                        assert record["binprovider"] == "env"
                        assert record["abspath"]
                        assert Path(record["abspath"]).exists()
                        return
                except json.JSONDecodeError:
                    continue

        pytest.fail("No Binary JSONL record found in output")

    def test_hook_finds_bash(self):
        """Hook should find bash binary in PATH."""
        env = os.environ.copy()
        env["SNAP_DIR"] = self.temp_dir

        result = subprocess.run(
            [str(INSTALL_HOOK),
                "--name=bash",
                "--binary-id=test-uuid",
                "--machine-id=test-machine",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        # Should succeed and output JSONL
        assert result.returncode == 0, f"Hook failed: {result.stderr}"

        # Parse JSONL output
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("{"):
                try:
                    record = json.loads(line)
                    if record.get("type") == "Binary" and record.get("name") == "bash":
                        assert record["binprovider"] == "env"
                        assert record["abspath"]
                        return
                except json.JSONDecodeError:
                    continue

        pytest.fail("No Binary JSONL record found in output")

    def test_hook_fails_for_missing_binary(self):
        """Hook should fail for binary not in PATH."""
        env = os.environ.copy()
        env["SNAP_DIR"] = self.temp_dir

        result = subprocess.run(
            [str(INSTALL_HOOK),
                "--name=nonexistent_binary_xyz123",
                "--binary-id=test-uuid",
                "--machine-id=test-machine",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        # Should fail with exit code 1
        assert result.returncode == 1
        assert "not found" in result.stderr.lower()

    def test_hook_skips_when_env_not_allowed(self):
        """Hook should skip when env not in allowed binproviders."""
        env = os.environ.copy()
        env["SNAP_DIR"] = self.temp_dir

        result = subprocess.run(
            [str(INSTALL_HOOK),
                "--name=python3",
                "--binary-id=test-uuid",
                "--machine-id=test-machine",
                "--binproviders=pip,apt",  # env not allowed
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        # Should exit cleanly (code 0) when env not allowed
        assert result.returncode == 0
        assert "env provider not allowed" in result.stderr


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
