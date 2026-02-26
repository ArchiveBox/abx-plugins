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
from pathlib import Path

import pytest


# Get the path to the npm provider hook
PLUGIN_DIR = Path(__file__).parent.parent
INSTALL_HOOK = next(PLUGIN_DIR.glob('on_Binary__*_npm_install.py'), None)


def npm_available() -> bool:
    """Check if npm is installed."""
    return shutil.which('npm') is not None


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

    def test_hook_uses_default_lib_dir(self):
        """Hook should fall back to default LIB_DIR when not set."""
        env = os.environ.copy()
        env.pop('LIB_DIR', None)
        env['HOME'] = self.temp_dir

        result = subprocess.run(
            [
                sys.executable, str(INSTALL_HOOK),
                '--name=some-package',
                '--binary-id=test-uuid',
                '--machine-id=test-machine',
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30
        )

        assert 'LIB_DIR environment variable not set' not in result.stderr
        default_prefix = Path(self.temp_dir) / '.config' / 'abx' / 'lib' / 'npm'
        assert default_prefix.exists()

    def test_hook_skips_when_npm_not_allowed(self):
        """Hook should skip when npm not in allowed binproviders."""
        env = os.environ.copy()
        env['HOME'] = self.temp_dir
        env.pop('LIB_DIR', None)

        result = subprocess.run(
            [
                sys.executable, str(INSTALL_HOOK),
                '--name=some-package',
                '--binary-id=test-uuid',
                '--machine-id=test-machine',
                '--binproviders=pip,apt',  # npm not allowed
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30
        )

        # Should exit cleanly (code 0) when npm not allowed
        assert 'npm provider not allowed' in result.stderr
        assert result.returncode == 0

    def test_hook_creates_npm_prefix(self):
        """Hook should create npm prefix directory."""
        env = os.environ.copy()
        env['HOME'] = self.temp_dir
        env.pop('LIB_DIR', None)

        # Even if installation fails, the npm prefix should be created
        subprocess.run(
            [
                sys.executable, str(INSTALL_HOOK),
                '--name=nonexistent-xyz123',
                '--binary-id=test-uuid',
                '--machine-id=test-machine',
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=60
        )

        npm_prefix = Path(self.temp_dir) / '.config' / 'abx' / 'lib' / 'npm'
        assert npm_prefix.exists()

    def test_hook_handles_overrides(self):
        """Hook should accept overrides JSON."""
        env = os.environ.copy()
        env['HOME'] = self.temp_dir
        env.pop('LIB_DIR', None)

        overrides = json.dumps({'npm': {'packages': ['custom-pkg']}})

        # Just verify it doesn't crash with overrides
        result = subprocess.run(
            [
                sys.executable, str(INSTALL_HOOK),
                '--name=test-pkg',
                '--binary-id=test-uuid',
                '--machine-id=test-machine',
                f'--overrides={overrides}',
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=60
        )

        # May fail to install, but should not crash parsing overrides
        assert 'Failed to parse overrides JSON' not in result.stderr


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
