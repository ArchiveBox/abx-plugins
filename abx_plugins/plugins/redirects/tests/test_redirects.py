"""
Tests for the redirects plugin.

Tests the real redirects hook with actual URLs to verify
redirect chain capture.
"""

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    CHROME_NAVIGATE_HOOK,
    get_plugin_dir,
    get_hook_script,
)


def chrome_available() -> bool:
    """Check if Chrome/Chromium is available."""
    for name in ["chromium", "chromium-browser", "google-chrome", "chrome"]:
        if shutil.which(name):
            return True
    return False


# Get the path to the redirects hook
PLUGIN_DIR = get_plugin_dir(__file__)
REDIRECTS_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_redirects.*")


class TestRedirectsPlugin:
    """Test the redirects plugin."""

    def test_redirects_hook_exists(self):
        """Redirects hook script should exist."""
        assert REDIRECTS_HOOK is not None, (
            "Redirects hook not found in plugin directory"
        )
        assert REDIRECTS_HOOK.exists(), f"Hook not found: {REDIRECTS_HOOK}"


class TestRedirectsWithChrome:
    """Integration tests for redirects plugin with Chrome."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self, _method=None):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_redirects_captures_navigation(self, chrome_test_urls):
        """Redirects hook should capture redirect-chain records from navigation."""
        test_url = chrome_test_urls["redirect_url"]
        snapshot_id = "test-redirects-snapshot"

        try:
            with chrome_session(
                self.temp_dir,
                crawl_id="test-redirects-crawl",
                snapshot_id=snapshot_id,
                test_url=test_url,
                navigate=False,
                timeout=30,
            ) as (chrome_process, chrome_pid, snapshot_chrome_dir, env):
                # Use the environment from chrome_session (already has CHROME_HEADLESS=true)

                # Run redirects hook with the active Chrome session (background hook)
                result = subprocess.Popen(
                    [
                        "node",
                        str(REDIRECTS_HOOK),
                        f"--url={test_url}",
                        f"--snapshot-id={snapshot_id}",
                    ],
                    cwd=str(snapshot_chrome_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )

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
                assert nav_result.returncode == 0, (
                    f"Navigation failed: {nav_result.stderr}\nStdout: {nav_result.stdout}"
                )

                # Check for output file
                snap_dir = Path(env["SNAP_DIR"])
                redirects_output = snap_dir / "redirects" / "redirects.jsonl"

                # Wait briefly for background hook to write output
                for _ in range(30):
                    if (
                        redirects_output.exists()
                        and redirects_output.stat().st_size > 0
                    ):
                        break
                    time.sleep(1)

                # Verify hook ran successfully
                if result.poll() is None:
                    result.terminate()
                    try:
                        stdout, stderr = result.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        result.kill()
                        stdout, stderr = result.communicate()
                else:
                    stdout, stderr = result.communicate()
                assert "Traceback" not in stderr
                assert "Error:" not in stderr

                assert redirects_output.exists(), (
                    f"redirects.jsonl not created in {redirects_output.parent}"
                )
                content = redirects_output.read_text().strip()
                assert content, "redirects.jsonl should not be empty"

                redirects_records = []
                for line in content.split("\n"):
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        redirects_records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

                assert redirects_records, "No redirect records captured"
                assert any(record.get("to_url") for record in redirects_records), (
                    f"Redirect records missing to_url: {redirects_records}"
                )
                assert any(
                    record.get("type") == "http"
                    and str(record.get("status")) in {"301", "302", "303", "307", "308"}
                    for record in redirects_records
                ), f"No HTTP redirect captured: {redirects_records}"

                archive_result = None
                for line in stdout.split("\n"):
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") == "ArchiveResult":
                        archive_result = record
                        break
                assert archive_result is not None, (
                    "Missing ArchiveResult from redirects hook"
                )
                assert archive_result.get("status") == "succeeded", (
                    f"Redirects hook did not report success: {archive_result}"
                )

        except RuntimeError:
            raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
