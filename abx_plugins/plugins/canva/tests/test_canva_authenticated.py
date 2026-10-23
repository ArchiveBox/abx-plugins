# ci-environment: provider-capture
"""Live acceptance through the author's public use-template flow."""

import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://www.canva.com/design/DAGudZAYlEE/VzrUrqpV2RhkVHCg43lvKQ/view?mode=preview&utm_campaign=designshare&utm_content=DAGudZAYlEE&utm_medium=link&utm_source=publishsharelink"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_canva.js"


def run_observed_hook(tmp_path, chrome, env):
    # The export tab closes on failure. Preserve the trusted input target rather
    # than inferring a successful click from the control the hook intended to hit.
    script = tmp_path / "observe-canva.js"
    script.write_text(
        """const fs = require('fs');
const puppeteer = require('puppeteer');
let browser;
const started = Date.now();
const record = value => fs.appendFileSync(process.argv[3],
  JSON.stringify({elapsed: (Date.now() - started) / 1000, ...value}) + '\\n');
process.on('SIGTERM', () => { if (browser) browser.disconnect(); process.exit(0); });
(async () => {
  browser = await puppeteer.connect({
    browserWSEndpoint: fs.readFileSync(process.argv[2], 'utf8').trim(),
    defaultViewport: null,
  });
  const observed = new Set();
  const observe = async page => {
    if (observed.has(page)) return;
    observed.add(page);
    const pageId = observed.size;
    await page.exposeFunction('__archiveboxObserveCanvaInput', value => record({page: pageId, ...value}));
    const listen = () => {
      if (window.__archiveboxCanvaInputObserved) return;
      window.__archiveboxCanvaInputObserved = true;
      const rect = el => { const r = el?.getBoundingClientRect();
        return r && {x: r.x, y: r.y, width: r.width, height: r.height}; };
      const describe = el => el && ({tag: el.tagName, role: el.getAttribute('role'),
        label: el.getAttribute('aria-label'), rect: rect(el)});
      const state = () => {
        const control = document.querySelector('[role="combobox"][aria-label="File type"]');
        return {control: describe(control), expanded: control?.getAttribute('aria-expanded'),
          selectedFormat: control?.innerText.slice(0, 100),
          options: document.querySelectorAll('[role="option"]').length};
      };
      for (const type of ['pointerdown', 'pointerup', 'click']) document.addEventListener(type, event => {
        const top = document.elementFromPoint(event.clientX, event.clientY);
        const control = event.target.closest?.('button,[role="combobox"],[role="menuitem"],[role="option"]');
        window.__archiveboxObserveCanvaInput({phase: 'input', type, trusted: event.isTrusted,
          x: event.clientX, y: event.clientY, target: describe(event.target),
          inputControl: describe(control), text: control?.innerText.slice(0, 100),
          top: describe(top), sameTarget: event.target === top || event.target.contains(top),
          focus: document.hasFocus(), visibility: document.visibilityState,
          viewport: {width: innerWidth, height: innerHeight}, ...state(),
          animations: document.getAnimations().filter(a => a.playState === 'running').slice(0, 20)
            .map(a => ({target: describe(a.effect?.target), currentTime: a.currentTime}))});
      }, true);
      let previous;
      new MutationObserver(() => {
        const current = state();
        const encoded = JSON.stringify(current);
        if (current.control && encoded !== previous) {
          previous = encoded;
          window.__archiveboxObserveCanvaInput({phase: 'state', ...current});
        }
      }).observe(document, {subtree: true, childList: true, attributes: true,
        attributeFilter: ['aria-expanded']});
    };
    await page.evaluateOnNewDocument(listen);
    await page.evaluate(listen).catch(() => {});
    record({phase: 'attached', page: pageId});
  };
  browser.on('targetcreated', target => target.page().then(page => page && observe(page))
    .catch(error => record({phase: 'attach_error', name: error.name})));
  for (const page of await browser.pages()) await observe(page);
})().catch(error => record({phase: 'observer_error', name: error.name}));
""",
    )
    observations = chrome / "canva-observer.stderr.log"
    with observations.open("a") as log:
        observer = subprocess.Popen(
            [
                env["NODE_BINARY"],
                str(script),
                str(chrome / "cdp_url.txt"),
                str(observations),
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            return subprocess.run(
                [str(HOOK), f"--url={URL}"],
                cwd=chrome.parent,
                env=env,
                capture_output=True,
                text=True,
                timeout=150,
            )
        finally:
            observer.terminate()
            try:
                observer.wait(timeout=2)
            except subprocess.TimeoutExpired:
                observer.kill()
                observer.wait(timeout=2)


@pytest.mark.parametrize("resolution", ["1440,2000", "800,600"])
def test_authenticated_template_copy(tmp_path, ensure_chrome_test_prereqs, resolution):
    auth = os.environ.get("AUTH_STORAGE_FILE")
    assert auth and Path(auth).is_file(), (
        "Set AUTH_STORAGE_FILE to the authorized Canva persona before running this explicit acceptance test"
    )
    with chrome_session(
        tmp_path,
        test_url=URL,
        timeout=90,
        env_overrides={
            "AUTH_STORAGE_FILE": auth,
            "CHROME_HEADLESS": "false",
            "CHROME_RESOLUTION": resolution,
        },
    ) as (_, _, chrome, env):
        result = run_observed_hook(tmp_path, chrome, env)
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record is not None, result.stdout
        assert record["status"] == "succeeded", result.stdout
        output = chrome.parent / "canva"
        manifest = json.loads((output / "downloads.json").read_text())
        assert "Public Access - Certificate" in manifest["title"]
        assert {item["format"] for item in manifest["files"]} == {"pptx", "pdf"}
        for item in manifest["files"]:
            file = output / item["path"]
            data = file.read_bytes()
            assert len(data) == item["size"] > 0
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
            if item["format"] == "pptx":
                with zipfile.ZipFile(file) as archive:
                    assert archive.testzip() is None
                    slide = archive.read("ppt/slides/slide1.xml")
                    assert b"CERTIFICATE" in slide
                    assert b"APPRECIATION" in slide
                    assert (
                        len(
                            [
                                name
                                for name in archive.namelist()
                                if name.startswith("ppt/media/")
                            ],
                        )
                        > 0
                    )
            else:
                assert data.startswith(b"%PDF-") and b"%%EOF" in data[-100:]
                text = subprocess.run(
                    ["pdftotext", str(file), "-"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                # The curved CERTIFICATE heading consists of individually
                # positioned glyphs, so PDF reading order separates letters.
                assert "OF APPRECIATION" in text
                assert "This certificate is awarded to" in text
                assert "First Lastname" in text
