"""SingleFile must retain content used by other replay viewport sizes."""

import json
import subprocess

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session
from abx_plugins.plugins.singlefile.tests.test_singlefile import (
    CHROME_UTILS,
    SNAPSHOT_HOOK,
)


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_singlefile_preserves_responsive_images(
    tmp_path,
    httpserver,
    singlefile_install_state,
):
    httpserver.expect_request("/").respond_with_data(
        """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
        <style>.mobile{display:none}@media(max-width:600px){.desktop{display:none}.mobile{display:block}}</style>
        </head><body><h1>Responsive archive</h1>
        <img id="desktop" class="desktop" src="/desktop.svg" width="80" height="60">
        <img id="mobile" class="mobile" src="/mobile.svg" width="80" height="60">
        </body></html>""",
        content_type="text/html",
    )
    for name, color in (("desktop", "blue"), ("mobile", "green")):
        httpserver.expect_request(f"/{name}.svg").respond_with_data(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60"><rect width="80" height="60" fill="{color}"/></svg>',
            content_type="image/svg+xml",
        )
    url = httpserver.url_for("/")
    httpserver.expect_request("/favicon.ico").respond_with_data(b"", status=204)
    state = singlefile_install_state
    with chrome_session(
        tmp_path,
        test_url=url,
        navigate=True,
        timeout=30,
        env_overrides={
            "ABXPKG_CHROMEWEBSTORE_ROOT": str(state["extensions_dir"].parent),
            "CHROMEWEBSTORE_EXTENSIONS_DIR": str(state["extensions_dir"]),
        },
    ) as (_, _, chrome, env):
        output = chrome.parent / "singlefile"
        output.mkdir()
        script = f"""
const chromeUtils = require({json.dumps(str(CHROME_UTILS))});
(async () => {{
  const {{ browser, page, extensions }} = await chromeUtils.connectToPage({{
    chromeSessionDir: {json.dumps(str(chrome))}, requireTargetId: true,
    waitForNavigationComplete: true,
  }});
  try {{
    const extension = chromeUtils.findExtensionMetadataByName(extensions, 'singlefile');
    const configPage = await browser.newPage();
    let profiles;
    try {{
      await configPage.goto(`chrome-extension://${{extension.id}}/src/ui/pages/offscreen-document.html`);
      profiles = await configPage.evaluate(() => chrome.runtime.sendMessage({{method: 'config.getProfiles'}}));
    }} finally {{ await configPage.close(); }}
    if (process.argv[1] === 'prepare') {{
      await page.setViewport({{ width: 1280, height: 800 }});
      console.log(JSON.stringify({{profiles, display: await page.$eval('#mobile', e => getComputedStyle(e).display)}}));
    }} else {{
      const replay = await browser.newPage();
      try {{
        const rendered = [];
        for (const [width, selector] of [[390, '#mobile'], [1280, '#desktop']]) {{
          await replay.setViewport({{ width, height: 844 }});
          await replay.goto({json.dumps((output / "singlefile.html").as_uri())});
          rendered.push(await replay.evaluate(selector => {{
            const image = document.querySelector(selector);
            return {{ src: image?.getAttribute('src'), width: image?.naturalWidth,
              display: image && getComputedStyle(image).display,
              heading: document.querySelector('h1')?.textContent }};
          }}, selector));
        }}
        console.log(JSON.stringify({{rendered, profiles}}));
      }} finally {{ await replay.close(); }}
    }}
  }} finally {{ await browser.disconnect(); }}
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
        before = subprocess.run(
            [env["NODE_BINARY"], "-e", script, "prepare"],
            cwd=output,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert before.returncode == 0, before.stderr
        before_state = json.loads(before.stdout)
        assert before_state["display"] == "none"
        capture = subprocess.run(
            [str(SNAPSHOT_HOOK), f"--url={url}"],
            cwd=output,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert capture.returncode == 0, capture.stderr
        replay = subprocess.run(
            [env["NODE_BINARY"], "-e", script, "replay"],
            cwd=output,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert replay.returncode == 0, replay.stderr
        after_state = json.loads(replay.stdout)
        assert after_state["profiles"] == before_state["profiles"]
        for rendered in after_state["rendered"]:
            assert rendered["heading"] == "Responsive archive"
            assert rendered["width"] == 80, rendered
            assert rendered["display"] != "none", rendered
            assert rendered["src"].startswith("data:image/svg+xml"), rendered
