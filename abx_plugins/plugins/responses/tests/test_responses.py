"""
Tests for the responses plugin.

Tests the real responses hook with an actual URL to verify
network response capture.
"""

import json
import posixpath
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlparse
from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import get_hook_script, get_plugin_dir
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_NAVIGATE_HOOK,
    chrome_session,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")
REPO_ROOT = Path(__file__).resolve().parents[4]


# Get the path to the responses hook
PLUGIN_DIR = get_plugin_dir(__file__)
RESPONSES_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_responses.*")
FILENAME_UTILS = PLUGIN_DIR / "filename_utils.js"


class TestResponsesPlugin:
    """Test the responses plugin."""

    def test_responses_hook_exists(self):
        """Responses hook script should exist."""
        assert RESPONSES_HOOK is not None, (
            "Responses hook not found in plugin directory"
        )
        assert RESPONSES_HOOK.exists(), f"Hook not found: {RESPONSES_HOOK}"

    def test_unique_filename_does_not_duplicate_existing_suffix(self):
        """Short encoded URLs should not get .ext appended twice."""
        node_binary = shutil.which("node")
        assert node_binary, "Node.js is required for JS filename helper tests"

        result = subprocess.run(
            [
                node_binary,
                "-e",
                (
                    "const { buildUniqueFilename } = require("
                    f"{json.dumps(str(FILENAME_UTILS))}"
                    ");"
                    "const names = ["
                    "buildUniqueFilename({"
                    "timestamp: '20260322T225802',"
                    "method: 'GET',"
                    "url: 'https://imgur.zervice.io/DP391ax.png',"
                    "extension: 'png',"
                    "}),"
                    "buildUniqueFilename({"
                    "timestamp: '20260322T225802',"
                    "method: 'GET',"
                    "url: 'https://docs.sweeting.me/uploads/e6eb92a9-7c41-42b9-a3b4-example.png',"
                    "extension: 'png',"
                    "})"
                    "];"
                    "console.log(JSON.stringify(names));"
                ),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )

        unique_names = json.loads(result.stdout)
        assert unique_names[0].endswith("https_3A_2F_2Fimgur.zervice.io_2FDP391ax.png")
        assert not unique_names[0].endswith(".png.png")
        assert unique_names[1].endswith(".png")
        assert ".png.png" not in unique_names[1]


class TestResponsesWithChrome:
    """Integration tests for responses plugin with Chrome."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self, _method=None):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_responses_captures_network_responses(self, chrome_test_url):
        """Responses hook should capture network responses from page load."""
        test_url = chrome_test_url
        snapshot_id = "test-responses-snapshot"

        with chrome_session(
            self.temp_dir,
            crawl_id="test-responses-crawl",
            snapshot_id=snapshot_id,
            test_url=test_url,
            navigate=False,
            timeout=30,
        ) as (chrome_process, chrome_pid, snapshot_chrome_dir, env):
            responses_dir = snapshot_chrome_dir.parent / "responses"
            responses_dir.mkdir(exist_ok=True)
            index_output = responses_dir / "index.jsonl"

            # Run responses hook with the active Chrome session (background hook)
            result = subprocess.Popen(
                [
                    str(RESPONSES_HOOK),
                    f"--url={test_url}",
                    f"--snapshot-id={snapshot_id}",
                ],
                cwd=str(responses_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            for _ in range(30):
                if index_output.exists():
                    break
                time.sleep(1)
            assert index_output.exists(), "Responses hook did not signal readiness"

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

            # Wait briefly for background hook to write output
            for _ in range(30):
                if index_output.exists() and index_output.stat().st_size > 0:
                    break
                time.sleep(1)

            # Verify hook ran (may keep running waiting for cleanup signal)
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

            archive_result_records = []
            for line in stdout.splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "ArchiveResult":
                    archive_result_records.append(record)

            # If index file exists, verify it's valid JSONL
            if index_output.exists():
                records = []
                with open(index_output) as f:
                    content = f.read().strip()
                    assert content, "Responses output should not be empty"
                    for line in content.split("\n"):
                        if line.strip():
                            try:
                                record = json.loads(line)
                                # Verify structure
                                assert "url" in record
                                assert "resourceType" in record
                                records.append(record)
                            except json.JSONDecodeError:
                                pass  # Some lines may be incomplete

                assert records, (
                    "Responses output should include at least one valid record"
                )

                symlink_record = next(
                    (record for record in records if urlparse(record["url"]).hostname),
                    None,
                )
                assert symlink_record is not None, (
                    "Expected at least one URL-addressable response record for symlink checks"
                )

                parsed_url = urlparse(symlink_record["url"])
                pathname = parsed_url.path or "/"
                filename = posixpath.basename(pathname) or (
                    "index"
                    + (
                        f".{symlink_record['extension']}"
                        if symlink_record.get("extension")
                        else ""
                    )
                )
                dir_path_raw = posixpath.dirname(pathname)
                dir_path = "" if dir_path_raw == "." else dir_path_raw.lstrip("/")

                unique_path = responses_dir / Path(symlink_record["path"])
                typed_symlink = (
                    responses_dir
                    / symlink_record["resourceType"]
                    / parsed_url.hostname
                    / dir_path
                    / filename
                )
                site_symlink = responses_dir / parsed_url.hostname / dir_path / filename

                assert unique_path.exists(), (
                    f"Expected recorded response artifact to exist: {unique_path}"
                )
                assert typed_symlink.is_symlink(), (
                    f"Expected resource-type symlink to exist: {typed_symlink}"
                )
                assert site_symlink.is_symlink(), (
                    f"Expected site-style symlink to exist: {site_symlink}"
                )
                assert typed_symlink.resolve(strict=True) == unique_path.resolve(
                    strict=True,
                )
                assert site_symlink.resolve(strict=True) == unique_path.resolve(
                    strict=True,
                )

                if archive_result_records:
                    archive_result = archive_result_records[-1]
                    parsed_main_url = urlparse(test_url)
                    main_pathname = parsed_main_url.path or "/"
                    main_filename = posixpath.basename(main_pathname) or "index.html"
                    main_dir_raw = posixpath.dirname(main_pathname)
                    main_dir = "" if main_dir_raw == "." else main_dir_raw.lstrip("/")
                    expected_output = posixpath.join(
                        "responses",
                        parsed_main_url.hostname or "",
                        main_dir,
                        main_filename,
                    )
                    assert archive_result["output_str"] == expected_output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
