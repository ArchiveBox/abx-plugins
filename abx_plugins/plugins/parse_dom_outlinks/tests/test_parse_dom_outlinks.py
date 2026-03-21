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

from abx_plugins.plugins.base.test_utils import get_hook_script, get_plugin_dir
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


def chrome_available() -> bool:
    """Check if Chrome/Chromium is available."""
    for name in ["chromium", "chromium-browser", "google-chrome", "chrome"]:
        if shutil.which(name):
            return True
    return False


# Get the path to the parse_dom_outlinks hook
PLUGIN_DIR = get_plugin_dir(__file__)
OUTLINKS_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_parse_dom_outlinks.*")


class TestParseDomOutlinksPlugin:
    """Test the parse_dom_outlinks plugin."""

    def test_outlinks_hook_exists(self):
        """DOM outlinks hook script should exist."""
        assert OUTLINKS_HOOK is not None, (
            "DOM outlinks hook not found in plugin directory"
        )
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
        snapshot_id = "test-outlinks-snapshot"

        try:
            with chrome_session(
                self.temp_dir,
                crawl_id="test-outlinks-crawl",
                snapshot_id=snapshot_id,
                test_url=test_url,
                navigate=True,
                timeout=30,
            ) as (chrome_process, chrome_pid, snapshot_chrome_dir, env):
                # Use the environment from chrome_session (already has CHROME_HEADLESS=true)

                # Run outlinks hook with the active Chrome session
                result = subprocess.run(
                    [
                        str(OUTLINKS_HOOK),
                        f"--url={test_url}",
                        f"--snapshot-id={snapshot_id}",
                    ],
                    cwd=str(snapshot_chrome_dir),
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
                )

                # Check for output file
                snap_dir = Path(env["SNAP_DIR"])
                urls_output = snap_dir / "parse_dom_outlinks" / "urls.jsonl"

                # Verify hook ran successfully
                assert result.returncode == 0, f"Hook failed: {result.stderr}"
                assert "Traceback" not in result.stderr

                archive_result = json.loads(result.stdout.strip().splitlines()[-1])
                assert archive_result["type"] == "ArchiveResult"
                assert archive_result["status"] == "succeeded"

                assert urls_output.exists(), "urls.jsonl not created"
                urls_data = [
                    json.loads(line)
                    for line in urls_output.read_text().splitlines()
                    if line.strip()
                ]
                assert urls_data, "urls.jsonl should contain at least one URL"
                assert all(entry["type"] == "Snapshot" for entry in urls_data)
                assert archive_result["output_str"] == f"{len(urls_data)} URLs parsed"

        except RuntimeError:
            raise

    def test_outlinks_removes_outputs_when_no_crawlable_urls(self):
        """Hook should not leave output files behind when no crawlable URLs are found."""
        input_file = self.temp_dir / "no-links.html"
        input_file.write_text(
            """<!doctype html>
<html>
<head><title>No Links</title></head>
<body><p>No crawlable links on this page.</p></body>
</html>
"""
        )
        test_url = input_file.resolve().as_uri()
        snapshot_id = "test-outlinks-empty"

        try:
            with chrome_session(
                self.temp_dir,
                crawl_id="test-outlinks-empty-crawl",
                snapshot_id=snapshot_id,
                test_url=test_url,
                navigate=True,
                timeout=30,
            ) as (_chrome_process, _chrome_pid, snapshot_chrome_dir, env):
                result = subprocess.run(
                    [
                        str(OUTLINKS_HOOK),
                        f"--url={test_url}",
                        f"--snapshot-id={snapshot_id}",
                    ],
                    cwd=str(snapshot_chrome_dir),
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
                )

                assert result.returncode == 0, f"Hook failed: {result.stderr}"

                archive_result = json.loads(result.stdout.strip().splitlines()[-1])
                assert archive_result["type"] == "ArchiveResult"
                assert archive_result["status"] == "noresults"
                assert archive_result["output_str"] == "0 URLs parsed"

                snap_dir = Path(env["SNAP_DIR"])
                outlinks_dir = snap_dir / "parse_dom_outlinks"
                assert not (outlinks_dir / "urls.jsonl").exists()

        except RuntimeError:
            raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
