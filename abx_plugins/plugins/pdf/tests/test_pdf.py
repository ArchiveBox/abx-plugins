"""
Integration tests for pdf plugin

Tests verify:
    pass
1. Hook script exists
2. Dependencies installed via chrome validation hooks
3. Verify deps with abx-pkg
4. PDF extraction works on https://example.com
5. JSONL output is correct
6. Filesystem output is valid PDF file
7. Config options work
"""

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    get_test_env,
    get_plugin_dir,
    get_hook_script,
    PLUGINS_ROOT,
    chrome_session,
)


PLUGIN_DIR = get_plugin_dir(__file__)
_PDF_HOOK = get_hook_script(PLUGIN_DIR, 'on_Snapshot__*_pdf.*')
if _PDF_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
PDF_HOOK = _PDF_HOOK
NPM_PROVIDER_HOOK = PLUGINS_ROOT / 'npm' / 'on_Binary__install_using_npm_provider.py'
TEST_URL = 'https://example.com'


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert PDF_HOOK.exists(), f"Hook not found: {PDF_HOOK}"


def test_verify_deps_with_abx_pkg():
    """Verify dependencies are available via abx-pkg after hook installation."""
    from abx_pkg import Binary, EnvProvider

    # Verify node is available
    node_binary = Binary(name='node', binproviders=[EnvProvider()])
    node_loaded = node_binary.load()
    assert node_loaded and node_loaded.abspath, "Node.js required for pdf plugin"


def test_extracts_pdf_from_example_com():
    """Test full workflow: extract PDF from real example.com via hook."""
    # Prerequisites checked by earlier test

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        with chrome_session(tmpdir, test_url=TEST_URL) as (_process, _pid, snapshot_chrome_dir, env):
            pdf_dir = snapshot_chrome_dir.parent / 'pdf'
            pdf_dir.mkdir(exist_ok=True)

            # Run PDF extraction hook
            result = subprocess.run(
                ['node', str(PDF_HOOK), f'--url={TEST_URL}', '--snapshot-id=test789'],
                cwd=pdf_dir,
                capture_output=True,
                text=True,
                timeout=120,
                env=env
            )

        # Parse clean JSONL output (hook might fail due to network issues)
        result_json = None
        for line in result.stdout.strip().split('\n'):
            line = line.strip()
            if line.startswith('{'):
                pass
                try:
                    record = json.loads(line)
                    if record.get('type') == 'ArchiveResult':
                        result_json = record
                        break
                except json.JSONDecodeError:
                    pass

        assert result_json, "Should have ArchiveResult JSONL output"

        # Skip verification if network failed
        if result_json['status'] != 'succeeded':
            pass
            if 'TIMED_OUT' in result_json.get('output_str', '') or 'timeout' in result_json.get('output_str', '').lower():
                pass
            pytest.fail(f"Extraction failed: {result_json}")

        assert result.returncode == 0, f"Should exit 0 on success: {result.stderr}"

        # Verify filesystem output (hook writes to current directory)
        pdf_file = pdf_dir / 'output.pdf'
        assert pdf_file.exists(), "output.pdf not created"

        # Verify file is valid PDF
        file_size = pdf_file.stat().st_size
        assert file_size > 500, f"PDF too small: {file_size} bytes"
        assert file_size < 10 * 1024 * 1024, f"PDF suspiciously large: {file_size} bytes"

        # Check PDF magic bytes
        pdf_data = pdf_file.read_bytes()
        assert pdf_data[:4] == b'%PDF', "Should be valid PDF file"


def test_config_save_pdf_false_skips():
    """Test that PDF_ENABLED=False exits without emitting JSONL."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / 'snap'
        snap_dir.mkdir(parents=True, exist_ok=True)
        env = get_test_env() | {'SNAP_DIR': str(snap_dir)}
        env['PDF_ENABLED'] = 'False'

        result = subprocess.run(
            ['node', str(PDF_HOOK), f'--url={TEST_URL}', '--snapshot-id=test999'],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30
        )

        assert result.returncode == 0, f"Should exit 0 when feature disabled: {result.stderr}"

        # Feature disabled - temporary failure, should NOT emit JSONL
        assert 'Skipping' in result.stderr or 'False' in result.stderr, "Should log skip reason to stderr"

        # Should NOT emit any JSONL
        jsonl_lines = [line for line in result.stdout.strip().split('\n') if line.strip().startswith('{')]
        assert len(jsonl_lines) == 0, f"Should not emit JSONL when feature disabled, but got: {jsonl_lines}"


def test_reports_missing_chrome():
    """Test that script reports error when Chrome session is missing."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / 'snap'
        pdf_dir = snap_dir / 'pdf'
        pdf_dir.mkdir(parents=True, exist_ok=True)
        env = get_test_env() | {'SNAP_DIR': str(snap_dir)}

        result = subprocess.run(
            ['node', str(PDF_HOOK), f'--url={TEST_URL}', '--snapshot-id=test123'],
            cwd=pdf_dir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30
        )

        assert result.returncode != 0, "Should fail without shared Chrome session"
        combined = result.stdout + result.stderr
        assert 'chrome session' in combined.lower() or 'chrome plugin' in combined.lower()


def test_runs_with_shared_chrome_session():
    """Test that PDF hook completes when shared Chrome session is available."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        with chrome_session(tmpdir, test_url=TEST_URL) as (_process, _pid, snapshot_chrome_dir, env):
            pdf_dir = snapshot_chrome_dir.parent / 'pdf'
            pdf_dir.mkdir(exist_ok=True)

            result = subprocess.run(
                ['node', str(PDF_HOOK), f'--url={TEST_URL}', '--snapshot-id=testtimeout'],
                cwd=pdf_dir,
                capture_output=True,
                text=True,
                env=env,
                timeout=30
            )

        # Should complete (success or fail, but not hang)
        assert result.returncode in (0, 1), "Should complete without hanging"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
