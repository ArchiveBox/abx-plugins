"""Export actual public Keynote, Pages and Numbers documents."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest
from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://www.icloud.com/keynote/066uNOonke1QaqLmsnlMXpEng"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_iwork.js"


def test_public_keynote_pdf(tmp_path, ensure_chrome_test_prereqs):
    assert HOOK.is_file(), "iWork hook is missing"
    with chrome_session(
        tmp_path,
        test_url=URL,
        timeout=60,
        env_overrides={"IWORK_TIMEOUT": "120"},
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
        assert record and record["status"] == "succeeded", result.stdout
        output = chrome.parent / "iwork"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["files"]) == 1
        item = manifest["files"][0]
        assert item["filename"] == "CoT-zuzalu-final.pdf"
        data = (output / item["path"]).read_bytes()
        assert data.startswith(b"%PDF-")
        assert b"%%EOF" in data[-1024:]
        assert b"/Title (CoT-zuzalu-final)" in data
        assert re.search(rb"/Count\s+61(?=\s|/|>)", data)
        assert len(data) == item["size"] > 10000
        assert hashlib.sha256(data).hexdigest() == item["sha256"]


@pytest.mark.parametrize(
    ("url", "title", "pages"),
    [
        pytest.param(
            "https://www.icloud.com/pages/0aI0WdI93gupdpSgzGNwu90Pw#Protocole_Raman",
            "Protocole Raman",
            21,
            id="pages",
        ),
        pytest.param(
            "https://www.icloud.com/numbers/049dkCvCOid9P8S0UxtCGE3iA#NIO-dependency-check",
            "NIO-dependency-check",
            1,
            id="numbers",
        ),
    ],
)
def test_public_pages_and_numbers_pdf(
    tmp_path,
    ensure_chrome_test_prereqs,
    url,
    title,
    pages,
):
    # Published by dccote/Enseignement and SwiftPackageIndex respectively.
    with chrome_session(
        tmp_path,
        test_url=url,
        timeout=60,
        env_overrides={"IWORK_TIMEOUT": "120"},
    ) as (_, _, chrome, env):
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
        output = chrome.parent / "iwork"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["files"]) == 1
        item = manifest["files"][0]
        assert item["filename"] == title + ".pdf"
        data = (output / item["path"]).read_bytes()
        assert data.startswith(b"%PDF-") and b"%%EOF" in data[-1024:]
        assert f"/Title ({title})".encode() in data
        assert re.search(rb"/Count\s+" + str(pages).encode() + rb"(?=\s|/|>)", data)
        assert len(data) == item["size"] > 10000
        assert hashlib.sha256(data).hexdigest() == item["sha256"]


def test_provider_homepage_has_no_document(tmp_path, ensure_chrome_test_prereqs):
    url = "https://www.icloud.com/keynote/"
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
        assert not (chrome.parent / "iwork").exists()
