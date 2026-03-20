"""
Tests for the accessibility plugin.

Tests the real accessibility hook with an actual URL to verify
accessibility tree and page outline extraction.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    get_test_env,
    get_plugin_dir,
    get_hook_script,
)
from abx_plugins.plugins.base.test_utils import parse_jsonl_output


def chrome_available() -> bool:
    """Check if Chrome/Chromium is available."""
    for name in ["chromium", "chromium-browser", "google-chrome", "chrome"]:
        if shutil.which(name):
            return True
    return False


# Get the path to the accessibility hook
PLUGIN_DIR = get_plugin_dir(__file__)
ACCESSIBILITY_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_accessibility.*")


class TestAccessibilityPlugin:
    """Test the accessibility plugin."""

    def test_accessibility_hook_exists(self):
        """Accessibility hook script should exist."""
        assert ACCESSIBILITY_HOOK is not None, (
            "Accessibility hook not found in plugin directory"
        )
        assert ACCESSIBILITY_HOOK.exists(), f"Hook not found: {ACCESSIBILITY_HOOK}"


class TestAccessibilityWithChrome:
    """Integration tests for accessibility plugin with Chrome."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.snap_dir = self.temp_dir / "snap"
        self.snap_dir.mkdir(parents=True, exist_ok=True)

    def teardown_method(self, _method=None):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_accessibility_extracts_page_outline(self, chrome_test_url):
        """Accessibility hook should extract headings and accessibility tree."""
        test_url = chrome_test_url
        snapshot_id = "test-accessibility-snapshot"

        try:
            with chrome_session(
                self.temp_dir,
                crawl_id="test-accessibility-crawl",
                snapshot_id=snapshot_id,
                test_url=test_url,
                navigate=True,
                timeout=30,
            ) as (chrome_process, chrome_pid, snapshot_chrome_dir, env):
                # Use the environment from chrome_session (already has CHROME_HEADLESS=true)

                # Run accessibility hook with the active Chrome session
                result = subprocess.run(
                    [str(ACCESSIBILITY_HOOK),
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
                accessibility_output = (
                    Path(env["SNAP_DIR"]) / "accessibility" / "accessibility.json"
                )

                accessibility_data = None

                # Try parsing from file first
                if accessibility_output.exists():
                    with open(accessibility_output) as f:
                        try:
                            accessibility_data = json.load(f)
                        except json.JSONDecodeError:
                            pass

                # Verify hook ran successfully
                assert result.returncode == 0, f"Hook failed: {result.stderr}"
                assert "Traceback" not in result.stderr
                result_json = parse_jsonl_output(result.stdout)
                assert result_json is not None, f"Expected ArchiveResult JSONL. stdout: {result.stdout}"
                assert result_json["output_str"] == "accessibility/accessibility.json", result_json

                # example.com has headings, so we should get accessibility data
                assert accessibility_data is not None, (
                    "No accessibility data was generated"
                )

                # Verify we got page outline data
                assert "headings" in accessibility_data, (
                    f"Missing headings: {accessibility_data}"
                )
                assert "url" in accessibility_data, f"Missing url: {accessibility_data}"

        except RuntimeError:
            raise

    def test_accessibility_disabled_skips(self, chrome_test_url):
        """Test that ACCESSIBILITY_ENABLED=False skips without error."""
        test_url = chrome_test_url
        snapshot_id = "test-disabled"

        env = get_test_env() | {"SNAP_DIR": str(self.snap_dir)}
        env["ACCESSIBILITY_ENABLED"] = "False"

        result = subprocess.run(
            [str(ACCESSIBILITY_HOOK),
                f"--url={test_url}",
                f"--snapshot-id={snapshot_id}",
            ],
            cwd=str(self.temp_dir),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        # Should exit 0 even when disabled
        assert result.returncode == 0, f"Should succeed when disabled: {result.stderr}"

        # Should NOT create output file when disabled
        accessibility_output = self.snap_dir / "accessibility" / "accessibility.json"
        assert not accessibility_output.exists(), "Should not create file when disabled"

    def test_accessibility_missing_url_argument(self):
        """Test that missing --url argument causes error."""
        snapshot_id = "test-missing-url"

        result = subprocess.run(
            [str(ACCESSIBILITY_HOOK), f"--snapshot-id={snapshot_id}"],
            cwd=str(self.temp_dir),
            capture_output=True,
            text=True,
            timeout=30,
            env=get_test_env() | {"SNAP_DIR": str(self.snap_dir)},
        )

        # Should fail with non-zero exit code
        assert result.returncode != 0, "Should fail when URL missing"

    def test_accessibility_missing_snapshot_id_argument(self, chrome_test_url):
        """Test that missing --snapshot-id argument causes error."""
        test_url = chrome_test_url

        result = subprocess.run(
            [str(ACCESSIBILITY_HOOK), f"--url={test_url}"],
            cwd=str(self.temp_dir),
            capture_output=True,
            text=True,
            timeout=30,
            env=get_test_env() | {"SNAP_DIR": str(self.snap_dir)},
        )

        # Should fail with non-zero exit code
        assert result.returncode != 0, "Should fail when snapshot-id missing"

    def test_accessibility_with_no_chrome_session(self, chrome_test_url):
        """Test that hook fails gracefully when no Chrome session exists."""
        test_url = chrome_test_url
        snapshot_id = "test-no-chrome"

        result = subprocess.run(
            [str(ACCESSIBILITY_HOOK),
                f"--url={test_url}",
                f"--snapshot-id={snapshot_id}",
            ],
            cwd=str(self.temp_dir),
            capture_output=True,
            text=True,
            timeout=30,
            env=get_test_env(),
        )

        # Should fail when no Chrome session
        assert result.returncode != 0, "Should fail when no Chrome session exists"
        # Error should mention CDP or Chrome
        err_lower = result.stderr.lower()
        assert any(
            x in err_lower for x in ["chrome", "cdp", "cannot find", "puppeteer"]
        ), f"Should mention Chrome/CDP in error: {result.stderr}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
