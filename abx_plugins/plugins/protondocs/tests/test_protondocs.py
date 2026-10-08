"""Export a publicly published document using the real Proton Docs menu."""

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://docs.proton.me/doc?mode=open-url&token=80ACJEV9WR#bjlO8WmpUzTA"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_protondocs.js"


def test_public_document_exports(tmp_path, ensure_chrome_test_prereqs):
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
        output = chrome.parent / "protondocs"
        manifest = json.loads((output / "downloads.json").read_text())
        files = {item["format"]: output / item["path"] for item in manifest["files"]}
        assert set(files) == {"md", "html", "docx"}
        for item in manifest["files"]:
            data = (output / item["path"]).read_bytes()
            assert len(data) == item["size"] > 1000
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
        for extension in ("md", "html"):
            text = files[extension].read_text()
            assert (
                "Artists Demand End to Aichi-Israel Relations during Aichi Triennale!"
                in text
            )
            assert "Historical Precedent for Artist Resistance" in text
            assert "地域社会の反応" in text
        with zipfile.ZipFile(files["docx"]) as document:
            xml = document.read("word/document.xml").decode()
            assert "Historical Precedent for Artist Resistance" in xml
            assert "地域社会の反応" in xml
