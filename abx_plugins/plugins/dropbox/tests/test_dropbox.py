"""Real provider ZIP downloads using the existing Chrome tab."""

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    fetch_devtools_targets,
)

URL = "https://www.dropbox.com/scl/fo/kf9a29cwaebpkbtug6a7k/AFiq9xq2XcvTcmHl_z-tsIc/Lockups?rlkey=4mrp0lpvxmwrlwdy349nspygn&dl=0"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_dropbox.js"


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
        if result.returncode:
            diagnostic = subprocess.run(
                [
                    env["NODE_BINARY"],
                    "-e",
                    """
const u=require(process.argv[1]);(async()=>{const {browser,page}=await u.connectToPage({chromeSessionDir:process.argv[2],waitForNavigationComplete:true});try{console.log(await page.evaluate(()=>document.body.innerText));console.log(JSON.stringify(await page.$$eval('[role="menuitem"], [guidedhelpid="folder_path_button"], [data-testid="action-bar-download-button"]',els=>els.map(e=>({text:e.textContent,html:e.outerHTML.slice(0,500)})))));}finally{await browser.disconnect();}})().catch(e=>console.error(e.message));
""",
                    str(HOOK.parent.parent / "chrome/chrome_utils.js"),
                    str(chrome_dir),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            print(diagnostic.stdout, diagnostic.stderr)
        assert result.returncode == 0, result.stderr
        result_record = parse_jsonl_output(result.stdout)
        assert result_record is not None, result.stdout
        assert result_record["status"] == "succeeded"
        after = fetch_devtools_targets(endpoint)
        assert {t["id"]: t["url"] for t in after if t["type"] == "page"} == {
            t["id"]: t["url"] for t in before if t["type"] == "page"
        }
        output = chrome_dir.parent / "dropbox"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["downloads"]) == 1
        item = manifest["downloads"][0]
        data = (output / item["path"]).read_bytes()
        assert len(data) == item["size"]
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
        with zipfile.ZipFile(output / item["path"]) as archive:
            assert archive.testzip() is None
            names = archive.namelist()
            assert len(names) == 9
            assert "/" in names
            names = [n for n in names if not n.endswith("/")]
            assert len(names) == 8
            assert all(n.endswith(".png") for n in names)
            assert all(archive.read(n).startswith(b"\x89PNG\r\n\x1a\n") for n in names)


def test_public_shared_file(tmp_path, ensure_chrome_test_prereqs):
    url = URL + "&preview=SHIFT_EverybodyWork_Lockup_Horizontal_Black.png"
    with chrome_session(tmp_path, test_url=url, timeout=60) as (_, _, chrome_dir, env):
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
        output = chrome_dir.parent / "dropbox"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["downloads"]) == 1
        item = manifest["downloads"][0]
        assert item["filename"] == "SHIFT_EverybodyWork_Lockup_Horizontal_Black.png"
        assert item["format"] == "png"
        data = (output / item["path"]).read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
        assert len(data) == item["size"]
