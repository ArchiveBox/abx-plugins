"""Export an official public draw.io diagram using its real export menu."""

import hashlib
import json
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://app.diagrams.net/#Uhttps%3A%2F%2Fraw.githubusercontent.com%2Fjgraph%2Fdrawio-diagrams%2Fmaster%2Fdiagrams%2Fschema.xml"
EMPTY_URL = "https://app.diagrams.net/#Uhttps://raw.githubusercontent.com/jgraph/mxgraph/ff141aab158417bd866e2dfebd06c61d40773cd2/javascript/examples/editors/diagrams/empty.xml"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_drawio.js"


@pytest.mark.parametrize(
    "url",
    ["https://app.diagrams.net/#foo", "https://app.draw.io/#foo"],
)
def test_unrelated_editor_fragment(tmp_path, ensure_chrome_test_prereqs, url):
    with chrome_session(tmp_path, test_url=url, timeout=60) as (_, _, chrome, env):
        result = subprocess.run(
            [str(HOOK), f"--url={url}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "noresults", result.stdout
        assert (
            result.stderr.strip()
            == record["output_str"]
            == "Not a shared draw.io diagram"
        )
        assert not (chrome.parent / "drawio").exists()


@pytest.mark.parametrize("url", [URL, EMPTY_URL], ids=["schema", "empty-diagram"])
def test_public_schema_diagram(tmp_path, ensure_chrome_test_prereqs, url):
    assert HOOK.is_file(), "drawio hook is missing"
    with chrome_session(tmp_path, test_url=url, timeout=60) as (_, _, chrome, env):
        if url == EMPTY_URL:
            env = dict(env, DRAWIO_TIMEOUT="12")
        result = subprocess.run(
            [str(HOOK), f"--url={url}"],
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
            assert "<svg" in svg
            assert "mxfile" in svg, "SVG must include editable diagram data"
            embedded = ElementTree.fromstring(
                ElementTree.fromstring(data).attrib["content"]
            )
            models = list(embedded.iter("mxGraphModel"))
            assert len(models) == 1
            if url == EMPTY_URL:
                # Existing official source has only Workflow/Layer bookkeeping,
                # no drawing objects. Its native data must survive the export.
                assert any(
                    element.tag == "Workflow"
                    and element.attrib.get("label") == "MyWorkflow"
                    for element in models[0].iter()
                )
                assert any(
                    element.tag == "Layer"
                    and element.attrib.get("label") == "Default Layer"
                    for element in models[0].iter()
                )
                assert not any(
                    element.attrib.get("vertex") == "1"
                    or element.attrib.get("edge") == "1"
                    for element in models[0].iter("mxCell")
                )
            else:
                assert "UserRole" in svg and "AccountName" in svg
