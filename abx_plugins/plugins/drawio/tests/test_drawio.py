"""Export an official public draw.io diagram using its real export menu."""

import hashlib
import json
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    chrome_session,
)

URL = "https://app.diagrams.net/#Uhttps%3A%2F%2Fraw.githubusercontent.com%2Fjgraph%2Fdrawio-diagrams%2Fmaster%2Fdiagrams%2Fschema.xml"
EMPTY_URL = "https://app.diagrams.net/#Uhttps://raw.githubusercontent.com/jgraph/mxgraph/ff141aab158417bd866e2dfebd06c61d40773cd2/javascript/examples/editors/diagrams/empty.xml"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_drawio.js"


@pytest.mark.parametrize(
    "url",
    ["https://app.diagrams.net/#foo", "https://app.draw.io/#foo"],
)
def test_unrelated_editor_fragment(tmp_path, ensure_chrome_test_prereqs, url):
    with chrome_session(tmp_path, test_url=url, timeout=60) as (_, _, chrome, env):
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
        assert (
            result.stderr.strip()
            == record["output_str"]
            == "Not a shared draw.io diagram"
        )
        assert not (chrome.parent / "drawio").exists()


@pytest.mark.parametrize(
    ("url", "blocked_download_dir"),
    [(URL, False), (EMPTY_URL, False), (URL, True)],
    ids=["schema", "empty-diagram", "blocked-download-directory"],
)
def test_public_schema_diagram(
    tmp_path,
    ensure_chrome_test_prereqs,
    url,
    blocked_download_dir,
):
    assert HOOK.is_file(), "drawio hook is missing"
    with chrome_session(tmp_path, test_url=url, timeout=60) as (_, _, chrome, env):
        if url == EMPTY_URL:
            env = dict(env, DRAWIO_TIMEOUT="12")
        markers = {
            name: (chrome / name).read_bytes()
            for name in ("target_id.txt", "navigation.json")
        }
        observe = f"""
const {{connectToPage,getTargetIdFromTarget}} = require({json.dumps(str(CHROME_UTILS))});
(async () => {{
  const {{browser,page}} = await connectToPage({{chromeSessionDir:process.argv[1],timeoutMs:30000,waitForNavigationComplete:true}});
  try {{
    if (process.argv[2] === 'start') {{
      // Start with the real capture tab focused, as if the user opened it.
      await page.bringToFront();
      // draw.io adds the loaded page ID to its URL after opening the source.
      // Baseline the settled document rather than that provider-owned startup.
      await page.waitForFunction(() => {{
        const container = document.querySelector('.geDiagramContainer');
        return container && getComputedStyle(container).visibility !== 'hidden' && decodeURIComponent(location.hash).includes('\"pageId\"');
      }}, {{timeout:Number(process.argv[3])*1000}});
      await page.evaluate(() => {{
        window.__abxExportObservedInputs = [];
        for (const type of ['pointerdown', 'mousedown', 'click', 'keydown', 'input']) {{
          document.addEventListener(type, event => {{
            if (event.isTrusted) window.__abxExportObservedInputs.push(event.type);
          }}, true);
        }}
      }});
    }}
    const state = await page.evaluate(() => ({{
      url:location.href,
      timeOrigin:performance.timeOrigin,
      focused:document.hasFocus(),
      visibility:document.visibilityState,
      inputs:window.__abxExportObservedInputs,
      dialogs:[...document.querySelectorAll('.geDialog')].filter(element => {{
        const box = element.getBoundingClientRect();
        return box.width > 0 && box.height > 0 && getComputedStyle(element).visibility !== 'hidden';
      }}).map(element => element.innerText),
    }}));
    state.targets = browser.targets().filter(target => target.type() === 'page').map(getTargetIdFromTarget).sort();
    console.log(JSON.stringify(state));
  }} finally {{ await browser.disconnect(); }}
}})().catch(error => {{ console.error(error);process.exitCode=1; }});
"""

        def observe_page(action):
            observed = subprocess.run(
                [
                    env["NODE_BINARY"],
                    "-e",
                    observe,
                    str(chrome),
                    action,
                    env.get("DRAWIO_TIMEOUT", "120"),
                ],
                cwd=chrome.parent,
                env=env,
                capture_output=True,
                text=True,
                timeout=int(env.get("DRAWIO_TIMEOUT", "120")) + 10,
            )
            assert observed.returncode == 0, observed.stderr
            return json.loads(observed.stdout)

        before = observe_page("start")
        provider_env = env
        if blocked_download_dir:
            # Exercise a genuine filesystem failure only in the provider hook.
            # The already-running capture browser retains its normal persona.
            blocked_personas = tmp_path / "blocked-personas"
            blocked_downloads = blocked_personas / "Default" / "chrome_downloads"
            blocked_downloads.parent.mkdir(parents=True)
            blocked_downloads.write_text(
                "A regular file obstructs the download directory",
            )
            provider_env = dict(env, PERSONAS_DIR=str(blocked_personas))
        result = subprocess.run(
            [str(HOOK), f"--url={url}"],
            cwd=chrome.parent,
            env=provider_env,
            capture_output=True,
            text=True,
            timeout=150,
        )
        record = parse_jsonl_output(result.stdout)
        assert record, result.stdout
        if blocked_download_dir:
            assert result.returncode == 1, result.stderr
            assert record["status"] == "failed", result.stdout
            assert "ENOTDIR" in result.stderr or "EEXIST" in result.stderr, (
                result.stderr
            )
            assert blocked_downloads.is_file()
        else:
            assert result.returncode == 0, result.stderr
            assert record["status"] == "succeeded", result.stdout
        after = observe_page("finish")
        assert after["inputs"] == [], (
            "Provider dispatched input onto the canonical capture tab"
        )
        for field in (
            "url",
            "timeOrigin",
            "targets",
            "dialogs",
            "focused",
            "visibility",
        ):
            assert after[field] == before[field], (
                f"Provider changed canonical browser state: {field}"
            )
        for name, original in markers.items():
            assert (chrome / name).read_bytes() == original

        dom_hook = HOOK.parents[1] / "dom" / "on_Snapshot__53_dom.js"
        dom_result = subprocess.run(
            [str(dom_hook), f"--url={url}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert dom_result.returncode == 0, dom_result.stderr
        dom_record = parse_jsonl_output(dom_result.stdout)
        assert dom_record and dom_record["status"] == "succeeded", dom_result.stdout
        dom = (chrome.parent / "dom" / "output.html").read_text()
        if url == URL:
            assert "UserRole" in dom and "AccountName" in dom
        output = chrome.parent / "drawio"
        if blocked_download_dir:
            assert not (output / "downloads.json").exists()
            return
        manifest = json.loads((output / "downloads.json").read_text())
        assert {item["format"] for item in manifest["files"]} == {"svg"}
        for item in manifest["files"]:
            data = (output / item["path"]).read_bytes()
            assert len(data) == item["size"] > 0
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
            svg = data.decode()
            assert "<svg" in svg
            assert "mxfile" in svg, "SVG must include editable diagram data"
            embedded = ElementTree.fromstring(
                ElementTree.fromstring(data).attrib["content"],
            )
            models = list(embedded.iter("mxGraphModel"))
            assert len(models) == 1
            if url == EMPTY_URL:
                # Existing official source has only Workflow/Layer bookkeeping,
                # no drawing objects. Its native data must survive the export.
                assert any(
                    element.tag == "Workflow"
                    and element.attrib.get("label") == "MyWorkflow"
                    for element in models[0].iter()
                )
                assert any(
                    element.tag == "Layer"
                    and element.attrib.get("label") == "Default Layer"
                    for element in models[0].iter()
                )
                assert not any(
                    element.attrib.get("vertex") == "1"
                    or element.attrib.get("edge") == "1"
                    for element in models[0].iter("mxCell")
                )
            else:
                assert "UserRole" in svg and "AccountName" in svg
