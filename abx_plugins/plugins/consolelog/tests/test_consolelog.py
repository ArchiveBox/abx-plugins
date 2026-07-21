"""
Tests for the consolelog plugin.

Tests the real consolelog hook with an actual URL to verify
console output capture.
"""

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import get_hook_script, get_plugin_dir
from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_NAVIGATE_HOOK,
    chrome_session,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


# Get the path to the consolelog hook
PLUGIN_DIR = get_plugin_dir(__file__)
CONSOLELOG_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_consolelog.*")


class TestConsolelogPlugin:
    """Test the consolelog plugin."""

    def test_consolelog_hook_exists(self):
        """Consolelog hook script should exist."""
        assert CONSOLELOG_HOOK is not None, (
            "Consolelog hook not found in plugin directory"
        )
        assert CONSOLELOG_HOOK.exists(), f"Hook not found: {CONSOLELOG_HOOK}"


class TestConsolelogWithChrome:
    """Integration tests for consolelog plugin with Chrome."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self, _method=None):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_consolelog_captures_output(self, httpserver):
        """Consolelog hook should capture console output from page."""
        httpserver.expect_request("/consolelog").respond_with_data(
            '<!doctype html><script>console.log("archivebox-console-test")</script>',
            content_type="text/html; charset=utf-8",
        )
        test_url = httpserver.url_for("/consolelog")
        snapshot_id = "test-consolelog-snapshot"

        with chrome_session(
            self.temp_dir,
            crawl_id="test-consolelog-crawl",
            snapshot_id=snapshot_id,
            test_url=test_url,
            navigate=False,
            timeout=30,
        ) as (chrome_process, chrome_pid, snapshot_chrome_dir, env):
            console_dir = snapshot_chrome_dir.parent / "consolelog"
            console_dir.mkdir(exist_ok=True)

            # Run consolelog hook with the active Chrome session (background hook)
            result = subprocess.Popen(
                [
                    str(CONSOLELOG_HOOK),
                    f"--url={test_url}",
                    f"--snapshot-id={snapshot_id}",
                ],
                cwd=str(console_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            console_output = console_dir / "console.jsonl"
            for _ in range(20):
                if console_output.exists():
                    break
                time.sleep(0.25)
            assert console_output.exists(), (
                "Consolelog hook did not become ready before navigation"
            )

            nav_result = subprocess.run(
                [
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

            # Allow it to run briefly, then terminate (background hook)
            for _ in range(10):
                if console_output.exists() and console_output.stat().st_size > 0:
                    break
                time.sleep(1)
            if result.poll() is None:
                result.terminate()
                try:
                    stdout, stderr = result.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    result.kill()
                    stdout, stderr = result.communicate()
            else:
                stdout, stderr = result.communicate()

            assert result.returncode == 0, (
                f"Consolelog hook did not shut down cleanly.\n"
                f"stdout: {stdout}\nstderr: {stderr}"
            )

            result_json = parse_jsonl_output(stdout)
            assert result_json is not None, (
                f"Consolelog hook should emit final ArchiveResult.\n"
                f"stdout: {stdout}\nstderr: {stderr}"
            )
            assert result_json["status"] == "succeeded", result_json
            assert result_json["output_str"].endswith("errors | 0 warnings"), (
                result_json
            )

            content = console_output.read_text().strip()
            assert content, "Console output should not be empty"
            records = [
                json.loads(line) for line in content.splitlines() if line.strip()
            ]
            assert records, "Console output should contain JSONL records"
            assert any(
                record.get("type") == "log"
                and "archivebox-console-test"
                in " ".join(map(str, record.get("args", [])))
                for record in records
            ), records
            for record in records:
                assert "timestamp" in record
                assert "type" in record


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
