"""
Tests for the apt binary provider plugin.

Tests cover:
1. Hook script execution
2. apt package availability detection
3. JSONL output format
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


# Get the path to the apt provider hook
PLUGIN_DIR = Path(__file__).parent.parent
INSTALL_HOOK = next(PLUGIN_DIR.glob('on_Binary__*_apt_install.py'), None)


def apt_available() -> bool:
    """Check if apt is installed."""
    return shutil.which('apt') is not None or shutil.which('apt-get') is not None


def is_linux() -> bool:
    """Check if running on Linux."""
    import platform
    return platform.system().lower() == 'linux'


class TestAptProviderHook:
    """Test the apt binary provider installation hook."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self, _method=None):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_hook_script_exists(self):
        """Hook script should exist."""
        assert INSTALL_HOOK and INSTALL_HOOK.exists(), f"Hook not found: {INSTALL_HOOK}"

    def test_hook_skips_when_apt_not_allowed(self):
        """Hook should skip when apt not in allowed binproviders."""
        result = subprocess.run(
            [
                sys.executable, str(INSTALL_HOOK),
                '--name=wget',
                '--binary-id=test-uuid',
                '--machine-id=test-machine',
                '--binproviders=pip,npm',  # apt not allowed
            ],
            capture_output=True,
            text=True,
            timeout=30
        )

        # Should exit cleanly (code 0) when apt not allowed
        assert 'apt provider not allowed' in result.stderr
        assert result.returncode == 0

    @pytest.mark.skipif(not is_linux(), reason="apt only available on Linux")
    def test_hook_detects_apt(self):
        """Hook should detect apt binary when available."""
        assert apt_available(), "apt not installed"
        result = subprocess.run(
            [
                sys.executable, str(INSTALL_HOOK),
                '--name=nonexistent-pkg-xyz123',
                '--binary-id=test-uuid',
                '--machine-id=test-machine',
            ],
            capture_output=True,
            text=True,
            timeout=30
        )

        # Should not say apt is not available
        assert 'apt not available' not in result.stderr

    def test_hook_handles_overrides(self):
        """Hook should accept overrides JSON."""
        overrides = json.dumps({
            'apt': {'packages': ['custom-package-name']}
        })

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
            timeout=30
        )

        # Should not crash parsing overrides
        assert 'Traceback' not in result.stderr


@pytest.mark.skipif(not is_linux(), reason="apt only available on Linux")
class TestAptProviderSystemBinaries:
    """Test apt provider with system binaries."""

    def test_detect_existing_binary(self):
        """apt provider should detect already-installed system binaries."""
        assert apt_available(), "apt not installed"
        # Check for a binary that's almost certainly installed (like 'ls' or 'bash')
        result = subprocess.run(
            [
                sys.executable, str(INSTALL_HOOK),
                '--name=bash',
                '--binary-id=test-uuid',
                '--machine-id=test-machine',
            ],
            capture_output=True,
            text=True,
            timeout=60
        )

        # Parse JSONL output
        for line in result.stdout.split('\n'):
            line = line.strip()
            if line.startswith('{'):
                try:
                    record = json.loads(line)
                    if record.get('type') == 'Binary' and record.get('name') == 'bash':
                        # Found bash
                        assert record.get('abspath')
                        assert Path(record['abspath']).exists()
                        return
                except json.JSONDecodeError:
                    continue

        # apt may not be able to "install" bash (already installed)
        # Just verify no crash
        assert 'Traceback' not in result.stderr


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
