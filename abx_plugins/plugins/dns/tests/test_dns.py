"""
Tests for the DNS plugin.

Tests the real DNS hook with an actual URL to verify
DNS resolution capture.
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


# Get the path to the DNS hook
PLUGIN_DIR = get_plugin_dir(__file__)
DNS_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_dns.*")
TEST_URL = "https://example.com"


class TestDNSPlugin:
    """Test the DNS plugin."""

    def test_dns_hook_exists(self):
        """DNS hook script should exist."""
        assert DNS_HOOK is not None, "DNS hook not found in plugin directory"
        assert DNS_HOOK.exists(), f"Hook not found: {DNS_HOOK}"


class TestDNSWithChrome:
    """Integration tests for DNS plugin with Chrome."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self, _method=None):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_dns_records_captured(self, require_chrome_runtime):
        """DNS hook should capture DNS records from a real URL."""
        test_url = TEST_URL
        snapshot_id = "test-dns-snapshot"

        with chrome_session(
            self.temp_dir,
            crawl_id="test-dns-crawl",
            snapshot_id=snapshot_id,
            test_url=test_url,
            navigate=False,
            timeout=30,
        ) as (_process, _pid, snapshot_chrome_dir, env):
            dns_dir = snapshot_chrome_dir.parent / "dns"
            dns_dir.mkdir(exist_ok=True)

            result = subprocess.Popen(
                [str(DNS_HOOK),
                    f"--url={test_url}",
                    f"--snapshot-id={snapshot_id}",
                ],
                cwd=str(dns_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            nav_result = subprocess.run(
                [str(CHROME_NAVIGATE_HOOK),
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

            dns_output = dns_dir / "dns.jsonl"
            for _ in range(30):
                if dns_output.exists() and dns_output.stat().st_size > 0:
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

            assert "Traceback" not in stderr

            assert dns_output.exists(), "dns.jsonl not created"
            content = dns_output.read_text().strip()
            assert content, f"DNS output unexpectedly empty for {test_url}"

            records = []
            for line in content.split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

            assert records, "No DNS records parsed"
            has_ip_record = any(r.get("hostname") and r.get("ip") for r in records)
            assert has_ip_record, f"No DNS record with hostname + ip: {records}"

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

            assert archive_result is not None, "Missing ArchiveResult from DNS hook"
            assert archive_result.get("status") == "succeeded"

            expected_ip = None
            for record in records:
                if record.get("hostname") == "example.com" and record.get("ip"):
                    expected_ip = record["ip"]
                    break
            expected_ip = expected_ip or next(
                (record.get("ip") for record in records if record.get("ip")),
                None,
            )
            assert archive_result.get("output_str") == expected_ip


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
