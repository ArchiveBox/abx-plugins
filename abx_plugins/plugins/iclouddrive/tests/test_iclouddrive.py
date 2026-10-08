"""Download the actual public PDF linked by its author's fundraiser."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://www.icloud.com/iclouddrive/09bul5IE1NC7UNrQMRSWznvnw#Tammy's_Poem_FINAL"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_iclouddrive.js"


def test_public_pdf(tmp_path, ensure_chrome_test_prereqs):
    assert HOOK.is_file(), "iCloud Drive hook is missing"
    extra_args = os.environ.get("CHROME_ARGS_EXTRA", "").strip()
    extra_args = (
        json.loads(extra_args)
        if extra_args.startswith("[")
        else [arg.strip() for arg in extra_args.split(",") if arg.strip()]
    )
    with chrome_session(
        tmp_path,
        test_url=URL,
        timeout=60,
        env_overrides={
            "CHROME_ARGS_EXTRA": json.dumps(
                extra_args
                + [
                    f"--log-net-log={tmp_path / 'chrome-net.log'}",
                    "--net-log-capture-mode=Default",
                ],
            ),
        },
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
        output = chrome.parent / "iclouddrive"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["files"]) == 1
        item = manifest["files"][0]
        assert item["filename"] == "Tammy's Poem FINAL.pdf"
        data = (output / item["path"]).read_bytes()
        assert data.startswith(b"%PDF-")
        assert b"%%EOF" in data[-1024:]
        assert len(data) == item["size"] == 41723
        assert (
            hashlib.sha256(data).hexdigest()
            == item["sha256"]
            == "140e13e3de9d0031e5b610d2f0c122e74303eff3c951503bab3526ecd437f60b"
        )


def test_provider_homepage_has_no_document(tmp_path, ensure_chrome_test_prereqs):
    url = "https://www.icloud.com/iclouddrive/"
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
        assert not (chrome.parent / "iclouddrive").exists()
