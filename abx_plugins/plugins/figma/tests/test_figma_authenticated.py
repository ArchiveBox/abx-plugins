# ci-environment: provider-capture
# ci-runner: ugnas
# Use the operator's residential egress for the authenticated Figma session.
"""Explicit live acceptance: requires an authorized AUTH_STORAGE_FILE persona."""

import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://www.figma.com/design/0YpAEiii3cM0l3xidTbWPk/Style-Guide-Starter--Copy---Copy-?node-id=0-1&p=f"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_figma.js"


def test_authenticated_public_design(tmp_path, ensure_chrome_test_prereqs):
    auth = os.environ.get("AUTH_STORAGE_FILE")
    assert auth and Path(auth).is_file(), (
        "Set AUTH_STORAGE_FILE to an authorized Figma persona before running this explicit acceptance test"
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
        output = chrome.parent / "figma"
        manifest = json.loads((output / "downloads.json").read_text())
        assert manifest["title"] == "Style Guide Starter (Copy) (Copy) – Figma"
        assert {item["format"] for item in manifest["files"]} >= {"fig", "pdf"}
        for item in manifest["files"]:
            file = output / item["path"]
            data = file.read_bytes()
            assert len(data) == item["size"] > 0
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
            if item["format"] == "fig":
                with zipfile.ZipFile(file) as archive:
                    assert archive.testzip() is None
                    assert archive.read("canvas.fig").startswith(b"fig-kiwi")
                    assert archive.read("thumbnail.png").startswith(
                        b"\x89PNG\r\n\x1a\n",
                    )
                    assert json.loads(archive.read("meta.json"))["client_meta"]
                    assert (
                        sum(
                            name.startswith("images/") and not name.endswith("/")
                            for name in archive.namelist()
                        )
                        > 10
                    )
            elif item["format"] == "pdf":
                assert data.startswith(b"%PDF-") and b"%%EOF" in data[-100:]
                assert b"/Count 8" in data
