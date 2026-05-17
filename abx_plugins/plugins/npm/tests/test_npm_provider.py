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
from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import get_hydrated_required_binaries
from abx_plugins.plugins.npm.on_BinaryRequest__10_npm import (
    _missing_requested_packages,
    _npm_package_name,
)


# Get the path to the npm provider hook
PLUGIN_DIR = Path(__file__).parent.parent
INSTALL_HOOK = next(PLUGIN_DIR.glob("on_BinaryRequest__*_npm.py"), None)


def npm_available() -> bool:
    """Check if npm is installed."""
    return shutil.which("npm") is not None


def test_npm_package_name_parses_scoped_and_versioned_args():
    assert _npm_package_name("abxbus@^2.5.4") == "abxbus"
    assert _npm_package_name("@puppeteer/browsers") == "@puppeteer/browsers"
    assert _npm_package_name("@scope/pkg@1.2.3") == "@scope/pkg"
    assert _npm_package_name("--min-release-age=0") is None


def test_missing_requested_packages_detects_companion_packages(tmp_path):
    node_modules = tmp_path / "node_modules"
    (node_modules / "puppeteer").mkdir(parents=True)
    (node_modules / "puppeteer" / "package.json").write_text("{}", encoding="utf-8")

    assert _missing_requested_packages(
        tmp_path,
        ["puppeteer", "@puppeteer/browsers", "abxbus@^2.5.4", "--min-release-age=0"],
    ) == ["@puppeteer/browsers", "abxbus"]


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


def test_hook_only_emits_binary_record(tmp_path):
    """Hook should emit the installed Binary record and no Machine records."""
    if not npm_available():
        pytest.skip("npm not available")

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env.pop("LIB_DIR", None)

    result = subprocess.run(
        [
            str(INSTALL_HOOK),
            "--name=npm",
            "--binproviders=npm",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr

    records = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    binary_record = next(record for record in records if record.get("type") == "Binary")

    assert binary_record["name"] == "npm"
    assert Path(binary_record["abspath"]).exists()
    assert not any(record.get("type") == "Machine" for record in records)
