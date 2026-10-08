"""Download and decrypt the real public folder published by pdown upstream."""

import hashlib
import json
import subprocess
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://drive.proton.me/urls/KGER0RS624#LzmiMIuikOuj"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_protondrive.js"


def test_public_nested_folder(tmp_path, ensure_chrome_test_prereqs):
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
        output = chrome.parent / "protondrive"
        manifest = json.loads((output / "downloads.json").read_text())
        files = {item["filename"]: output / item["path"] for item in manifest["files"]}
        assert set(files) == {
            "example.txt",
            "subfolder/example.jpeg",
            "subfolder/subfolder-2/example.mp4",
        }
        for item in manifest["files"]:
            data = (output / item["path"]).read_bytes()
            assert len(data) == item["size"] > 0
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
        assert files["example.txt"].read_text() == ":)\n"
        assert files["subfolder/example.jpeg"].read_bytes().startswith(b"\xff\xd8\xff")
        assert b"ftyp" in files["subfolder/subfolder-2/example.mp4"].read_bytes()[:32]
        assert files["subfolder/example.jpeg"].parent.name == "subfolder"
        assert files["subfolder/subfolder-2/example.mp4"].parent.name == "subfolder-2"
