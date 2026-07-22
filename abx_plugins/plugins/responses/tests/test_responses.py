"""
Tests for the responses plugin.

Tests the real responses hook with an actual URL to verify
network response capture.
"""

import json
import os
import posixpath
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse
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
        node_binary = os.environ.get("NODE_BINARY")
        assert node_binary and Path(node_binary).is_file(), (
            "NODE_BINARY was not resolved by abxpkg"
        )

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
            result = start_process_and_wait_for_file(
                [
                    str(RESPONSES_HOOK),
                    f"--url={test_url}",
                    f"--snapshot-id={snapshot_id}",
                ],
                index_output,
                cwd=responses_dir,
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
                index_output,
                process=result,
                ready=lambda path: path.stat().st_size > 0,
            )
            result.terminate()
            stdout, stderr = result.communicate(timeout=30)
            assert "Traceback" not in stderr

            archive_result_records = []
            for line in stdout.splitlines():
                if not line.strip():
                    continue
                if not line.strip().startswith("{"):
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    raise AssertionError(
                        f"Malformed JSONL record in responses stdout: {line}",
                    )
                if record.get("type") == "ArchiveResult":
                    archive_result_records.append(record)

            # Verify the readiness file contains real captured response records.
            assert index_output.exists()
            if index_output.exists():
                records = []
                with open(index_output) as f:
                    content = f.read().strip()
                    assert content, "Responses output should not be empty"
                    for line in content.split("\n"):
                        if line.strip():
                            record = json.loads(line)
                            assert "url" in record
                            assert "resourceType" in record
                            records.append(record)

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

                assert archive_result_records, (
                    f"Missing ArchiveResult from responses hook stdout: {stdout}"
                )
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
                assert archive_result == {
                    "type": "ArchiveResult",
                    "status": "succeeded",
                    "output_str": expected_output,
                }, archive_result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
