"""The primary response must be a captured file, not a manifest-excluded alias."""

import json
import subprocess

import pytest

from abx_plugins.plugins.base.testing import (
    start_process_and_wait_for_file,
    wait_for_file,
)
from abx_plugins.plugins.responses.tests.test_responses import (
    CHROME_NAVIGATE_HOOK,
    RESPONSES_HOOK,
    chrome_session,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


@pytest.mark.parametrize("main_path", ["/article", "/article/index.html"])
def test_main_output_is_saved_document_with_larger_child_frame(
    tmp_path,
    httpserver,
    main_path,
):
    main = b'<!doctype html><title>Main article</title><body>Valuable article<iframe src="/widget"></iframe></body>'
    child = (
        b"<!doctype html><title>Embedded widget</title><body>"
        + b"Widget " * 1000
        + b"</body>"
    )
    httpserver.expect_request(main_path).respond_with_data(
        main,
        content_type="text/html",
    )
    httpserver.expect_request("/widget").respond_with_data(
        child,
        content_type="text/html",
    )
    url = httpserver.url_for(main_path)
    child_url = httpserver.url_for("/widget")
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
                ready=lambda p: child_url in p.read_text(),
            )
        finally:
            process.terminate()
            stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, (stdout, stderr)
        records = [json.loads(line) for line in index.read_text().splitlines()]
        main_record = next(row for row in records if row["url"] == url)
        child_record = next(row for row in records if row["url"] == child_url)
        assert (output / main_record["path"]).read_bytes() == main
        assert (output / child_record["path"]).read_bytes() == child
        results = [
            json.loads(line) for line in stdout.splitlines() if line.startswith("{")
        ]
        result = next(row for row in results if row.get("type") == "ArchiveResult")
        assert result["status"] == "succeeded"
        selected = output.parent / result["output_str"]
        assert not selected.is_symlink(), (
            "Output manifests deliberately exclude symlink aliases"
        )
        assert selected == output / main_record["path"]
        assert selected.read_bytes() == main
