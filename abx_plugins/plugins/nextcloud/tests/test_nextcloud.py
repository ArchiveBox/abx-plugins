"""Download Nextcloud's real public ISV press assets through Chrome."""

import hashlib
import json
import subprocess
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://cloud.nextcloud.com/s/Ha8J6so77yXNrQ3"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_nextcloud.js"


def test_public_press_folder(tmp_path, ensure_chrome_test_prereqs):
    assert HOOK.is_file(), "nextcloud hook is missing"
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
        output = chrome.parent / "nextcloud"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["files"]) == 10
        for item in manifest["files"]:
            data = (output / item["path"]).read_bytes()
            assert len(data) == item["size"] > 0
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
        picture = (
            output
            / "files/Images_Nextcloud ISV Partner Program/Nextcloud ISV Partner Program_1.png"
        )
        assert picture.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert picture.stat().st_size == 603266
        assert not list(output.glob("*.zip"))


def test_public_owncloud_file(tmp_path, ensure_chrome_test_prereqs):
    # Published by the University of Goettingen's R teaching materials.
    url = "https://owncloud.gwdg.de/index.php/s/MwST1BwNLxtDon3"
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
        output = chrome.parent / "nextcloud"
        manifest = json.loads((output / "downloads.json").read_text())
        assert [f["filename"] for f in manifest["files"]] == ["join_exp.txt"]
        text = (output / "files/join_exp.txt").read_text()
        assert text.splitlines()[0].split() == ["subj", "time", "res"]
        assert len(text.splitlines()) == 10
