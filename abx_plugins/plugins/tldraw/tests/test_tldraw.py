"""Download a published tldraw board through the real browser menu."""

import base64
import hashlib
import json
import subprocess
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://www.tldraw.com/r/learn_with_jason"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_tldraw.js"


def test_public_readonly_board(tmp_path, ensure_chrome_test_prereqs):
    assert HOOK.is_file(), "tldraw hook is missing"
    with chrome_session(tmp_path, test_url=URL, timeout=90) as (_, _, chrome, env):
        result = subprocess.run(
            [str(HOOK), f"--url={URL}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=150,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record is not None, result.stdout
        assert record["status"] == "succeeded", result.stdout
        output = chrome.parent / "tldraw"
        manifest = json.loads((output / "downloads.json").read_text())
        assert {item["format"] for item in manifest["files"]} >= {"tldr", "svg"}
        for item in manifest["files"]:
            data = (output / item["path"]).read_bytes()
            assert len(data) == item["size"] > 0
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
            if item["format"] == "tldr":
                board = json.loads(data)
                assert board["tldrawFileFormatVersion"] == 1
                assert board["schema"]["schemaVersion"] >= 1
                assert any(record["typeName"] == "page" for record in board["records"])
                assert (
                    sum(record["typeName"] == "shape" for record in board["records"])
                    > 10
                )
                assets = {
                    record["id"]: record
                    for record in board["records"]
                    if record["typeName"] == "asset"
                }
                images = [
                    record
                    for record in board["records"]
                    if record["typeName"] == "shape" and record.get("type") == "image"
                ]
                assert images, "The published board includes real image assets"
                for image in images:
                    source = assets[image["props"]["assetId"]]["props"]["src"]
                    assert source.startswith("data:image/png;base64,"), (
                        "Native image assets must remain usable offline"
                    )
                    assert base64.b64decode(source.partition(",")[2]).startswith(
                        b"\x89PNG\r\n\x1a\n",
                    )
            elif item["format"] == "svg":
                assert b"<svg" in data and b"<path" in data
