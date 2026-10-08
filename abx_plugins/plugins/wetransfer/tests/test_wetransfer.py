"""Capture a genuine media kit and reject an expired transfer after accepting terms."""

import base64
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

import imagesize
from playwright.sync_api import sync_playwright

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://we.tl/t-JU6cMxNNFJMrW72K"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_wetransfer.js"
MEDIA_KIT_URL = "https://we.tl/t-vRtsYmlUVM"
ZIP_FILENAME = "katherinehunt_headshots-dr-katherine-hunt_2025-10-06_0144.zip"
HEADSHOTS = {
    "KH 2024 1.jpg": (2666, 2962),
    "KH FAAA photo.jpg": (6275, 4846),
    "KH on stage FAAA square.jpg": (3542, 2837),
    "ANDY4332.JPG": (4252, 2836),
    "ANDY2060.JPG": (4252, 2836),
    "ANDY2167.JPG": (4252, 2836),
}


def test_public_media_kit_zip(tmp_path, ensure_chrome_test_prereqs):
    # Published as high-resolution headshots on drkatherinehunt.com/media-kit.
    with chrome_session(tmp_path, test_url=MEDIA_KIT_URL, timeout=60) as (
        _,
        _,
        chrome,
        env,
    ):
        result = subprocess.run(
            [str(HOOK), f"--url={MEDIA_KIT_URL}"],
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
        output = chrome.parent / "wetransfer"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["files"]) == 1
        item = manifest["files"][0]
        assert item["filename"] == ZIP_FILENAME
        assert item["format"] == "zip"
        archive_path = output / item["path"]
        data = archive_path.read_bytes()
        assert len(data) == item["size"] == 12086255
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
        assert sorted(path.name for path in (output / "files").iterdir()) == [
            ZIP_FILENAME,
        ], "The delivery ZIP must remain unchanged, without extracted copies"
        with zipfile.ZipFile(archive_path) as archive, sync_playwright() as playwright:
            assert archive.testzip() is None, "All member CRCs must match"
            assert set(archive.namelist()) == set(HEADSHOTS)
            browser = playwright.chromium.connect_over_cdp(
                (chrome / "cdp_url.txt").read_text().strip(),
            )
            page = browser.contexts[0].pages[0]
            for filename, expected_size in HEADSHOTS.items():
                jpeg = archive.read(filename)
                assert jpeg.startswith(b"\xff\xd8\xff") and jpeg.endswith(b"\xff\xd9")
                assert imagesize.get(io.BytesIO(jpeg)) == expected_size
                decoded_size = page.evaluate(
                    """async encoded => {
                        const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
                        const bitmap = await createImageBitmap(new Blob([bytes], {type: 'image/jpeg'}));
                        const size = [bitmap.width, bitmap.height];
                        bitmap.close();
                        return size;
                    }""",
                    base64.b64encode(jpeg).decode(),
                )
                assert decoded_size == list(expected_size), filename


def test_expired_transfer_after_terms_acceptance(tmp_path, ensure_chrome_test_prereqs):
    with chrome_session(tmp_path, test_url=URL, timeout=60) as (_, _, chrome, env):
        # Record the actual export tab while it exists: the hook closes it on
        # failure. This observer reads DOM state without changing page behavior.
        observer_script = tmp_path / "observe-wetransfer.js"
        observer_script.write_text(
            """const fs = require('fs');
const puppeteer = require('puppeteer');
let browser;
let stopped = false;
const started = Date.now();
const record = value => fs.appendFileSync(process.argv[3],
  JSON.stringify({elapsed: (Date.now() - started) / 1000, ...value}) + '\\n');
process.on('SIGTERM', () => {
  stopped = true;
  if (browser) browser.disconnect();
  process.exit(0);
});
(async () => {
  browser = await puppeteer.connect({
    browserWSEndpoint: fs.readFileSync(process.argv[2], 'utf8').trim(),
    defaultViewport: null,
    protocolTimeout: 1000,
  });
  record({phase: 'observer_attached'});
  const previous = new Map();
  while (!stopped) {
    for (const page of await browser.pages()) {
      if (!/(^|\\.)wetransfer\\.com$/.test(new URL(page.url()).hostname)) continue;
      try {
        const state = await page.evaluate(() => {
          const buttons = [...document.querySelectorAll('button')]
            .filter(button => /agree|download|recover|refresh|reload|retry/i.test(button.innerText))
            .map(button => ({text: button.innerText.slice(0, 100),
              visible: !!(button.offsetWidth || button.offsetHeight), disabled: button.disabled}));
          const expired = /transfer (?:has )?expired|transfer (?:was )?deleted|Oops, the transfer you requested/i
            .test(document.body.innerText);
          return {url: location.origin + location.pathname, title: document.title.slice(0, 200),
            headings: [...document.querySelectorAll('h1,h2,[role="heading"]')]
              .filter(heading => !!(heading.offsetWidth || heading.offsetHeight))
              .map(heading => heading.innerText.slice(0, 200)).slice(0, 5), buttons, expired,
            focus: document.hasFocus(), visibility: document.visibilityState};
        });
        const encoded = JSON.stringify(state);
        const last = previous.get(page);
        if (!last || last.encoded !== encoded || Date.now() - last.at >= 5000) {
          const phase = state.expired ? 'expired' : state.buttons.some(button => button.text === 'I agree')
            ? 'terms' : state.buttons.some(button => /^(Download|Download all)$/.test(button.text))
            ? 'download' : 'loading';
          record({phase, page: previous.has(page) ? [...previous.keys()].indexOf(page) : previous.size, state});
          previous.set(page, {encoded, at: Date.now()});
        }
      } catch (error) { record({phase: 'page_read_error', name: error.name}); }
    }
    await new Promise(resolve => setTimeout(resolve, 500));
  }
})().catch(error => record({phase: 'observer_error', name: error.name}))
  .finally(() => { if (browser) browser.disconnect(); });
""",
        )
        observations = tmp_path / "wetransfer-expiry-observations.log"
        with observations.open("a") as observer_log:
            observer = subprocess.Popen(
                [
                    env["NODE_BINARY"],
                    str(observer_script),
                    str(chrome / "cdp_url.txt"),
                    str(observations),
                ],
                env=env,
                stdout=observer_log,
                stderr=observer_log,
            )
            try:
                result = subprocess.run(
                    [str(HOOK), f"--url={URL}"],
                    cwd=chrome.parent,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
            finally:
                observer.terminate()
                try:
                    observer.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    observer.kill()
                    observer.wait(timeout=2)
        record = parse_jsonl_output(result.stdout)
        evidence = "\n".join(observations.read_text().splitlines()[-20:])
        diagnostic = f"{result.stdout}\n{result.stderr}\nRead-only page observations:\n{evidence}"
        assert result.returncode == 1, diagnostic
        assert record and record["status"] == "failed", diagnostic
        assert "expired or was deleted" in record["output_str"], diagnostic
        assert not (chrome.parent / "wetransfer/downloads.json").exists()
