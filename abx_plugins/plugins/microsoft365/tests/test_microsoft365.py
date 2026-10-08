"""Capture the original of a real Office web-viewed CPUC Word document."""

import hashlib
import json
import subprocess
import zipfile

import pytest
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://view.officeapps.live.com/op/view.aspx?src=https%3A%2F%2Fdocs.cpuc.ca.gov%2FPublishedDocs%2FPublished%2FG000%2FM551%2FK722%2F551722326.docx"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_microsoft365.js"


def test_public_word_original(tmp_path, ensure_chrome_test_prereqs):
    assert HOOK.is_file(), "Microsoft365 hook is missing"
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
        output = chrome.parent / "microsoft365"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["files"]) == 1
        item = manifest["files"][0]
        assert item["filename"] == "551722326.docx"
        file = output / item["path"]
        data = file.read_bytes()
        assert data.startswith(b"PK\x03\x04")
        assert len(data) == item["size"] > 10000
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
        with zipfile.ZipFile(file) as archive:
            assert archive.testzip() is None
            assert "[Content_Types].xml" in archive.namelist()
            document = archive.read("word/document.xml")
            assert b"PUBLIC UTILITIES COMMISSION" in document
            assert b"VEGETATION MANAGEMENT WORK" in document


SHAREPOINT_URL = "https://pennstateoffice365.sharepoint.com/:b:/s/Research-to-PolicyCollaboration/EdtX1PTKSn5Apst-a_IKjIYBiP156uATgWctqeH0DK0v1A?e=mCHpWq"

# Actual settled document URL from the public share's Chrome navigation. Opening
# it directly in a fresh anonymous session lacks the share's access context.
SHAREPOINT_DOCUMENT_URL = "https://pennstateoffice365.sharepoint.com/sites/Research-to-PolicyCollaboration/Manuscripts/Forms/AllItems.aspx?id=%2Fsites%2FResearch%2Dto%2DPolicyCollaboration%2FManuscripts%2F2%2E%20Final%20Papers%2FGay%20%282018%29%20Network%20Engagement%20Survey%20Report%5FFINAL%2Epdf&parent=%2Fsites%2FResearch%2Dto%2DPolicyCollaboration%2FManuscripts%2F2%2E%20Final%20Papers&p=true&ga=1"


def test_sharepoint_document_login_prerequisite(tmp_path, ensure_chrome_test_prereqs):
    with chrome_session(
        tmp_path,
        test_url=SHAREPOINT_DOCUMENT_URL,
        timeout=60,
        env_overrides={"AUTH_STORAGE_FILE": "", "CHROME_HEADLESS": "true"},
    ) as (_, _, chrome, env):
        navigation = json.loads((chrome / "navigation.json").read_text())
        assert navigation["finalUrl"].startswith(
            "https://login.microsoftonline.com/",
        ), navigation
        result = subprocess.run(
            [str(HOOK), f"--url={SHAREPOINT_DOCUMENT_URL}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "skipped", result.stdout
        assert record["output_str"] == "Persona must be logged in to microsoft.com"
        assert not (chrome.parent / "microsoft365").exists()


def test_public_sharepoint_pdf(tmp_path, ensure_chrome_test_prereqs):
    with chrome_session(tmp_path, test_url=SHAREPOINT_URL, timeout=60) as (
        _,
        _,
        chrome,
        env,
    ):
        result = subprocess.run(
            [str(HOOK), f"--url={SHAREPOINT_URL}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=150,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "succeeded", result.stdout
        output = chrome.parent / "microsoft365"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["files"]) == 1
        item = manifest["files"][0]
        assert (
            item["filename"] == "Gay (2018) Network Engagement Survey Report_FINAL.pdf"
        )
        data = (output / item["path"]).read_bytes()
        assert data.startswith(b"%PDF-")
        assert b"%%EOF" in data[-1024:]
        assert len(data) == item["size"] == 1254865
        assert (
            hashlib.sha256(data).hexdigest()
            == item["sha256"]
            == "92b0400cee14ed189694ff1ef518cd5acdc210e3edf3b0ed301723c706bd7ff0"
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://pennstateoffice365.sharepoint.com/",
        "https://www.office.com/",
        "https://view.officeapps.live.com/op/view.aspx",
        "https://view.officeapps.live.com/op/embed.aspx?src=",
        "https://view.officeapps.live.com/op/view.aspx?src=not-a-document-url",
        "https://view.officeapps.live.com/op/view.aspx?src=ftp%3A%2F%2Fexample.com%2Ffile.docx",
    ],
)
def test_provider_homepages_have_no_document(tmp_path, ensure_chrome_test_prereqs, url):
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
        assert not (chrome.parent / "microsoft365").exists()
