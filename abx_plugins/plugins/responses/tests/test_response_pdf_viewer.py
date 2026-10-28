"""Keep Chrome's PDF viewer out of captured original document responses."""

import json

import pytest

from abx_plugins.plugins.base.testing import start_process_and_wait_for_file
from abx_plugins.plugins.responses.tests.test_responses import RESPONSES_HOOK
from tests.test_archivewebpage_concurrent_collections import (
    MINIMAL_PDF,
    _run_start_hook,
    _run_navigate_hook,
    _run_stop_hook,
    launch_snapshot_tab,
)
from tests.test_archivewebpage_concurrent_collections import (
    archivewebpage_crawl as archivewebpage_crawl,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


@pytest.mark.parametrize("reload_as_html", [False, True])
def test_pdf_response_remains_original_after_recorder_export(
    tmp_path,
    httpserver_ipv4,
    archivewebpage_crawl,
    reload_as_html,
):
    env, _, tab_processes = archivewebpage_crawl
    httpserver_ipv4.expect_request("/document.pdf").respond_with_data(
        MINIMAL_PDF,
        content_type="application/pdf",
    )
    html = b"<!doctype html><h1>This document is now an HTML page</h1>"
    url = httpserver_ipv4.url_for("/document.pdf")
    snapshot_dir = tmp_path / "pdf-viewer"
    chrome_dir = snapshot_dir / "chrome"
    chrome_dir.mkdir(parents=True)
    env = env | {"SNAP_DIR": str(snapshot_dir)}
    tab_processes.append(
        launch_snapshot_tab(
            snapshot_chrome_dir=chrome_dir,
            tab_env=env,
            test_url=url,
            snapshot_id="pdf-viewer",
            crawl_id="test-archivewebpage-lifecycle",
        ),
    )
    start = _run_start_hook(snapshot_dir, env, url)
    assert start.returncode == 0, (start.stdout, start.stderr)
    output = snapshot_dir / "responses"
    output.mkdir()
    index = output / "index.jsonl"
    process = start_process_and_wait_for_file(
        [str(RESPONSES_HOOK), f"--url={url}"],
        index,
        cwd=output,
        env=env,
    )
    try:
        nav = _run_navigate_hook(snapshot_dir, env, url)
        assert nav.returncode == 0, nav.stderr
        stop = _run_stop_hook(snapshot_dir, env, url)
        assert stop.returncode == 0, (stop.stdout, stop.stderr)
        assert (snapshot_dir / "archivewebpage/archivewebpage.wacz").stat().st_size > 0
        if reload_as_html:
            httpserver_ipv4.clear()
            httpserver_ipv4.expect_request("/document.pdf").respond_with_data(
                html,
                content_type="text/html",
            )
            nav = _run_navigate_hook(snapshot_dir, env, url)
            assert nav.returncode == 0, nav.stderr
    finally:
        process.terminate()
        stdout, stderr = process.communicate(timeout=30)
        (tmp_path / "responses.stderr.log").write_text(stderr)
    assert process.returncode == 0, (stdout, stderr)
    records = [json.loads(line) for line in index.read_text().splitlines()]
    documents = [row for row in records if row["url"] == url]
    assert len(documents) == (2 if reload_as_html else 1), documents
    assert documents[0]["mimeType"] == "application/pdf"
    assert (output / documents[0]["path"]).read_bytes() == MINIMAL_PDF
    if reload_as_html:
        assert documents[1]["mimeType"] == "text/html"
        assert (output / documents[1]["path"]).read_bytes() == html
