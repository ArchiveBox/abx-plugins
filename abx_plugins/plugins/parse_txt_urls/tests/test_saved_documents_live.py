"""Live public Google exports through the actual Chrome and document hooks."""

import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session
from abx_plugins.plugins.parse_txt_urls.tests.test_saved_documents import run_parser

GOOGLEDOCS = (
    Path(__file__).resolve().parents[2] / "googledocs/on_Snapshot__53_googledocs.js"
)


@pytest.mark.parametrize(
    ("url", "format", "expected"),
    [
        (
            "https://docs.google.com/spreadsheets/d/13QQinPFhU9DwujctXS0A7un0up4N5BNyUDZwxK4I6hg/edit",
            "xlsx",
            "https://github.com/kmario23/deep-learning-drizzle",
        ),
        (
            "https://docs.google.com/document/d/1zvGN7hmeOVHOaQjCUr3XuNLZxZxvOn1u0GwVhR_ucB4/edit",
            "docx",
            "https://github.com/soyuka/signalhubws/blob/master/server.js",
        ),
    ],
)
def test_live_google_document_link_discovery(
    tmp_path,
    ensure_chrome_test_prereqs,
    url,
    format,
    expected,
):
    with chrome_session(tmp_path, test_url=url, timeout=60) as (_, _, chrome_dir, env):
        captured = subprocess.run(
            [str(GOOGLEDOCS), f"--url={url}"],
            cwd=chrome_dir.parent,
            env={**env, "GOOGLEDOCS_FORMATS": f'["{format}"]'},
            capture_output=True,
            text=True,
            timeout=150,
        )
        assert captured.returncode == 0, captured.stderr
        assert (chrome_dir.parent / f"googledocs/document.{format}").stat().st_size > 0
        # run_parser uses the same installed plugin runtime and saved snapshot.
        # No browser fetch or document conversion is needed for discovery.
        result, records = run_parser(chrome_dir.parent, url)
        urls = {record["url"] for record in records}
        assert expected in urls, result.stdout + result.stderr
        assert all(record["depth"] == 1 for record in records)
        assert not any("schemas.openxmlformats.org" in item for item in urls)
