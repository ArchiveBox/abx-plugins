"""Source parsers use real capture metadata without refetching binary bodies."""

import json
import shutil
import os
import subprocess
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from abx_plugins.plugins.base.utils import read_snapshot_source_text
from abx_plugins.plugins.parse_rss_urls.parse_rss_test_helpers import parse_rss_urls_env


@pytest.mark.parametrize("filename", ["source.png", "source.json"])
def test_parser_uses_portable_staticfile_result(
    tmp_path,
    real_staticfile_output,
    filename,
    capsys,
):
    origin = tmp_path / "origin"
    origin.mkdir()
    text = '{"url":"https://archivebox.io/tests/"}\n'
    if filename.endswith(".png"):
        shutil.copyfile(
            Path(__file__).parents[1] / "docs/assets/icon.png",
            origin / filename,
        )
    else:
        (origin / filename).write_text(text)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(SimpleHTTPRequestHandler, directory=str(origin)),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/{filename}"
    try:
        snapshot = real_staticfile_output(
            tmp_path / "capture",
            url,
            "portable-source",
            start_responses=False,
        )
        if filename.endswith(".png"):
            capsys.readouterr()
            for plugin in (
                "parse_html_urls",
                "parse_jsonl_urls",
                "parse_rss_urls",
                "parse_netscape_urls",
            ):
                hook = next(
                    (Path(__file__).parents[1] / "abx_plugins/plugins" / plugin).glob(
                        "on_Snapshot__*",
                    ),
                )
                env = {**os.environ, "SNAP_DIR": str(snapshot)}
                if plugin == "parse_rss_urls":
                    env = parse_rss_urls_env(env)
                output = subprocess.run(
                    [str(hook), f"--url={url}"],
                    cwd=snapshot,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert output.returncode == 0, output.stderr
                records = [
                    json.loads(line)
                    for line in output.stdout.splitlines()
                    if line.startswith("{")
                ]
                assert not [row for row in records if row.get("type") == "Snapshot"]
                assert (
                    next(row for row in records if row.get("type") == "ArchiveResult")[
                        "status"
                    ]
                    == "noresults"
                )
            assert '"GET /source.png ' not in capsys.readouterr().err
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    result = json.loads((snapshot / "staticfile/result.json").read_text())
    saved = snapshot / result["output_str"]
    assert saved.read_bytes() == (origin / filename).read_bytes()
    # Current executors keep attempt logs locally; result.json is the portable API.
    (snapshot / "staticfile/stdout.log").unlink()
    assert read_snapshot_source_text(
        url,
        snap_dir=snapshot,
        timeout=2,
        user_agent="ArchiveBox test",
    ) == ("" if filename.endswith(".png") else text)
    assert saved.read_bytes() == (origin / filename).read_bytes()
