"""
Tests for the SEO plugin.

Tests the real SEO hook with an actual URL to verify
meta tag extraction.
"""

import json
import subprocess
import tempfile
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    CHROME_NAVIGATE_HOOK,
    get_plugin_dir,
    get_hook_script,
)


# Get the path to the SEO hook
PLUGIN_DIR = get_plugin_dir(__file__)
SEO_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_seo.*")


class TestSEOPlugin:
    """Test the SEO plugin."""

    def test_seo_hook_exists(self):
        """SEO hook script should exist."""
        assert SEO_HOOK is not None, "SEO hook not found in plugin directory"
        assert SEO_HOOK.exists(), f"Hook not found: {SEO_HOOK}"


class TestSEOWithChrome:
    """Integration tests for SEO plugin with Chrome."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self, _method=None):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_seo_extracts_meta_tags(self, chrome_test_url):
        """SEO hook should extract meta tags from a real URL."""
        test_url = chrome_test_url
        snapshot_id = "test-seo-snapshot"

        with chrome_session(
            self.temp_dir,
            crawl_id="test-seo-crawl",
            snapshot_id=snapshot_id,
            test_url=test_url,
            navigate=False,
            timeout=30,
        ) as (chrome_process, chrome_pid, snapshot_chrome_dir, env):
            seo_dir = snapshot_chrome_dir.parent / "seo"
            seo_dir.mkdir(exist_ok=True)

            nav_result = subprocess.run(
                [
                    "node",
                    str(CHROME_NAVIGATE_HOOK),
                    f"--url={test_url}",
                    f"--snapshot-id={snapshot_id}",
                ],
                cwd=str(snapshot_chrome_dir),
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"

            # Run SEO hook with the active Chrome session
            result = subprocess.run(
                [
                    "node",
                    str(SEO_HOOK),
                    f"--url={test_url}",
                    f"--snapshot-id={snapshot_id}",
                ],
                cwd=str(seo_dir),
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )

            # Check for output file
            seo_output = seo_dir / "seo.json"

            seo_data = None

            # Try parsing from file first
            if seo_output.exists():
                with open(seo_output) as f:
                    try:
                        seo_data = json.load(f)
                    except json.JSONDecodeError:
                        pass

            # Try parsing from stdout if not in file
            if not seo_data:
                for line in result.stdout.split("\n"):
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            record = json.loads(line)
                            # SEO data typically has title, description, or og: tags
                            if any(
                                key in record
                                for key in [
                                    "title",
                                    "description",
                                    "og:title",
                                    "canonical",
                                ]
                            ):
                                seo_data = record
                                break
                        except json.JSONDecodeError:
                            continue

            # Verify hook ran successfully
            assert result.returncode == 0, f"Hook failed: {result.stderr}"
            assert "Traceback" not in result.stderr
            assert "Error:" not in result.stderr

            # example.com has a title, so we MUST get SEO data
            assert seo_data is not None, "No SEO data extracted from file or stdout"

            # Verify we got some SEO data
            has_seo_data = any(
                key in seo_data
                for key in ["title", "description", "og:title", "canonical", "meta"]
            )
            assert has_seo_data, f"No SEO data extracted: {seo_data}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
