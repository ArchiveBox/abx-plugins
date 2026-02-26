"""
Tests for the parse_dom_outlinks plugin.

Tests the real DOM outlinks hook with an actual URL to verify
link extraction and categorization.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    get_test_env,
    get_plugin_dir,
    get_hook_script,
    chrome_test_url,
)


def chrome_available() -> bool:
    """Check if Chrome/Chromium is available."""
    for name in ['chromium', 'chromium-browser', 'google-chrome', 'chrome']:
        if shutil.which(name):
            return True
    return False


# Get the path to the parse_dom_outlinks hook
PLUGIN_DIR = get_plugin_dir(__file__)
OUTLINKS_HOOK = get_hook_script(PLUGIN_DIR, 'on_Snapshot__*_parse_dom_outlinks.*')


class TestParseDomOutlinksPlugin:
    """Test the parse_dom_outlinks plugin."""

    def test_outlinks_hook_exists(self):
        """DOM outlinks hook script should exist."""
        assert OUTLINKS_HOOK is not None, "DOM outlinks hook not found in plugin directory"
        assert OUTLINKS_HOOK.exists(), f"Hook not found: {OUTLINKS_HOOK}"


class TestParseDomOutlinksWithChrome:
    """Integration tests for parse_dom_outlinks plugin with Chrome."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self, _method=None):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_outlinks_extracts_links_from_page(self, chrome_test_url):
        """DOM outlinks hook should extract and categorize links from page."""
        test_url = chrome_test_url
        snapshot_id = 'test-outlinks-snapshot'

        try:
            with chrome_session(
                self.temp_dir,
                crawl_id='test-outlinks-crawl',
                snapshot_id=snapshot_id,
                test_url=test_url,
                navigate=True,
                timeout=30,
            ) as (chrome_process, chrome_pid, snapshot_chrome_dir, env):
                # Use the environment from chrome_session (already has CHROME_HEADLESS=true)


                # Run outlinks hook with the active Chrome session
                result = subprocess.run(
                    ['node', str(OUTLINKS_HOOK), f'--url={test_url}', f'--snapshot-id={snapshot_id}'],
                    cwd=str(snapshot_chrome_dir),
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env
                )

                # Check for output file
                snap_dir = Path(env['SNAP_DIR'])
                outlinks_output = snap_dir / 'parse_dom_outlinks' / 'outlinks.json'

                outlinks_data = None
                json_error = None

                # Try parsing from file first
                if outlinks_output.exists():
                    with open(outlinks_output) as f:
                        try:
                            outlinks_data = json.load(f)
                        except json.JSONDecodeError as e:
                            json_error = str(e)

                # Verify hook ran successfully
                assert result.returncode == 0, f"Hook failed: {result.stderr}"
                assert 'Traceback' not in result.stderr

                # Verify we got outlinks data with expected categories
                assert outlinks_data is not None, (
                    f"No outlinks data found - file missing or invalid JSON: {json_error}"
                )

                assert 'url' in outlinks_data, f"Missing url: {outlinks_data}"
                assert 'hrefs' in outlinks_data, f"Missing hrefs: {outlinks_data}"
                # example.com has at least one link (to iana.org)
                assert isinstance(outlinks_data['hrefs'], list)

        except RuntimeError:
            raise


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
