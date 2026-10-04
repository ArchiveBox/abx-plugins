"""Real provider ZIP downloads using the existing Chrome tab."""

import hashlib
import json
import subprocess
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    fetch_devtools_targets,
)

URL = "https://drive.google.com/drive/folders/1KpLl_1tcK0eeehzN980zbG-3M2nhbVks"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_gdrive.js"


def test_public_folder_zip(tmp_path, ensure_chrome_test_prereqs):
    assert HOOK.is_file(), "gdrive folder download hook is missing"
    with chrome_session(tmp_path, test_url=URL, timeout=60) as (_, _, chrome_dir, env):
        endpoint = (chrome_dir / "cdp_url.txt").read_text().strip()
        before = fetch_devtools_targets(endpoint)
        result = subprocess.run(
            [str(HOOK), f"--url={URL}"],
            cwd=chrome_dir.parent,
            env={**env, "GDRIVE_TIMEOUT": "120"},
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
        output = chrome_dir.parent / "gdrive"
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
