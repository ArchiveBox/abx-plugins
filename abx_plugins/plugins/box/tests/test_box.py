"""Download a real public PDF published by Nepean Sailing Club."""

import hashlib
import json
import subprocess
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://app.box.com/s/7o5sbghsq70vxcz8f8bylhemnngi7rrz"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_box.js"


def test_public_pdf(tmp_path, ensure_chrome_test_prereqs):
    assert HOOK.is_file(), "Box hook is missing"
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
        output = chrome.parent / "box"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["files"]) == 1
        item = manifest["files"][0]
        assert item["filename"] == "registration.pdf"
        data = (output / item["path"]).read_bytes()
        assert data.startswith(b"%PDF-")
        assert len(data) == item["size"] == 507937
        assert (
            hashlib.sha256(data).hexdigest()
            == item["sha256"]
            == "996244c7692aa2ac1d2fefdd6330f2d1042dad421651415d57c7b30d2e6fd96e"
        )


def test_public_logo_folder(tmp_path, ensure_chrome_test_prereqs):
    url = "https://app.box.com/s/9h9dfu6nbfskj4p64gal20xuf7d7f635"
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
        output = chrome.parent / "box"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["files"]) == 30
        assert not list((output / "files").rglob("*.zip"))
        for item in manifest["files"]:
            data = (output / item["path"]).read_bytes()
            assert b"<svg" in data
            assert len(data) == item["size"] > 0
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
        assert {Path(item["path"]).suffix for item in manifest["files"]} == {".svg"}


def test_unrelated_page(tmp_path, ensure_chrome_test_prereqs):
    url = "https://example.com"
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
        assert not (chrome.parent / "box/downloads.json").exists()


def test_provider_homepage_has_no_document(tmp_path, ensure_chrome_test_prereqs):
    url = "https://app.box.com/login"
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
        assert not (chrome.parent / "box").exists()
