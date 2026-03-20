"""
Tests for the npm binary provider plugin.

Tests cover:
1. Hook script execution
2. npm package installation
3. PATH and NODE_MODULES_DIR updates
4. JSONL output format
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import importlib.util
from pathlib import Path

import pytest
from click.testing import CliRunner


# Get the path to the npm provider hook
PLUGIN_DIR = Path(__file__).parent.parent
INSTALL_HOOK = next(PLUGIN_DIR.glob("on_Binary__*_npm_install.py"), None)
CRAWL_HOOK = next(PLUGIN_DIR.glob("on_Crawl__*_npm_install.py"), None)


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
        assert CRAWL_HOOK and CRAWL_HOOK.exists(), f"Crawl hook not found: {CRAWL_HOOK}"

    def test_crawl_hook_order_is_after_env_discovery_floor(self):
        """npm crawl hook should not occupy the 00 floor reserved for env discovery."""
        assert CRAWL_HOOK is not None, "Crawl hook should exist"
        assert CRAWL_HOOK.name.startswith("on_Crawl__01_"), CRAWL_HOOK.name

    def test_hook_uses_default_lib_dir(self):
        """Hook should fall back to default LIB_DIR when not set."""
        env = os.environ.copy()
        env.pop("LIB_DIR", None)
        env["HOME"] = self.temp_dir

        result = subprocess.run(
            [str(INSTALL_HOOK),
                "--name=some-package",
                "--binary-id=test-uuid",
                "--machine-id=test-machine",
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
            [str(INSTALL_HOOK),
                "--name=some-package",
                "--binary-id=test-uuid",
                "--machine-id=test-machine",
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
            [str(INSTALL_HOOK),
                "--name=nonexistent-xyz123",
                "--binary-id=test-uuid",
                "--machine-id=test-machine",
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
            [str(INSTALL_HOOK),
                "--name=test-pkg",
                "--binary-id=test-uuid",
                "--machine-id=test-machine",
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


def test_hook_emits_node_module_aliases(tmp_path, monkeypatch):
    """Hook should emit NODE_MODULES_DIR, NODE_MODULE_DIR, and NODE_PATH together."""

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
    monkeypatch.setattr(module, "EnvProvider", lambda: object())

    runner = CliRunner()
    env = os.environ.copy()
    env["LIB_DIR"] = str(tmp_path / "lib")

    result = runner.invoke(
        module.main,
        [
            "--name=fake-cli",
            "--binary-id=test-binary",
            "--machine-id=test-machine",
        ],
        env=env,
    )

    assert result.exit_code == 0, result.output

    records = [
        json.loads(line)
        for line in result.output.splitlines()
        if line.startswith("{")
    ]
    machine_configs = [
        record["config"]
        for record in records
        if record.get("type") == "Machine"
    ]
    node_config = next(
        config
        for config in machine_configs
        if "NODE_MODULES_DIR" in config
    )

    assert node_config["NODE_MODULES_DIR"] == node_config["NODE_MODULE_DIR"]
    assert node_config["NODE_MODULES_DIR"] == node_config["NODE_PATH"]


def test_hook_uses_resolved_binary_path_for_node_module_aliases(tmp_path, monkeypatch):
    """Hook should point NODE_MODULES_DIR at the module tree that owns the resolved binary."""

    spec = importlib.util.spec_from_file_location("npm_install_hook", INSTALL_HOOK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    requested_lib = tmp_path / "requested-lib"
    resolved_node_modules = tmp_path / "shared-lib" / "npm" / "node_modules"
    fake_bin = resolved_node_modules / ".bin" / "puppeteer"
    fake_bin.parent.mkdir(parents=True, exist_ok=True)
    fake_bin.write_text("", encoding="utf-8")

    class FakeNpmProvider:
        INSTALLER_BIN = "npm"

        def __init__(self, npm_prefix):
            self.npm_prefix = npm_prefix

    class FakeBinaryResult:
        abspath = fake_bin
        version = "24.40.0"
        sha256 = "deadbeef"

    class FakeBinary:
        def __init__(self, *args, **kwargs):
            pass

        def load_or_install(self):
            return FakeBinaryResult()

    monkeypatch.setattr(module, "NpmProvider", FakeNpmProvider)
    monkeypatch.setattr(module, "Binary", FakeBinary)
    monkeypatch.setattr(module, "EnvProvider", lambda: object())

    runner = CliRunner()
    env = os.environ.copy()
    env["LIB_DIR"] = str(requested_lib)

    result = runner.invoke(
        module.main,
        [
            "--name=puppeteer",
            "--binary-id=test-binary",
            "--machine-id=test-machine",
        ],
        env=env,
    )

    assert result.exit_code == 0, result.output

    records = [
        json.loads(line)
        for line in result.output.splitlines()
        if line.startswith("{")
    ]
    node_config = next(
        record["config"]
        for record in records
        if record.get("type") == "Machine" and "NODE_MODULES_DIR" in record.get("config", {})
    )

    assert node_config["NODE_MODULES_DIR"] == str(resolved_node_modules)
    assert node_config["NODE_MODULE_DIR"] == str(resolved_node_modules)
    assert node_config["NODE_PATH"] == str(resolved_node_modules)
