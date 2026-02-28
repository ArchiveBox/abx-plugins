"""
Tests for the SSL plugin.

Tests the real SSL hook with an actual HTTPS URL to verify
certificate information extraction.
"""

import json
import os
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


# Get the path to the SSL hook
PLUGIN_DIR = get_plugin_dir(__file__)
SSL_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_ssl.*")


class TestSSLPlugin:
    """Test the SSL plugin with real HTTPS URLs."""

    def test_ssl_hook_exists(self):
        """SSL hook script should exist."""
        assert SSL_HOOK is not None, "SSL hook not found in plugin directory"
        assert SSL_HOOK.exists(), f"Hook not found: {SSL_HOOK}"


class TestSSLWithChrome:
    """Integration tests for SSL plugin with Chrome."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self, _method=None):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ssl_extracts_certificate_from_https_url(self, chrome_test_https_url):
        """SSL hook should extract certificate info from a real HTTPS URL."""
        test_url = chrome_test_https_url
        snapshot_id = "test-ssl-snapshot"

        old_ssl_setting = os.environ.get("CHROME_CHECK_SSL_VALIDITY")
        os.environ["CHROME_CHECK_SSL_VALIDITY"] = "false"
        try:
            with chrome_session(
                self.temp_dir,
                crawl_id="test-ssl-crawl",
                snapshot_id=snapshot_id,
                test_url=test_url,
                navigate=False,
                timeout=30,
            ) as (chrome_process, chrome_pid, snapshot_chrome_dir, env):
                ssl_dir = snapshot_chrome_dir.parent / "ssl"
                ssl_dir.mkdir(exist_ok=True)

                # Run SSL hook with the active Chrome session (background hook)
                result = subprocess.Popen(
                    [
                        "node",
                        str(SSL_HOOK),
                        f"--url={test_url}",
                        f"--snapshot-id={snapshot_id}",
                    ],
                    cwd=str(ssl_dir),
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
                    f"Navigation failed: {nav_result.stderr}"
                )

                # Check for output file
                ssl_output = ssl_dir / "ssl.jsonl"
                for _ in range(30):
                    if ssl_output.exists() and ssl_output.stat().st_size > 0:
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

                ssl_data = None

                # Try parsing from file first
                if ssl_output.exists():
                    with open(ssl_output) as f:
                        content = f.read().strip()
                        if content.startswith("{"):
                            try:
                                ssl_data = json.loads(content)
                            except json.JSONDecodeError:
                                pass

                # Try parsing from stdout if not in file
                if not ssl_data:
                    for line in stdout.split("\n"):
                        line = line.strip()
                        if line.startswith("{"):
                            try:
                                record = json.loads(line)
                                if (
                                    "protocol" in record
                                    or "issuer" in record
                                    or record.get("type") == "SSL"
                                ):
                                    ssl_data = record
                                    break
                            except json.JSONDecodeError:
                                continue

                # Verify hook ran successfully
                assert "Traceback" not in stderr
                assert "Error:" not in stderr

                # HTTPS fixture page must produce SSL metadata.
                assert ssl_data is not None, "No SSL data extracted from HTTPS URL"

                # Verify we got certificate info
                assert "protocol" in ssl_data, f"SSL data missing protocol: {ssl_data}"
                assert ssl_data["protocol"].startswith("TLS") or ssl_data[
                    "protocol"
                ].startswith("SSL"), f"Unexpected protocol: {ssl_data['protocol']}"
        finally:
            if old_ssl_setting is None:
                os.environ.pop("CHROME_CHECK_SSL_VALIDITY", None)
            else:
                os.environ["CHROME_CHECK_SSL_VALIDITY"] = old_ssl_setting


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
