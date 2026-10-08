# ci-environment: provider-capture
"""Explicit live acceptance: requires an authorized AUTH_STORAGE_FILE persona."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://miro.com/app/board/uXjVK4c-_uU=/"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_miro.js"


def test_authenticated_public_board_pdf(tmp_path, ensure_chrome_test_prereqs):
    auth = os.environ.get("AUTH_STORAGE_FILE")
    assert auth and Path(auth).is_file(), (
        "Set AUTH_STORAGE_FILE to an authorized Miro persona before running this explicit acceptance test"
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
        output = chrome.parent / "miro"
        manifest = json.loads((output / "downloads.json").read_text())
        assert "Master template for student activity frame" in manifest["title"]
        pdfs = [item for item in manifest["files"] if item["format"] == "pdf"]
        assert len(pdfs) == 1
        for item in manifest["files"]:
            data = (output / item["path"]).read_bytes()
            assert len(data) == item["size"] > 0
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
        pdf = (output / pdfs[0]["path"]).read_bytes()
        assert pdf.startswith(b"%PDF-") and b"%%EOF" in pdf[-100:]
        assert b"/Subtype /Image" in pdf or b"/Subtype/Image" in pdf
        assert b"/Type /Page" in pdf or b"/Type/Page" in pdf
        image_prefix = tmp_path / "exported-frame"
        subprocess.run(
            ["pdfimages", "-j", str(output / pdfs[0]["path"]), str(image_prefix)],
            check=True,
        )
        image = (tmp_path / "exported-frame-000.jpg").read_bytes()
        # Visually verified actual public frame: a yellow sticky with 'Hello'.
        # Hash only the exported image, excluding the PDF creation timestamp.
        assert (
            hashlib.sha256(image).hexdigest()
            == "95eb1816613a615be25fdb47a9c3bd2c1300c278824997b76bfe825d4e5db01e"
        )
