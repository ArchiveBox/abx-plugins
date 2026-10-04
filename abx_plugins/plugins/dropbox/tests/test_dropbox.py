"""Real provider ZIP downloads using the existing Chrome tab."""

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, parse_qsl, urlencode

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    fetch_devtools_targets,
)

URL = "https://www.dropbox.com/scl/fo/kf9a29cwaebpkbtug6a7k/AFiq9xq2XcvTcmHl_z-tsIc/Lockups?rlkey=4mrp0lpvxmwrlwdy349nspygn&dl=0"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_dropbox.js"


def tab_identity(targets: list[dict[str, Any]]) -> dict[str, str]:
    # Dropbox adds its e=1 experiment parameter during download. Retain the
    # target ID, path, share token, and every other query parameter.
    result: dict[str, str] = {}
    for target in targets:
        if target["type"] != "page":
            continue
        url = urlsplit(target["url"])
        query = urlencode(
            sorted((key, value) for key, value in parse_qsl(url.query) if key != "e"),
        )
        result[target["id"]] = url._replace(query=query).geturl()
    return result


def test_public_folder_zip(tmp_path, ensure_chrome_test_prereqs):
    assert HOOK.is_file(), "dropbox folder download hook is missing"
    with chrome_session(tmp_path, test_url=URL, timeout=60) as (_, _, chrome_dir, env):
        endpoint = (chrome_dir / "cdp_url.txt").read_text().strip()
        before = fetch_devtools_targets(endpoint)
        result = subprocess.run(
            [str(HOOK), f"--url={URL}"],
            cwd=chrome_dir.parent,
            env={**env, "DROPBOX_TIMEOUT": "120"},
            capture_output=True,
            text=True,
            timeout=150,
        )
        assert result.returncode == 0, result.stderr
        result_record = parse_jsonl_output(result.stdout)
        assert result_record is not None, result.stdout
        assert result_record["status"] == "succeeded"
        after = fetch_devtools_targets(endpoint)
        assert tab_identity(after) == tab_identity(before)
        output = chrome_dir.parent / "dropbox"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["files"]) == 8
        assert not list(output.glob("*.zip"))
        names = [item["filename"] for item in manifest["files"]]
        for item in manifest["files"]:
            data = (output / item["path"]).read_bytes()
            assert len(data) == item["size"]
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
            assert data.startswith(b"\x89PNG\r\n\x1a\n")
        assert all(name.endswith(".png") for name in names)


def test_public_shared_file(tmp_path, ensure_chrome_test_prereqs):
    url = URL + "&preview=SHIFT_EverybodyWork_Lockup_Horizontal_Black.png"
    with chrome_session(tmp_path, test_url=url, timeout=60) as (_, _, chrome_dir, env):
        endpoint = (chrome_dir / "cdp_url.txt").read_text().strip()
        before = fetch_devtools_targets(endpoint)
        result = subprocess.run(
            [str(HOOK), f"--url={url}"],
            cwd=chrome_dir.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=150,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record is not None and record["status"] == "succeeded"
        after = fetch_devtools_targets(endpoint)
        assert tab_identity(after) == tab_identity(before)
        output = chrome_dir.parent / "dropbox"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["files"]) == 1
        item = manifest["files"][0]
        assert item["filename"] == "SHIFT_EverybodyWork_Lockup_Horizontal_Black.png"
        assert item["format"] == "png"
        data = (output / item["path"]).read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
        assert len(data) == item["size"]
