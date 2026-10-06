"""Exercise the real hook against Chrome and a responsive page served over HTTP."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_PLUGIN_DIR,
    chrome_session,
    get_test_env,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")
PLUGIN_DIR = Path(__file__).parent.parent
HOOK = PLUGIN_DIR / "on_Snapshot__60_mobilesize.js"


def inspect_page(env, chrome_dir, prepare=False, width=1280, height=800, stall=False):
    result = subprocess.run(
        [
            env["NODE_BINARY"],
            "-e",
            """
const {connectToPage} = require(process.argv[1]);
(async () => {
  const {browser, page, targetId} = await connectToPage({
    chromeSessionDir: process.argv[2], waitForNavigationComplete: true,
  });
  try {
    if (process.argv[3] === 'true') {
      await page.setViewport({width:Number(process.argv[4]), height:Number(process.argv[5]), deviceScaleFactor:1});
      await page.evaluate(() => window.scrollTo({top:700, behavior:'instant'}));
    }
    if (process.argv[6] === 'true') {
      // A real busy renderer, not a mocked CDP timeout. Schedule after returning
      // so this client can disconnect while the hook encounters the stall.
      await page.evaluate(() => setTimeout(() => {
        const end = performance.now() + 20000;
        while (performance.now() < end) { /* busy page script */ }
      }, 0));
      console.log('{}');
      return;
    }
    console.log(JSON.stringify({targetId, ...await page.evaluate(() => ({
      width:innerWidth, height:innerHeight, dpr:devicePixelRatio, y:scrollY,
      timeOrigin:performance.timeOrigin, url:location.href,
      resources:performance.getEntriesByType('resource').map(entry => entry.name),
    }))}));
  } finally {browser.disconnect();}
})().catch(error => {console.error(error); process.exit(1);});
""",
            str(CHROME_PLUGIN_DIR / "chrome_utils.js"),
            str(chrome_dir),
            str(prepare).lower(),
            str(width),
            str(height),
            str(stall).lower(),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("width,height,busy", [(390, 844, False), (320, 600, True)])
def test_mobile_assets_load_without_reloading_and_desktop_state_is_restored(
    httpserver,
    tmp_path,
    width,
    height,
    busy,
):
    # Real responsive image selection plus a CSS media query. None of these
    # resources is loaded by the desktop viewport until the hook resizes it.
    polling = (
        "<script>setInterval(() => fetch('/desktop.svg', {cache:'no-store'}), 100)</script>"
        if busy
        else ""
    )
    httpserver.expect_request("/").respond_with_data(
        """<!doctype html><meta name="viewport" content="width=device-width">
<title>Responsive capture</title>
<style>body {margin:0; min-height:3000px}
@media(max-width:600px) {body {background-image:url('/mobile-background.svg')}}</style>
<picture><source media="(max-width:350px)" srcset="/small-mobile.svg">
<source media="(max-width:600px)" srcset="/mobile.svg">
<img src="/desktop.svg" width="80" height="80"></picture>"""
        + polling,
        content_type="text/html",
    )
    for name, color in (
        ("desktop", "blue"),
        ("mobile", "green"),
        ("small-mobile", "purple"),
        ("mobile-background", "red"),
    ):
        httpserver.expect_request(f"/{name}.svg").respond_with_data(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="{color}"/></svg>',
            content_type="image/svg+xml",
        )
    url = httpserver.url_for("/").replace("localhost", "127.0.0.1", 1)
    with chrome_session(tmp_path, test_url=url, timeout=45) as (_, _, chrome_dir, env):
        env = env | {
            "MOBILESIZE_WIDTH": str(width),
            "MOBILESIZE_HEIGHT": str(height),
            "MOBILESIZE_TIMEOUT": "16",
            "MOBILESIZE_WAIT": "1" if busy else "3",
        }
        before = inspect_page(env, chrome_dir, prepare=True)
        assert not any("/mobile" in resource for resource in before["resources"])
        result = subprocess.run(
            [str(HOOK), f"--url={url}"],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record is not None, result.stdout
        assert record["status"] == "succeeded", record
        assert f"{width}x{height}" in record["output_str"], record
        if busy:
            assert "network wait reached" in result.stderr
        after = inspect_page(env, chrome_dir)
        for key in ("targetId", "width", "height", "dpr", "y", "timeOrigin", "url"):
            assert after[key] == before[key], (key, before, after)
        assert (
            url + ("small-mobile.svg" if width == 320 else "mobile.svg")
            in after["resources"]
        )
        assert url + "mobile-background.svg" in after["resources"]
        assert not (chrome_dir.parent / "mobilesize").exists()
        # Do not widen an existing phone-sized viewport (or unnecessarily
        # disturb a viewport already equal to the requested width).
        already_sized = subprocess.run(
            [str(HOOK), f"--url={url}"],
            env=env | {"MOBILESIZE_WIDTH": "1280", "MOBILESIZE_HEIGHT": "800"},
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert already_sized.returncode == 0, already_sized.stderr
        record = parse_jsonl_output(already_sized.stdout)
        assert record is not None, already_sized.stdout
        assert record["status"] == "noresults"


def test_disabled_skips_without_chrome_session(tmp_path):
    env = get_test_env() | {"SNAP_DIR": str(tmp_path), "MOBILESIZE_ENABLED": "False"}
    result = subprocess.run(
        [str(HOOK), "--url=https://example.com"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record is not None, result.stdout
    assert record["status"] == "skipped"


def test_missing_session_fails(tmp_path):
    result = subprocess.run(
        [str(HOOK), "--url=https://example.com"],
        env=get_test_env() | {"SNAP_DIR": str(tmp_path)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    record = parse_jsonl_output(result.stdout)
    assert record is not None, result.stdout
    assert record["status"] == "failed"


@pytest.mark.parametrize("original_width", [320, 390])
def test_height_breakpoint_at_phone_width(httpserver, tmp_path, original_width):
    httpserver.expect_request("/").respond_with_data(
        """<!doctype html><style>body {min-height:3000px}
