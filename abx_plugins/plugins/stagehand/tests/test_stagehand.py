"""Real stagehand snapshot hooks without ArchiveBox."""

import os
from zipfile import ZipFile
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from abx_plugins.plugins.chrome.tests.browser_hook_helpers import (
    assert_unconfigured_hook,
    read_config_macro,
    assert_challenge_capture,
    assert_script_capture,
)

PLUGIN = Path(__file__).parents[1]
HOOK = PLUGIN / "on_Snapshot__31_stagehand.js"


def test_browser_environment_preserves_installed_package(ensure_chrome_test_prereqs):
    from abx_plugins.plugins.base.utils import load_required_binary_from_config
    from abx_plugins.plugins.stagehand.runtime import browser_environment

    binary = load_required_binary_from_config(
        "stagehand",
        PLUGIN / "config.json",
        install=True,
    )
    original = Path(binary.loaded_abspath).resolve().parent
    archive = "stagehand/stagehand-ext-v4.0.0rc56-f3179ab9.zip"
    before = {
        name: (original / name).read_bytes()
        for name in (
            "index.ts",
            "package.json",
            "stagehand/client.ts",
            "stagehand/runtime.ts",
            archive,
        )
    }
    environment = browser_environment(binary)
    prepared = Path(environment["ARCHIVEBOX_STAGEHAND_MODULE"]).parent
    assert prepared != original
    assert browser_environment(binary) == environment
    assert all(
        (original / name).read_bytes() == content for name, content in before.items()
    )
    assert (prepared / "index.ts").read_bytes() == before["index.ts"]
    assert (prepared / "stagehand/client.ts").read_bytes() == before[
        "stagehand/client.ts"
    ]
    with ZipFile(original / archive) as source, ZipFile(prepared / archive) as copy:
        assert set(source.namelist()) == set(copy.namelist())
        assert {
            name for name in source.namelist() if source.read(name) != copy.read(name)
        } == {"stagehand-modcdp/background.js"}


def test_unconfigured_hook(tmp_path):
    assert_unconfigured_hook(HOOK, tmp_path)


def test_script_capture(tmp_path, httpserver, ensure_chrome_test_prereqs):
    assert_script_capture(
        HOOK,
        'if (await page.evaluate({expression: "window.driverToken"}) !== "page-owned") throw new Error("Wrong world");\nawait page.evaluate({expression: "document.querySelector(\'#prepare\').click()"});',
        tmp_path,
        httpserver,
    )


def test_native_startup_events_and_latency(
    tmp_path,
    httpserver,
    ensure_chrome_test_prereqs,
):
    assert_script_capture(
        HOOK,
        """
const upstream = client.runtime.upstream;
const state = await upstream.send('Runtime.evaluate', {
  expression: '({ready:globalThis.__stagehand_modcdp_ready, finished:globalThis.__stagehandStartupTimings.defineBackgroundEnd})',
  returnByValue: true,
}, upstream.serviceWorkerSessionId);
if (!state.result.value.ready || !state.result.value.finished)
  throw new Error('Stagehand service worker did not finish startup');
const received = [];
const timings = [];
client.on('PageEvaluateEvent', event => received.push(event));
for (let i = 0; i < 3; i++) {
  const started = performance.now();
  if (await page.evaluate({expression:'document.title'}) !== 'Unprepared')
    throw new Error('Wrong native evaluation result');
  const elapsed = performance.now() - started;
  timings.push(elapsed);
  if (elapsed > 2000)
    throw new Error('Native evaluation waited for a missing keepalive listener');
}
if (!received.length || received.some(event => event.event_type !== 'PageEvaluateEvent'))
  throw new Error('Native typed events were not delivered');
await (await import('node:fs/promises')).writeFile(config.SNAP_DIR + '/native-timings.json', JSON.stringify(timings));
await page.evaluate({expression: "document.querySelector('#prepare').click()"});
""",
        tmp_path,
        httpserver,
    )


def test_native_keyboard_input(tmp_path, httpserver, ensure_chrome_test_prereqs):
    assert_script_capture(
        HOOK,
        """
await page.evaluate({expression: "document.querySelector('#prepare').focus()"});
await page.sendCdp({method:'Input.dispatchKeyEvent', params:{type:'keyDown', key:'Enter', code:'Enter', windowsVirtualKeyCode:13, text:'\\r', unmodifiedText:'\\r'}});
await page.sendCdp({method:'Input.dispatchKeyEvent', params:{type:'keyUp', key:'Enter', code:'Enter', windowsVirtualKeyCode:13}});
""",
        tmp_path,
        httpserver,
    )


