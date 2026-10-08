"""Export a real public Excalidraw scene through its browser controls."""

import base64
import hashlib
import json
import re
import subprocess
import time
import zlib
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    chrome_session,
)

URL = "https://excalidraw.com/#json=pJK6JcJMr7LGOuy1NbCKP,YneEARvxllEU6vlDQfz81A"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_excalidraw.js"


def test_public_encrypted_scene(tmp_path, ensure_chrome_test_prereqs):
    assert HOOK.is_file(), "Excalidraw hook is missing"
    with chrome_session(tmp_path, test_url=URL, timeout=90) as (_, _, chrome, env):
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
        output = chrome.parent / "excalidraw"
        manifest = json.loads((output / "downloads.json").read_text())
        assert {item["format"] for item in manifest["files"]} >= {"excalidraw", "svg"}
        native = next(
            item for item in manifest["files"] if item["format"] == "excalidraw"
        )
        native_scene = json.loads((output / native["path"]).read_bytes())
        for item in manifest["files"]:
            data = (output / item["path"]).read_bytes()
            assert len(data) == item["size"] > 0
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
            if item["format"] == "excalidraw":
                scene = json.loads(data)
                assert scene["type"] == "excalidraw"
                assert len(scene["elements"]) > 10
                assert "appState" in scene and "files" in scene
                assert any(
                    "Excalidraw" in element.get("text", "")
                    for element in scene["elements"]
                )
            elif item["format"] == "svg":
                assert b"<svg" in data and b"Excalidraw" in data
                payload = re.search(
                    rb"<!-- payload-start -->(.*?)<!-- payload-end -->",
                    data,
                    re.S,
                )
                assert payload, "SVG must preserve its editable scene"
                encoded = json.loads(
                    base64.b64decode(payload.group(1)).decode("latin1"),
                )
                assert encoded["encoding"] == "bstring" and encoded["compressed"]
                embedded = json.loads(
                    zlib.decompress(encoded["encoded"].encode("latin1")),
                )
                assert {element["id"] for element in embedded["elements"]} == {
                    element["id"] for element in native_scene["elements"]
                }


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://excalidraw.com/", "Not an Excalidraw shared scene"),
        # Public workshop room linked by Hochschule Bremerhaven:
        # https://informatik.hs-bremerhaven.de/ideathon/Ideathon-Workshops-Doku.pdf
        (
            "https://excalidraw.com/#room=1a2912ca0852c41eef79,S4VMrFiSIGhof-exCIRBFQ",
            "Live rooms need a saved #json scene link",
        ),
    ],
)
def test_page_without_saved_scene(tmp_path, ensure_chrome_test_prereqs, url, reason):
    with chrome_session(tmp_path, test_url=url, timeout=90) as (_, _, chrome, env):
        started = time.monotonic()
        result = subprocess.run(
            [str(HOOK), f"--url={url}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record is not None, result.stdout
        assert record["status"] == "noresults", result.stdout
        assert result.stderr.strip() == record["output_str"] == reason
        assert time.monotonic() - started < 10
        assert not (chrome.parent / "excalidraw" / "downloads.json").exists()


@pytest.mark.parametrize(
    "cancel_overwrite",
    [False, True],
    ids=["pending-overwrite", "canceled-overwrite"],
)
def test_existing_local_scene_is_preserved(
    tmp_path,
    ensure_chrome_test_prereqs,
    cancel_overwrite,
):
    with chrome_session(tmp_path, test_url=URL, timeout=90) as (_, _, chrome, env):
        # Edit the real scene using its text tool. Reloading the actual share in
        # this same persona must show the provider's real overwrite prompt.
        setup = f"""
const {{connectToPage}} = require({json.dumps(str(CHROME_UTILS))});
(async () => {{
  const {{browser,page}} = await connectToPage({{chromeSessionDir:process.argv[1],timeoutMs:30000,waitForNavigationComplete:true}});
  try {{
    await page.bringToFront();
    await page.waitForFunction(() => !location.hash, {{timeout:30000}});
    await page.click('[data-testid="toolbar-text"]');
    await page.mouse.click(1100,1500);
    await page.keyboard.type('ArchiveBox existing local scene must stay unchanged');
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => localStorage.getItem('excalidraw')?.includes('ArchiveBox existing local scene must stay unchanged'), {{timeout:30000}});
    let before = await page.evaluate(() => localStorage.getItem('excalidraw'));
    await page.goto({json.dumps(URL)}, {{waitUntil:'load',timeout:30000}});
    await page.reload({{waitUntil:'load',timeout:30000}});
    await page.waitForSelector('.OverwriteConfirm', {{visible:true,timeout:30000}});
    if ({json.dumps(cancel_overwrite)}) {{
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => !location.hash && !document.querySelector('.OverwriteConfirm'), {{timeout:30000}});
      // Excalidraw restores null binding lists to arrays and asynchronously
      // saves that normalized scene. Baseline its settled state before the hook.
      await page.waitForFunction(() => JSON.parse(localStorage.getItem('excalidraw')).some(element => element.text === 'ArchiveBox existing local scene must stay unchanged' && Array.isArray(element.boundElements)), {{timeout:30000}});
      before = await page.evaluate(() => localStorage.getItem('excalidraw'));
    }}
    console.log(JSON.stringify({{before,after:await page.evaluate(() => localStorage.getItem('excalidraw'))}}));
  }} finally {{ await browser.disconnect(); }}
}})().catch(error => {{ console.error(error);process.exitCode=1; }});
"""
        prepared = subprocess.run(
            [env["NODE_BINARY"], "-e", setup, str(chrome)],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert prepared.returncode == 0, prepared.stderr
        scenes = json.loads(prepared.stdout)
        assert scenes["before"] == scenes["after"]
        assert "ArchiveBox existing local scene must stay unchanged" in scenes["before"]
        hook_env = dict(env, EXCALIDRAW_TIMEOUT="12")
        started = time.monotonic()
        result = subprocess.run(
            [str(HOOK), f"--url={URL}"],
            cwd=chrome.parent,
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 1, result.stdout
        record = parse_jsonl_output(result.stdout)
        assert record is not None, result.stdout
        assert record["status"] == "failed", result.stdout
        expected_error = "not imported" if cancel_overwrite else "overwrite"
        assert expected_error in result.stdout, result.stdout
        assert time.monotonic() - started < 8
        assert not (chrome.parent / "excalidraw" / "downloads.json").exists()
        inspect = f"""
const {{connectToPage}} = require({json.dumps(str(CHROME_UTILS))});
(async () => {{
  const {{browser,page}} = await connectToPage({{chromeSessionDir:process.argv[1],timeoutMs:30000,waitForNavigationComplete:true}});
  try {{ console.log(JSON.stringify(await page.evaluate(() => ({{scene:localStorage.getItem('excalidraw'),overwrite:Boolean(document.querySelector('.OverwriteConfirm'))}})))); }}
  finally {{ await browser.disconnect(); }}
}})().catch(error => {{ console.error(error);process.exitCode=1; }});
"""
        inspected = subprocess.run(
            [env["NODE_BINARY"], "-e", inspect, str(chrome)],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=40,
        )
        assert inspected.returncode == 0, inspected.stderr
        final_state = json.loads(inspected.stdout)
        assert final_state["scene"] == scenes["before"]
        assert final_state["overwrite"] is not cancel_overwrite