@media(max-height:650px) {body {background-image:url('/short.svg')}}</style>""",
        content_type="text/html",
    )
    httpserver.expect_request("/short.svg").respond_with_data(
        '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>',
        content_type="image/svg+xml",
    )
    url = httpserver.url_for("/").replace("localhost", "127.0.0.1", 1)
    with chrome_session(tmp_path, test_url=url) as (_, _, chrome_dir, env):
        before = inspect_page(env, chrome_dir, prepare=True, width=original_width)
        assert url + "short.svg" not in before["resources"]
        result = subprocess.run(
            [str(HOOK), f"--url={url}"],
            env=env | {"MOBILESIZE_HEIGHT": "600", "MOBILESIZE_TIMEOUT": "16"},
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "succeeded", result.stdout
        assert f"{original_width}x600" in record["output_str"]
        after = inspect_page(env, chrome_dir)
        assert url + "short.svg" in after["resources"]
        for key in ("width", "height", "dpr", "y", "timeOrigin", "targetId"):
            assert after[key] == before[key], (key, before, after)
        assert not (chrome_dir.parent / "mobilesize").exists()


@pytest.mark.parametrize(
    "settings",
    [
        {"MOBILESIZE_WIDTH": "0"},
        {"MOBILESIZE_HEIGHT": "0"},
        {"MOBILESIZE_WAIT": "0"},
        {"MOBILESIZE_WAIT": "11"},
        {"MOBILESIZE_TIMEOUT": "15", "MOBILESIZE_WAIT": "10"},
    ],
)
def test_invalid_config_fails_before_connecting(tmp_path, settings):
    result = subprocess.run(
        [str(HOOK), "--url=https://example.com"],
        env=get_test_env() | {"SNAP_DIR": str(tmp_path)} | settings,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "failed", result.stdout
    assert "Invalid MOBILESIZE" in record["output_str"]
    assert not (tmp_path / "mobilesize").exists()


def test_timeout_bounds_a_stalled_renderer(httpserver, tmp_path):
    httpserver.expect_request("/").respond_with_data(
        "<!doctype html><body style='min-height:3000px'>Busy renderer</body>",
        content_type="text/html",
    )
    url = httpserver.url_for("/").replace("localhost", "127.0.0.1", 1)
    with chrome_session(tmp_path, test_url=url) as (_, _, chrome_dir, env):
        before = inspect_page(env, chrome_dir, prepare=True)
        inspect_page(env, chrome_dir, stall=True)
        started = time.monotonic()
        result = subprocess.run(
            [str(HOOK), f"--url={url}"],
            env=env | {"MOBILESIZE_TIMEOUT": "16"},
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=18,
        )
        elapsed = time.monotonic() - started
        assert result.returncode != 0, result.stdout
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "failed", result.stdout
        assert "MOBILESIZE_TIMEOUT exceeded" in record["output_str"]
        assert 10 <= elapsed < 18, elapsed
        after = inspect_page(env, chrome_dir)
        for key in ("width", "height", "dpr", "y", "timeOrigin", "targetId"):
            assert after[key] == before[key], (key, before, after)
