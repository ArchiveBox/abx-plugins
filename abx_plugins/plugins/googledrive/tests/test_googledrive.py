"""Real provider ZIP downloads using the existing Chrome tab."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    fetch_devtools_targets,
)

URL = "https://drive.google.com/drive/folders/1KpLl_1tcK0eeehzN980zbG-3M2nhbVks"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_googledrive.js"


@pytest.mark.parametrize(
    "custom_user_agent",
    [False, True],
    ids=["native-ua", "cross-platform-ua"],
)
def test_public_folder_zip(tmp_path, ensure_chrome_test_prereqs, custom_user_agent):
    assert HOOK.is_file(), "googledrive folder download hook is missing"
    # ArchiveBox configures a Mac UA even on Linux. The provider chooses its
    # keyboard shortcuts from that UA, which may differ from navigator.platform.
    platform = (
        "X11; Linux x86_64"
        if sys.platform == "darwin"
        else "Macintosh; Intel Mac OS X 10_15_7"
    )
    overrides = (
        {
            "CHROME_USER_AGENT": f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }
        if custom_user_agent
        else {}
    )
    with chrome_session(
        tmp_path,
        test_url=URL,
        timeout=60,
        env_overrides=overrides,
    ) as (_, _, chrome_dir, env):
        endpoint = (chrome_dir / "cdp_url.txt").read_text().strip()
        before = fetch_devtools_targets(endpoint)
        result = subprocess.run(
            [str(HOOK), f"--url={URL}"],
            cwd=chrome_dir.parent,
            env={**env, "GOOGLEDRIVE_TIMEOUT": "120"},
            capture_output=True,
            text=True,
            timeout=150,
        )
        assert result.returncode == 0, result.stderr
        result_record = parse_jsonl_output(result.stdout)
        assert result_record is not None, result.stdout
        assert result_record["status"] == "succeeded"
        after = fetch_devtools_targets(endpoint)
        assert {t["id"]: t["url"] for t in after if t["type"] == "page"} == {
            t["id"]: t["url"] for t in before if t["type"] == "page"
        }
        output = chrome_dir.parent / "googledrive"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["files"]) == 6
        assert not list(output.glob("*.zip"))
        names = [item["filename"] for item in manifest["files"]]
        for item in manifest["files"]:
            data = (output / item["path"]).read_bytes()
            assert len(data) == item["size"]
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
        assert "fractal.jpg" in names
        assert (output / "files/directory-0").is_dir()
        assert any("directory-1/" in name for name in names)
        assert "Lorem ipsum" in (output / "files/this is a file.txt").read_text()
