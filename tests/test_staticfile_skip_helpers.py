from __future__ import annotations

import json
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import install_binary_with_abxpkg
from abx_plugins.plugins.base.utils import (
    has_staticfile_output,
    iter_staticfile_text_inputs,
)
from abx_plugins.plugins.parse_rss_urls.parse_rss_test_helpers import parse_rss_urls_env


REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_UTILS_JS = REPO_ROOT / "abx_plugins" / "plugins" / "base" / "utils.js"

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


@pytest.mark.parametrize(
    ("plugin", "filename", "content_type", "content"),
    [
        (
            "parse_jsonl_urls",
            "bookmarks.json",
            "application/json",
            json.dumps(
                {
                    "url": "https://example.com/captured",
                    "title": "A\u2028B\u2029C\u0085D",
                },
                ensure_ascii=False,
            )
            + "\n",
        ),
        (
            "parse_rss_urls",
            "feed.xml",
            "application/xml",
            '<rss version="2.0"><channel><title>Captured feed</title>'
            "<item><title>Saved article</title>"
            "<link>https://example.com/captured</link></item></channel></rss>",
        ),
    ],
)
def test_parser_reuses_downloaded_staticfile_when_origin_is_offline(
    tmp_path,
    httpserver,
    real_staticfile_output,
    plugin,
    filename,
    content_type,
    content,
):
    httpserver.expect_request(f"/{filename}").respond_with_data(
        content,
        content_type=content_type,
    )
    url = httpserver.url_for(f"/{filename}")
    snapshot_dir = real_staticfile_output(tmp_path, url, f"saved-{plugin}")
    capture_records = [
        json.loads(line)
        for line in (snapshot_dir / "staticfile" / "stdout.log").read_text().split("\n")
        if line.startswith("{")
    ]
    capture = next(
        record for record in capture_records if record["type"] == "ArchiveResult"
    )
    assert capture["status"] == "succeeded"
    saved_source = snapshot_dir / capture["output_str"]
    assert saved_source.read_text() == content
    captured_body = saved_source.resolve()
    assert iter_staticfile_text_inputs(snapshot_dir) == (captured_body,)
    # Portable artifact metadata must not turn a symlink into a source outside
    # the snapshot, even when that file otherwise has an allowed content type.
    outside_source = tmp_path / filename
    captured_body.rename(outside_source)
    captured_body.symlink_to(outside_source)
    assert iter_staticfile_text_inputs(snapshot_dir) == ()
    captured_body.unlink()
    outside_source.rename(captured_body)
    hook = next((REPO_ROOT / "abx_plugins" / "plugins" / plugin).glob("on_Snapshot__*"))
    env = {**os.environ, "SNAP_DIR": str(snapshot_dir)}
    if plugin == "parse_rss_urls":
        env = parse_rss_urls_env(env)
    httpserver.stop()
    try:
        result = subprocess.run(
            [str(hook), f"--url={url}"],
            cwd=snapshot_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        httpserver.start()
    assert result.returncode == 0, result.stderr
    records = [
        json.loads(line) for line in result.stdout.split("\n") if line.startswith("{")
    ]
    links = [record for record in records if record["type"] == "Snapshot"]
    assert [record["url"] for record in links] == ["https://example.com/captured"]
    if plugin == "parse_jsonl_urls":
        assert links[0]["title"] == "A\u2028B\u2029C\u0085D"
    assert (
        next(record for record in records if record["type"] == "ArchiveResult")[
            "status"
        ]
        == "succeeded"
    )
    stored = snapshot_dir / plugin / "urls.jsonl"
    assert [
        json.loads(line)["url"] for line in stored.read_text().split("\n") if line
    ] == ["https://example.com/captured"]
    assert saved_source.read_text() == content


@pytest.mark.parametrize(
    "plugin",
    ["parse_rss_urls", "parse_netscape_urls", "parse_jsonl_urls"],
)
def test_parser_reuses_raw_main_html_when_origin_is_offline(
    tmp_path,
    httpserver,
    real_staticfile_output,
    plugin,
):
    content = (
        "<!DOCTYPE NETSCAPE-Bookmark-file-1><html><body><DL><DT>"
        '<A HREF="https://example.com/saved-bookmark">Saved bookmark</A>'
        '</DL><iframe src="/other.html"></iframe></body></html>'
    )
    httpserver.expect_request(
        "/bookmarks.html",
        query_string="export=1",
    ).respond_with_data(
        content,
        content_type="text/html",
    )
    httpserver.expect_request("/other.html").respond_with_data(
        "<html><body>Unrelated iframe</body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/redirect").respond_with_data(
        "",
        status=302,
        headers={"Location": "/bookmarks.html?export=1"},
    )
    url = httpserver.url_for("/redirect")
    snapshot_dir = real_staticfile_output(tmp_path, url, f"raw-{plugin}")
    navigation = json.loads((snapshot_dir / "chrome/navigation.json").read_text())
    assert navigation["status"] == 200
    assert navigation["finalUrl"] == httpserver.url_for("/bookmarks.html?export=1")
    response_entries = [
        json.loads(line)
        for line in (snapshot_dir / "responses/index.jsonl").read_text().split("\n")
        if line
    ]
    main_entry = next(
        entry for entry in response_entries if entry["url"] == navigation["finalUrl"]
    )
    captured_body = (snapshot_dir / "responses" / main_entry["path"]).resolve()
    assert captured_body.read_text() == content
    assert (
        hashlib.sha256(captured_body.read_bytes()).hexdigest()
        == main_entry["responseSha256"]
    )
    # Normal captures use per-attempt process logs, not this legacy filename.
    (snapshot_dir / "staticfile/stdout.log").unlink()
    hook = next((REPO_ROOT / "abx_plugins" / "plugins" / plugin).glob("on_Snapshot__*"))
    env = {**os.environ, "SNAP_DIR": str(snapshot_dir)}
    if plugin == "parse_rss_urls":
        env = parse_rss_urls_env(env)
    httpserver.stop()
    try:
        result = subprocess.run(
            [str(hook), f"--url={url}"],
            cwd=snapshot_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        httpserver.start()
    assert result.returncode == 0, result.stderr
    records = [
        json.loads(line) for line in result.stdout.split("\n") if line.startswith("{")
    ]
    archive_result = next(
        record for record in records if record["type"] == "ArchiveResult"
    )
    links = [record for record in records if record["type"] == "Snapshot"]
    if plugin == "parse_netscape_urls":
        assert archive_result["status"] == "succeeded", result.stderr
        assert [record["url"] for record in links] == [
            "https://example.com/saved-bookmark",
        ]
        assert links[0]["title"] == "Saved bookmark"
    else:
        assert archive_result["status"] == "noresults", result.stderr
        assert links == []
    assert iter_staticfile_text_inputs(snapshot_dir) == (captured_body,)
    assert captured_body.read_text() == content
    # Optional metadata cannot authorize an unrelated or out-of-snapshot body.
    navigation_path = snapshot_dir / "chrome/navigation.json"
    navigation_text = navigation_path.read_text()
    navigation_path.write_text("{}")
    assert iter_staticfile_text_inputs(snapshot_dir) == ()
    navigation_path.write_text(navigation_text)
    outside_source = tmp_path / "outside.html"
    captured_body.rename(outside_source)
    captured_body.symlink_to(outside_source)
    assert iter_staticfile_text_inputs(snapshot_dir) == ()
    captured_body.unlink()
    outside_source.rename(captured_body)


def _js_has_staticfile_output(staticfile_dir: Path) -> bool:
    node = install_binary_with_abxpkg("node", binproviders="env,node,brew,apt")
    assert node.loaded_abspath is not None
    result = subprocess.run(
        [
            str(node.loaded_abspath),
            "-e",
            (
                "const { hasStaticFileOutput } = require("
                f"{json.dumps(str(BASE_UTILS_JS))}"
                ");"
                "console.log(hasStaticFileOutput(process.argv[1]) ? 'true' : 'false');"
            ),
            str(staticfile_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip() == "true"


def test_staticfile_output_helpers_accept_real_static_artifact(
    tmp_path: Path,
    real_staticfile_output,
    local_staticfile_urls,
) -> None:
    snapshot_dir = real_staticfile_output(
        tmp_path,
        local_staticfile_urls["json"],
        "static-artifact",
    )
    staticfile_dir = snapshot_dir / "staticfile"

    assert has_staticfile_output(str(staticfile_dir)) is True
    assert _js_has_staticfile_output(staticfile_dir) is True


def test_staticfile_output_helpers_reject_real_html_noresults(
    tmp_path: Path,
    real_staticfile_output,
    local_staticfile_urls,
) -> None:
    snapshot_dir = real_staticfile_output(
        tmp_path,
        local_staticfile_urls["html"],
        "html-noresults",
    )
    staticfile_dir = snapshot_dir / "staticfile"

    assert has_staticfile_output(str(staticfile_dir)) is False
    assert _js_has_staticfile_output(staticfile_dir) is False
