"""Export an official public draw.io diagram using its real export menu."""

import hashlib
import json
import subprocess
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://app.diagrams.net/#Uhttps%3A%2F%2Fraw.githubusercontent.com%2Fjgraph%2Fdrawio-diagrams%2Fmaster%2Fdiagrams%2Fschema.xml"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_drawio.js"


def test_public_schema_diagram(tmp_path, ensure_chrome_test_prereqs):
    assert HOOK.is_file(), "drawio hook is missing"
    with chrome_session(tmp_path, test_url=URL, timeout=60) as (_, _, chrome, env):
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
        assert record and record["status"] == "succeeded", result.stdout
        output = chrome.parent / "drawio"
        manifest = json.loads((output / "downloads.json").read_text())
        assert {item["format"] for item in manifest["files"]} == {"svg"}
        for item in manifest["files"]:
            data = (output / item["path"]).read_bytes()
            assert len(data) == item["size"] > 0
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
            svg = data.decode()
            assert "<svg" in svg and "UserRole" in svg and "AccountName" in svg
            assert "mxfile" in svg, "SVG must include editable diagram data"
