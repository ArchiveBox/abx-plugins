"""
Tests for the responses plugin.

Tests the real responses hook with an actual URL to verify
network response capture.
"""

import json
import base64
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

    def test_same_second_responses_do_not_overwrite_each_other(self, tmp_path):
        result = subprocess.run(
            [
                os.environ["NODE_BINARY"],
                "-e",
                r"""
const {buildUniqueFilename} = require(process.argv[1]);
const fs = require('node:fs');
const path = require('node:path');
const names = ['first body', 'second body'].map(body => {
  const name = buildUniqueFilename({timestamp: '20261009T070000', method: 'GET',
    url: 'https://example.com/feed.json', extension: 'json'});
  fs.writeFileSync(path.join(process.argv[2], name), body);
  return name;
});
console.log(JSON.stringify(names));
""",
                str(FILENAME_UTILS),
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        names = json.loads(result.stdout)
        assert len(set(names)) == 2
        assert [(tmp_path / name).read_text() for name in names] == [
            "first body",
            "second body",
        ]

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

    @pytest.mark.parametrize("previous_exists", [False, True])
    def test_capture_replaces_both_existing_url_aliases(
        self,
        httpserver,
        previous_exists,
    ):
        body = (
            b"<html><body>Current response body, preserved byte for byte.</body></html>"
        )
        httpserver.expect_request("/article/index.html").respond_with_data(
            body,
            content_type="text/html",
        )
        url = httpserver.url_for("/article/index.html")
        with chrome_session(
            self.temp_dir,
            test_url=url,
            navigate=False,
            timeout=30,
        ) as (_, _, chrome_dir, env):
            output = chrome_dir.parent / "responses"
            output.mkdir()
            previous = output / "previous.html"
            if previous_exists:
                previous.write_bytes(b"Previous response must remain intact.")
            hostname = urlparse(url).hostname
            assert hostname
            aliases = [
                output / prefix / hostname / "article/index.html"
                for prefix in ("document", "")
            ]
            for alias in aliases:
                alias.parent.mkdir(parents=True, exist_ok=True)
                alias.symlink_to(os.path.relpath(previous, alias.parent))
            index = output / "index.jsonl"
            process = start_process_and_wait_for_file(
                [str(RESPONSES_HOOK), f"--url={url}"],
                index,
                cwd=output,
                env=env,
            )
            try:
                navigation = subprocess.run(
                    [str(CHROME_NAVIGATE_HOOK), f"--url={url}"],
                    cwd=chrome_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                assert navigation.returncode == 0, navigation.stderr
                wait_for_file(
                    index,
                    process=process,
                    ready=lambda p: url in p.read_text(),
                )
            finally:
                process.terminate()
                stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, (stdout, stderr)
            records = [json.loads(line) for line in index.read_text().splitlines()]
            current = next(record for record in records if record["url"] == url)
            saved = output / current["path"]
            assert saved.read_bytes() == body
            for alias in aliases:
                assert alias.is_symlink()
                assert alias.resolve(strict=True) == saved.resolve(strict=True)
                assert alias.read_bytes() == body
                assert set(alias.parent.iterdir()) == {alias}
            if previous_exists:
                assert previous.read_bytes() == b"Previous response must remain intact."

    def test_data_url_body_is_saved_without_url_shaped_directories(self, httpserver):
        """Inline image bytes must survive without creating oversized B2 keys."""
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2">'
            "<metadata>" + "inline-image " * 200 + "</metadata>"
            '<rect width="2" height="2" fill="red"/></svg>'
        ).encode()
        data_url = "data:image/svg+xml;base64," + base64.b64encode(svg).decode()
        httpserver.expect_request("/inline-image").respond_with_data(
            f'<html><body><img src="{data_url}"></body></html>',
            content_type="text/html",
        )
        url = httpserver.url_for("/inline-image")
        with chrome_session(
            self.temp_dir,
            test_url=url,
            navigate=False,
            timeout=30,
        ) as (_, _, chrome_dir, env):
            output = chrome_dir.parent / "responses"
            output.mkdir()
            index = output / "index.jsonl"
            process = start_process_and_wait_for_file(
                [str(RESPONSES_HOOK), f"--url={url}"],
                index,
                cwd=output,
                env=env,
            )
            try:
                navigation = subprocess.run(
                    [str(CHROME_NAVIGATE_HOOK), f"--url={url}"],
                    cwd=chrome_dir,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=60,
                )
                assert navigation.returncode == 0, navigation.stderr
                wait_for_file(
                    index,
                    process=process,
                    ready=lambda p: data_url in p.read_text(),
                )
            finally:
                process.terminate()
                stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, (stdout, stderr)
            records = [json.loads(line) for line in index.read_text().splitlines()]
            image = next(r for r in records if r["url"] == data_url)
            assert (output / image["path"]).read_bytes() == svg
            assert not (output / "image").exists(), (
                "data: URLs have no hostname/path to mirror"
            )

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
                captured_files = list((responses_dir / "all").iterdir())
                assert len(captured_files) == len(records), (
                    "Shutdown must drain every accepted response before emitting results"
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


@pytest.mark.parametrize(
    "broken_path",
    ["all", "index.jsonl", "document_alias", "site_alias"],
)
def test_response_storage_failure_is_not_success(tmp_path, httpserver, broken_path):
    """Real filesystem failures must not silently discard a completed response."""
    body = b"<!doctype html><title>ArchiveBox response storage</title><p>Preserve these bytes</p>"
    httpserver.expect_request("/document").respond_with_data(
        body,
        content_type="text/html",
    )
    url = httpserver.url_for("/document")
    hostname = urlparse(url).hostname
    assert hostname
    with chrome_session(tmp_path, test_url=url, navigate=False, timeout=30) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        output = chrome_dir.parent / "responses"
        output.mkdir()
        alias = output / hostname / "document"
        if broken_path == "document_alias":
            alias = output / "document" / hostname / "document"
        index = output / "index.jsonl"
        process = start_process_and_wait_for_file(
            [str(RESPONSES_HOOK), f"--url={url}"],
            index,
            cwd=output,
            env=env,
        )
        try:
            if broken_path == "all":
                (output / "all").rmdir()
                (output / "all").write_text("existing non-directory archive entry")
            elif broken_path == "index.jsonl":
                index.unlink()
                index.mkdir()
            else:
                alias.mkdir(parents=True)
                # A URL may legitimately have child resources. Obstruct its
                # index body too, so this remains an actual storage failure.
                (alias / "index.html").mkdir()
                (alias / "existing.txt").write_bytes(b"Preserve existing archive data")
            navigation = subprocess.run(
                [str(CHROME_NAVIGATE_HOOK), f"--url={url}"],
                cwd=chrome_dir,
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            assert navigation.returncode == 0, navigation.stderr
        finally:
            process.terminate()
            stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 1, (stdout, stderr)
        results = [
            json.loads(line) for line in stdout.splitlines() if line.startswith("{")
        ]
        record = next(row for row in results if row.get("type") == "ArchiveResult")
        assert record["status"] == "failed", record
        if broken_path.endswith("_alias"):
            assert "ERROR: Cannot save response alias:" in stderr, stderr
            assert str(alias) in stderr, stderr
        else:
            assert "ENOTDIR" in stderr or "EISDIR" in stderr, stderr
        if broken_path != "all":
            assert body in [path.read_bytes() for path in (output / "all").iterdir()]
        if broken_path.endswith("_alias"):
            assert (
                alias / "existing.txt"
            ).read_bytes() == b"Preserve existing archive data"
            entries = [json.loads(line) for line in index.read_text().splitlines()]
            entry = next(row for row in entries if row["url"] == url)
            assert (output / entry["path"]).read_bytes() == body
            other_prefix = "" if broken_path == "document_alias" else "document"
            other_alias = output / other_prefix / hostname / "document"
            assert other_alias.is_symlink()
            assert other_alias.read_bytes() == body


@pytest.mark.parametrize(
    "trailing_slash,child_first",
    [(True, False), (False, False), (False, True)],
)
def test_response_parent_and_child_urls_keep_both_bodies(
    tmp_path,
    httpserver,
    trailing_slash,
    child_first,
):
    import hashlib

    parent_path = "/article/" if trailing_slash else "/article"
    image = b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><rect width="2" height="2" fill="red"/></svg>'
    body = b'<!doctype html><title>Nested response paths</title><img src="/article/image.svg">'
    httpserver.expect_request(parent_path).respond_with_data(
        body,
        content_type="text/html",
    )
    httpserver.expect_request("/article/image.svg").respond_with_data(
        image,
        content_type="image/svg+xml",
    )
    if child_first:
        httpserver.expect_request("/start").respond_with_data(
            '<script>fetch("/article/image.svg").then(r=>r.text()).then(()=>fetch("/article"))</script>',
            content_type="text/html",
        )
    url = httpserver.url_for("/start" if child_first else parent_path)
    expected = {
        httpserver.url_for(parent_path): body,
        httpserver.url_for("/article/image.svg"): image,
    }
    with chrome_session(tmp_path, test_url=url, navigate=False, timeout=30) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        output = chrome_dir.parent / "responses"
        output.mkdir()
        index = output / "index.jsonl"
        process = start_process_and_wait_for_file(
            [str(RESPONSES_HOOK), f"--url={url}"],
            index,
            cwd=output,
            env=env,
        )
        try:
            navigation = subprocess.run(
                [str(CHROME_NAVIGATE_HOOK), f"--url={url}"],
                cwd=chrome_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert navigation.returncode == 0, navigation.stderr
            wait_for_file(
                index,
                process=process,
                ready=lambda p: (
                    set(expected)
                    <= {json.loads(line)["url"] for line in p.read_text().splitlines()}
                ),
            )
        finally:
            process.terminate()
            stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, (stdout, stderr)
        records = [json.loads(line) for line in index.read_text().splitlines()]
        for original_url, original_bytes in expected.items():
            record = next(row for row in records if row["url"] == original_url)
            assert (output / record["path"]).read_bytes() == original_bytes
            assert (
                record["responseSha256"] == hashlib.sha256(original_bytes).hexdigest()
            )
            suffix = (
                "article/index.html"
                if original_url.endswith(parent_path)
                else "article/image.svg"
            )
            for prefix in ("", record["resourceType"]):
                alias = output / prefix / urlparse(url).hostname / suffix
                if prefix and not trailing_slash and original_url.endswith(parent_path):
                    alias = alias.parent
                    if alias.is_dir():
                        alias = alias / "index.html"
                assert alias.is_symlink(), alias
                assert alias.read_bytes() == original_bytes
        results = [
            json.loads(line) for line in stdout.splitlines() if line.startswith("{")
        ]
        result = next(row for row in results if row.get("type") == "ArchiveResult")
        assert result["status"] == "succeeded"
        if not child_first:
            expected_main = output / urlparse(url).hostname / "article/index.html"
            assert (output.parent / result["output_str"]) == expected_main
            assert expected_main.read_bytes() == body
