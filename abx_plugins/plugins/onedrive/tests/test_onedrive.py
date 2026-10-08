"""Capture a genuine public OneDrive church bulletin through real Chrome."""

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://1drv.ms/b/s!Ag5FKK35oL7BgoVMG0S2okA5Z7fwMQ?e=NycsI4"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_onedrive.js"


def test_public_pdf(tmp_path, ensure_chrome_test_prereqs):
    assert HOOK.is_file(), "OneDrive hook is missing"
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
        output = chrome.parent / "onedrive"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["files"]) == 1
        item = manifest["files"][0]
        assert item["filename"] == "2024.06.16 Bulletin.pdf"
        data = (output / item["path"]).read_bytes()
        assert data.startswith(b"%PDF-")
        assert len(data) == item["size"] == 4501713
        assert (
            hashlib.sha256(data).hexdigest()
            == item["sha256"]
            == "ebb431ecff5d9f16f2cc16d8c766027af4a48dbc9c88995eafb9ef4713bd50a2"
        )


def test_public_excel_original(tmp_path, ensure_chrome_test_prereqs):
    url = "https://1drv.ms/x/s!AtWnsymKn5hRiD8rrKYuHTlatez2?e=7F03hJ"
    with chrome_session(tmp_path, test_url=url, timeout=60) as (_, _, chrome, env):
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
        output = chrome.parent / "onedrive"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["files"]) == 1
        item = manifest["files"][0]
        assert item["filename"] == "HWiNFO Log 1.xlsx"
        file = output / item["path"]
        data = file.read_bytes()
        assert len(data) == item["size"] == 41446
        assert (
            hashlib.sha256(data).hexdigest()
            == item["sha256"]
            == "6a76432aecd6cc4d862001c4964ad4c948db19412bc6ab350b450e92db5d6501"
        )
        with zipfile.ZipFile(file) as archive:
            assert archive.testzip() is None
            assert "xl/workbook.xml" in archive.namelist()
            assert "xl/worksheets/sheet1.xml" in archive.namelist()
            assert b'sheet name="in"' in archive.read("xl/workbook.xml")
            assert (
                b"Virtual Memory Commited [MB]"  # codespell:ignore commited
                in archive.read(
                    "xl/sharedStrings.xml",
                )
            )


def test_provider_homepage_has_no_document(tmp_path, ensure_chrome_test_prereqs):
    url = "https://onedrive.live.com/"
    with chrome_session(
        tmp_path,
        test_url=url,
        timeout=60,
        env_overrides={"AUTH_STORAGE_FILE": "", "CHROME_HEADLESS": "true"},
    ) as (_, _, chrome, env):
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
        assert not (chrome.parent / "onedrive").exists()
