# ci-environment: provider-capture
"""Live acceptance through the author's public use-template flow."""

import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://www.canva.com/design/DAGudZAYlEE/VzrUrqpV2RhkVHCg43lvKQ/view?mode=preview&utm_campaign=designshare&utm_content=DAGudZAYlEE&utm_medium=link&utm_source=publishsharelink"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_canva.js"


def test_authenticated_template_copy(tmp_path, ensure_chrome_test_prereqs):
    auth = os.environ.get("AUTH_STORAGE_FILE")
    assert auth and Path(auth).is_file(), (
        "Set AUTH_STORAGE_FILE to the authorized Canva persona before running this explicit acceptance test"
    )
    with chrome_session(
        tmp_path,
        test_url=URL,
        timeout=90,
        env_overrides={"AUTH_STORAGE_FILE": auth, "CHROME_HEADLESS": "false"},
    ) as (_, _, chrome, env):
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
        output = chrome.parent / "canva"
        manifest = json.loads((output / "downloads.json").read_text())
        assert "Public Access - Certificate" in manifest["title"]
        assert {item["format"] for item in manifest["files"]} == {"pptx", "pdf"}
        for item in manifest["files"]:
            file = output / item["path"]
            data = file.read_bytes()
            assert len(data) == item["size"] > 0
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
            if item["format"] == "pptx":
                with zipfile.ZipFile(file) as archive:
                    assert archive.testzip() is None
                    slide = archive.read("ppt/slides/slide1.xml")
                    assert b"CERTIFICATE" in slide
                    assert b"APPRECIATION" in slide
                    assert (
                        len(
                            [
                                name
                                for name in archive.namelist()
                                if name.startswith("ppt/media/")
                            ],
                        )
                        > 0
                    )
            else:
                assert data.startswith(b"%PDF-") and b"%%EOF" in data[-100:]
                text = subprocess.run(
                    ["pdftotext", str(file), "-"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                # The curved CERTIFICATE heading consists of individually
                # positioned glyphs, so PDF reading order separates letters.
                assert "OF APPRECIATION" in text
                assert "This certificate is awarded to" in text
                assert "First Lastname" in text
