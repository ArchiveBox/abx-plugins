"""
Tests for the npm binary provider plugin.

Tests cover:
1. Hook script execution
2. npm package installation
3. JSONL output format
"""

import json
import os
import shutil
import subprocess
import tempfile
import importlib.util
from pathlib import Path

import pytest
from click.testing import CliRunner

from abx_plugins.plugins.base.test_utils import get_hydrated_required_binaries


# Get the path to the npm provider hook
PLUGIN_DIR = Path(__file__).parent.parent
INSTALL_HOOK = next(PLUGIN_DIR.glob("on_BinaryRequest__*_npm.py"), None)


def npm_available() -> bool:
    """Check if npm is installed."""
    return shutil.which("npm") is not None


class TestNpmProviderHook:
    """Test the npm binary provider installation hook."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self, _method=None):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_hook_script_exists(self):
        """Hook script should exist."""
        assert INSTALL_HOOK and INSTALL_HOOK.exists(), f"Hook not found: {INSTALL_HOOK}"

    def test_required_binaries_declare_node_before_npm(self):
        """npm config should declare the shared Node runtime in required_binaries."""
        required_binaries = get_hydrated_required_binaries(PLUGIN_DIR)
        assert [record.get("name") for record in required_binaries] == ["node"]

    def test_hook_uses_default_lib_dir(self):
        """Hook should fall back to default LIB_DIR when not set."""
        env = os.environ.copy()
        env.pop("LIB_DIR", None)
        env["HOME"] = self.temp_dir

        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=some-package",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert "LIB_DIR environment variable not set" not in result.stderr
        default_prefix = Path(self.temp_dir) / ".config" / "abx" / "lib" / "npm"
        assert default_prefix.exists()

    def test_hook_skips_when_npm_not_allowed(self):
        """Hook should skip when npm not in allowed binproviders."""
        env = os.environ.copy()
        env["HOME"] = self.temp_dir
        env.pop("LIB_DIR", None)

        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=some-package",
                "--binproviders=pip,apt",  # npm not allowed
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        # Should exit cleanly (code 0) when npm not allowed
        assert "npm provider not allowed" in result.stderr
        assert result.returncode == 0

    def test_hook_creates_npm_prefix(self):
        """Hook should create npm prefix directory."""
        env = os.environ.copy()
        env["HOME"] = self.temp_dir
        env.pop("LIB_DIR", None)

        # Even if installation fails, the npm prefix should be created
        subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=nonexistent-xyz123",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        npm_prefix = Path(self.temp_dir) / ".config" / "abx" / "lib" / "npm"
        assert npm_prefix.exists()

    def test_hook_handles_overrides(self):
        """Hook should accept overrides JSON."""
        env = os.environ.copy()
        env["HOME"] = self.temp_dir
        env.pop("LIB_DIR", None)

        overrides = json.dumps({"npm": {"install_args": ["custom-pkg"]}})

        # Just verify it doesn't crash with overrides
        result = subprocess.run(
            [
                str(INSTALL_HOOK),
                "--name=test-pkg",
                f"--overrides={overrides}",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        # May fail to install, but should not crash parsing overrides
        assert "Failed to parse overrides JSON" not in result.stderr


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_hook_only_emits_binary_record(tmp_path, monkeypatch):
    """Hook should emit the installed Binary record and no Machine records."""

    spec = importlib.util.spec_from_file_location("npm_install_hook", INSTALL_HOOK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fake_bin = tmp_path / "lib" / "npm" / "node_modules" / ".bin" / "fake-cli"
    fake_bin.parent.mkdir(parents=True, exist_ok=True)
    fake_bin.write_text("", encoding="utf-8")

    class FakeNpmProvider:
        INSTALLER_BIN = "npm"

        def __init__(self, npm_prefix):
            self.npm_prefix = npm_prefix

    class FakeBinaryResult:
        abspath = fake_bin
        version = "1.2.3"
        sha256 = "deadbeef"

    class FakeBinary:
        def __init__(self, *args, **kwargs):
            pass

        def load_or_install(self):
            return FakeBinaryResult()

    monkeypatch.setattr(module, "NpmProvider", FakeNpmProvider)
    monkeypatch.setattr(module, "Binary", FakeBinary)
    runner = CliRunner()
    env = os.environ.copy()
    env["LIB_DIR"] = str(tmp_path / "lib")

    result = runner.invoke(
        module.main,
        [
            "--name=fake-cli",
        ],
        env=env,
    )

    assert result.exit_code == 0, result.output

    records = [
        json.loads(line) for line in result.output.splitlines() if line.startswith("{")
    ]
    binary_record = next(record for record in records if record.get("type") == "Binary")

    assert binary_record["name"] == "fake-cli"
    assert binary_record["abspath"] == str(fake_bin)
    assert not any(record.get("type") == "Machine" for record in records)