def test_native_agent_entrypoint(tmp_path, httpserver, ensure_chrome_test_prereqs):
    assert_script_capture(
        HOOK,
        """
const {createRequire} = await import('node:module');
const require = createRequire(process.argv[1]);
const {spawnSync} = await import('node:child_process');
const result = spawnSync(process.execPath, [require.resolve('./agent.js')], {
  env: {...process.env, ARCHIVEBOX_STAGEHAND_MODULE:require.resolve('shalpha'),
    ARCHIVEBOX_BROWSER_CDP_URL:client.options.cdp_url,
    ARCHIVEBOX_BROWSER_TARGET_ID:page.targetId},
  input: String.raw`
const upstream = client.runtime.upstream;
const state = await upstream.send('Runtime.evaluate', {
  expression:'globalThis.__stagehand_modcdp_ready', returnByValue:true,
}, upstream.serviceWorkerSessionId);
if (state.result.value !== true) throw new Error('Agent loaded uncorrected Stagehand');
await page.evaluate({expression:"document.querySelector('#prepare').focus()"});
await page.sendCdp({method:'Input.dispatchKeyEvent',params:{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,text:'\\r',unmodifiedText:'\\r'}});
await page.sendCdp({method:'Input.dispatchKeyEvent',params:{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13}});
`, encoding:'utf8', timeout:30000,
});
if (result.status !== 0) throw new Error(result.stderr || String(result.error));
""",
        tmp_path,
        httpserver,
    )


def test_prompt_capture(tmp_path, httpserver, ensure_chrome_test_prereqs):
    if os.environ.get("OPENAI_API_KEY"):
        model = "openai/gpt-6.1-sol"
    else:
        assert os.environ.get("ANTHROPIC_API_KEY"), (
            "Set OPENAI_API_KEY or ANTHROPIC_API_KEY for the real agent test"
        )
        model = "anthropic/claude-haiku-4-5-20251001"
    assert_script_capture(
        HOOK,
        "",
        tmp_path,
        httpserver,
        {
            "STAGEHAND_PROMPT": "Click the Prepare page button, confirm the page title becomes Prepared, then finish.",
            "STAGEHAND_MODEL": model,
            "STAGEHAND_TIMEOUT": "120",
        },
    )


def test_script_failure_preserves_tab(tmp_path, httpserver, ensure_chrome_test_prereqs):
    assert_script_capture(
        HOOK,
        'throw new Error("Requested failure");',
        tmp_path,
        httpserver,
        expected_status="failed",
        expected_title="Unprepared",
    )


def test_ten_challenges(tmp_path, ensure_chrome_test_prereqs):
    assert_challenge_capture(HOOK, tmp_path)


def test_saved_files(tmp_path, httpserver, ensure_chrome_test_prereqs):
    assert_script_capture(
        HOOK,
        'await (await import("node:fs/promises")).writeFile(outputDir + "/page.html", "<!DOCTYPE html>" + await page.evaluate({expression: "document.documentElement.outerHTML"}));',
        tmp_path,
        httpserver,
        expected_title="Unprepared",
        saved_file=True,
    )


@pytest.mark.parametrize(
    "domain, status, title",
    [
        ("example.com", "skipped", "Unprepared"),
        (None, "succeeded", "Prepared"),
    ],
)
def test_domain_filtered_script(
    tmp_path,
    httpserver,
    ensure_chrome_test_prereqs,
    domain,
    status,
    title,
):
    domain = domain or urlsplit(httpserver.url_for("/")).hostname
    script = read_config_macro(HOOK, "+ Domain guard").replace("example.com", domain)
    assert_script_capture(
        HOOK,
        script
        + "await page.evaluate({expression: \"document.querySelector('#prepare').click()\"});",
        tmp_path,
        httpserver,
        expected_status=status,
        expected_title=title,
        expected_output=f"Domain not selected: {urlsplit(httpserver.url_for('/')).hostname}"
        if status == "skipped"
        else None,
    )


def test_domain_prompt_uses_agent_validation(
    tmp_path,
    httpserver,
    ensure_chrome_test_prereqs,
):
    script = read_config_macro(HOOK, "+ Domain prompts")
    script = script.replace("example.com", urlsplit(httpserver.url_for("/")).hostname)
    script = script.replace("__VALUE__", '"Click the Prepare page button."')
    assert_script_capture(
        HOOK,
        script,
        tmp_path,
        httpserver,
        {"STAGEHAND_MODEL": ""},
        expected_status="failed",
        expected_title="Unprepared",
        expected_output="An agent model in provider/model format is required",
    )
