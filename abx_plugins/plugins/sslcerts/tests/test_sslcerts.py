"""
Tests for the SSL plugin.

Tests the real SSL hook with an actual HTTPS URL to verify
certificate information extraction.
"""

import json
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import (
    get_hook_script,
    get_plugin_dir,
    start_process_and_wait_for_file,
    wait_for_file,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_NAVIGATE_HOOK,
    chrome_session,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


# Get the path to the SSL hook
PLUGIN_DIR = get_plugin_dir(__file__)
SSLCERTS_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_sslcerts.*")


class TestSSLPlugin:
    """Test the SSL plugin with real HTTPS URLs."""

    def test_sslcerts_hook_exists(self):
        """SSL hook script should exist."""
        assert SSLCERTS_HOOK is not None, "SSL hook not found in plugin directory"
        assert SSLCERTS_HOOK.exists(), f"Hook not found: {SSLCERTS_HOOK}"


class TestSSLWithChrome:
    """Integration tests for SSL plugin with Chrome."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self, _method=None):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_failed_https_navigation_does_not_report_certificate_success(self):
        """A real failed Chrome navigation cannot produce a successful SSL result."""
        with socket.socket() as closed_socket:
            closed_socket.bind(("127.0.0.1", 0))
            closed_port = closed_socket.getsockname()[1]
        test_url = f"https://127.0.0.1:{closed_port}/"
        snapshot_id = "test-ssl-unreachable"

        with chrome_session(
            self.temp_dir,
            crawl_id="test-ssl-unreachable-crawl",
            snapshot_id=snapshot_id,
            test_url=test_url,
            navigate=False,
            timeout=30,
        ) as (_, _, snapshot_chrome_dir, env):
            ssl_dir = snapshot_chrome_dir.parent / "sslcerts"
            ssl_dir.mkdir(exist_ok=True)
            ssl_output = ssl_dir / "sslcerts.jsonl"
            listener = start_process_and_wait_for_file(
                [
                    str(SSLCERTS_HOOK),
                    f"--url={test_url}",
                    f"--snapshot-id={snapshot_id}",
                ],
                ssl_output,
                cwd=ssl_dir,
                env=env,
            )
            try:
                navigation = subprocess.run(
                    [
                        str(CHROME_NAVIGATE_HOOK),
                        f"--url={test_url}",
                        f"--snapshot-id={snapshot_id}",
                    ],
                    cwd=snapshot_chrome_dir,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=env,
                )
                assert navigation.returncode != 0
                navigation_state = json.loads(
                    (snapshot_chrome_dir / "navigation.json").read_text(),
                )
                assert "ERR_CONNECTION_REFUSED" in navigation_state["error"]
            finally:
                listener.terminate()
                stdout, stderr = listener.communicate(timeout=30)

            assert listener.returncode == 0, stderr
            assert ssl_output.read_text() == ""
            records = [
                json.loads(line) for line in stdout.splitlines() if line.startswith("{")
            ]
            assert len(records) == 1
            assert records[0]["type"] == "ArchiveResult"
            assert records[0]["status"] == "failed"
            assert "ERR_CONNECTION_REFUSED" in records[0]["output_str"]

    @pytest.mark.parametrize("use_public_url", [False, True])
    def test_ssl_extracts_certificate_from_https_url(
        self,
        chrome_test_https_url,
        use_public_url,
    ):
        """SSL hook should extract certificate info from a real HTTPS URL."""
        test_url = "https://example.com" if use_public_url else chrome_test_https_url
        snapshot_id = "test-ssl-snapshot"

        with chrome_session(
            self.temp_dir,
            crawl_id="test-ssl-crawl",
            snapshot_id=snapshot_id,
            test_url=test_url,
            navigate=False,
            timeout=30,
            env_overrides={"CHROME_CHECK_SSL_VALIDITY": "false"},
        ) as (chrome_process, chrome_pid, snapshot_chrome_dir, env):
            ssl_dir = snapshot_chrome_dir.parent / "sslcerts"
            ssl_dir.mkdir(exist_ok=True)

            # Run SSL hook with the active Chrome session (background hook)
            ssl_output = ssl_dir / "sslcerts.jsonl"
            result = start_process_and_wait_for_file(
                [
                    str(SSLCERTS_HOOK),
                    f"--url={test_url}",
                    f"--snapshot-id={snapshot_id}",
                ],
                ssl_output,
                cwd=ssl_dir,
                env=env,
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

            wait_for_file(
                ssl_output,
                process=result,
                ready=lambda path: path.exists() and path.stat().st_size > 0,
            )
            result.terminate()
            stdout, stderr = result.communicate(timeout=30)

            ssl_data = None

            # Try parsing from file first
            if ssl_output.exists():
                with open(ssl_output) as f:
                    content = f.read().strip()
                    if content.startswith("{"):
                        ssl_data = json.loads(content)

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

            if use_public_url:
                assert ssl_data["certificateChain"]
                for certificate in ssl_data["certificateChain"]:
                    fingerprint = certificate["fingerprint256"].replace(":", "").lower()
                    assert certificate["ctSearchUrl"] == (
                        f"https://ctlogs.dev/search?q={fingerprint}"
                    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
