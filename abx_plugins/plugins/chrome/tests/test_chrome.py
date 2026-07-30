"""
Integration tests for chrome plugin

Tests verify:
1. Chromium install via @puppeteer/browsers
2. Verify deps with abxpkg
3. Chrome hooks exist
4. Chromium launches at crawl level
5. Tab creation at snapshot level
6. Tab navigation works
7. Tab cleanup on SIGTERM
8. Chromium cleanup on crawl end

NOTE: Extension tests use Chromium/Canary and load unpacked extensions via CDP
before snapshot tabs are created.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from werkzeug.wrappers import Response

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_LAUNCH_HOOK,
    CHROME_CRAWL_WAIT_HOOK,
    CHROME_WAIT_HOOK,
    CHROME_NAVIGATE_HOOK,
    CHROME_SNAPSHOT_LAUNCH_HOOK,
    CHROME_TAB_HOOK,
    CHROME_UTILS,
    LoggedPopen,
    _call_chrome_utils,
    close_target_and_wait_destroyed,
    chrome_session,
    create_target_via_cdp,
    fetch_devtools_targets,
    get_extensions_dir,
    get_cookies_via_cdp,
    get_test_env,
    is_pid_alive,
    kill_chrome,
    kill_chromium_session,
    launch_chromium_session,
    launch_snapshot_tab,
    port_from_cdp_url,
    wait_for_extensions_metadata,
    wait_for_chrome_session_state,
    write_browser_metadata,
)
from abx_plugins.plugins.base.testing import (
    assert_isolated_snapshot_env,
    install_required_binary_from_config,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

TEST_EXTENSION_NAME = "ublock"
TEST_EXTENSION_WEBSTORE_ID = "ddkjiahejlhfcafbddmgiahcphecmpfh"


@dataclass
class _ConcurrentChromeSession:
    snapshot_id: str
    chrome_dir: Path
    env: dict[str, str]
    launch_process: subprocess.Popen[str]
    tab_process: subprocess.Popen[str] | None = None
    chrome_pid: int | None = None


def test_acquire_session_lock_creates_missing_parent_dir(tmp_path):
    lock_file = tmp_path / "missing" / "nested" / ".chrome-launch.lock"
    script = (
        f"const chromeUtils = require({json.dumps(str(CHROME_UTILS))});\n"
        "(async () => {\n"
        "  const release = await chromeUtils.acquireSessionLock(process.argv[1]);\n"
        "  release();\n"
        "})().catch((error) => {\n"
        "  console.error(error);\n"
        "  process.exit(1);\n"
        "});\n"
    )
    env = {**os.environ, **get_test_env()}
    result = subprocess.run(
        [env["NODE_BINARY"], "-e", script, str(lock_file)],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert lock_file.parent.is_dir()
    assert not lock_file.exists()


def _probe_browser_page_via_cdp(cdp_url: str, env: dict) -> dict:
    script = (
        f"const chromeUtils = require({json.dumps(str(CHROME_UTILS))});\n"
        "const puppeteer = chromeUtils.resolvePuppeteerModule();\n"
        "(async () => {\n"
        "  const browser = await puppeteer.connect({ browserWSEndpoint: process.argv[1], defaultViewport: null });\n"
        "  try {\n"
        "    const pages = await browser.pages();\n"
        "    const page = pages.find(candidate => candidate.url() === 'about:blank') || pages[0];\n"
        "    if (!page) {\n"
        "      throw new Error('No page available in browser');\n"
        "    }\n"
        "    process.stdout.write(JSON.stringify({\n"
        "      url: page.url(),\n"
        "      title: await page.title(),\n"
        "      targetId: page.target()._targetId || page.target()._targetInfo?.targetId || null,\n"
        "    }));\n"
        "  } finally {\n"
        "    await browser.disconnect();\n"
        "  }\n"
        "})().catch((error) => {\n"
        "  console.error(error);\n"
        "  process.exit(1);\n"
        "});\n"
    )
    result = subprocess.run(
        [env["NODE_BINARY"], "-e", script, cdp_url],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, (
        f"Failed to probe browser page via CDP: {result.stderr}\nStdout: {result.stdout}"
    )
    return json.loads(result.stdout.strip())


def _cleanup_session_artifacts(
    session_dir: Path,
    env: dict,
    *,
    require_target_id: bool = False,
) -> dict:
    script = """
const path = require('path');
const utils = require(process.argv[1]);
const sessionDir = process.argv[2];
const requireTargetId = process.argv[3] === 'true';
(async () => {
  const result = await utils.cleanupStaleChromeSessionArtifacts(sessionDir, { requireTargetId, probeTimeoutMs: 250 });
  const payload = {
    hasArtifacts: result.hasArtifacts,
    stale: result.stale,
    reason: result.reason,
    cleanedFiles: result.cleanedFiles.map(filePath => path.basename(filePath)),
  };
  process.stdout.write(JSON.stringify(payload));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
    result = subprocess.run(
        [
            env["NODE_BINARY"],
            "-e",
            script,
            str(CHROME_UTILS),
            str(session_dir),
            str(require_target_id).lower(),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    assert result.returncode == 0, (
        f"cleanupStaleChromeSessionArtifacts failed: {result.stderr}\nStdout: {result.stdout}"
    )
    return json.loads(result.stdout.strip())


def _assert_snapshot_page_state_cleared(snapshot_chrome_dir: Path) -> None:
    for file_name in [
        "target_id.txt",
        "url.txt",
        "navigation.json",
    ]:
        assert not (snapshot_chrome_dir / file_name).exists(), (
            f"{file_name} should be removed from snapshot chrome dir during teardown"
        )


def _assert_snapshot_browser_state_cleared(snapshot_chrome_dir: Path) -> None:
    for file_name in [
        "cdp_url.txt",
        "chrome.pid",
        "browser.json",
    ]:
        assert not (snapshot_chrome_dir / file_name).exists(), (
            f"{file_name} should be removed from snapshot chrome dir during browser teardown"
        )


def _assert_snapshot_chrome_state_cleared(snapshot_chrome_dir: Path) -> None:
    _assert_snapshot_page_state_cleared(snapshot_chrome_dir)
    _assert_snapshot_browser_state_cleared(snapshot_chrome_dir)


def test_cleanup_chrome_profile_lock_files_removes_all_stale_profile_locks(tmp_path):
    profile_dir = tmp_path / "chrome_profile"
    profile_dir.mkdir()
    for file_name in [
        "SingletonLock",
        "SingletonSocket",
        "SingletonCookie",
        "DevToolsActivePort",
    ]:
        (profile_dir / file_name).write_text("stale")

    script = """
const path = require('path');
const utils = require(process.argv[1]);
const cleaned = utils.cleanupChromeProfileLockFiles(process.argv[2], { quiet: true });
process.stdout.write(JSON.stringify(cleaned.map(filePath => path.basename(filePath)).sort()));
"""
    env = {**os.environ, **get_test_env()}
    result = subprocess.run(
        [env["NODE_BINARY"], "-e", script, str(CHROME_UTILS), str(profile_dir)],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "DevToolsActivePort",
        "SingletonCookie",
        "SingletonLock",
        "SingletonSocket",
    ]
    assert not any(profile_dir.iterdir())


def test_cleanup_chrome_profile_lock_files_removes_dangling_symlink_locks(tmp_path):
    profile_dir = tmp_path / "chrome_profile"
    profile_dir.mkdir()
    (profile_dir / "SingletonLock").symlink_to("old-container-46929")
    (profile_dir / "SingletonSocket").symlink_to("/tmp/missing-chrome-socket")
    (profile_dir / "SingletonCookie").symlink_to("5206236564510194239")

    script = """
const path = require('path');
const utils = require(process.argv[1]);
const cleaned = utils.cleanupChromeProfileLockFiles(process.argv[2], { quiet: true });
process.stdout.write(JSON.stringify(cleaned.map(filePath => path.basename(filePath)).sort()));
"""
    env = {**os.environ, **get_test_env()}
    result = subprocess.run(
        [env["NODE_BINARY"], "-e", script, str(CHROME_UTILS), str(profile_dir)],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "SingletonCookie",
        "SingletonLock",
        "SingletonSocket",
    ]
    assert not any(profile_dir.iterdir())


def test_chrome_user_data_dir_defaults_to_persona_chrome_profile(tmp_path):
    personas_dir = tmp_path / "personas"
    script = """
const chromeUtils = require(process.argv[1]);
const baseUtils = require(process.argv[2]);
const configPath = process.argv[3];
const config = baseUtils.loadConfig(configPath);
const options = chromeUtils.resolveChromeLaunchOptions(config);
process.stdout.write(options.CHROME_USER_DATA_DIR);
"""
    env = os.environ.copy()
    env.update(get_test_env())
    env["PERSONAS_DIR"] = str(personas_dir)
    env.pop("ACTIVE_PERSONA", None)
    env.pop("CHROME_USER_DATA_DIR", None)

    result = subprocess.run(
        [
            env["NODE_BINARY"],
            "-e",
            script,
            str(CHROME_UTILS),
            str(CHROME_UTILS.parent.parent / "base" / "utils.js"),
            str(CHROME_UTILS.parent / "config.json"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(personas_dir / "Default" / "chrome_profile")


def _install_test_extension(extensions_dir: Path, env: dict[str, str]) -> dict:
    """Install the shipped uBlock plugin through its real abxpkg provider."""
    plugin_dir = CHROME_UTILS.parent.parent / TEST_EXTENSION_NAME
    loaded = install_required_binary_from_config(
        plugin_dir,
        TEST_EXTENSION_NAME,
        env=env,
    )
    assert loaded.loaded_abspath is not None
    manifest_path = Path(loaded.loaded_abspath)
    assert manifest_path.is_file(), manifest_path
    cache_file = extensions_dir / f"{TEST_EXTENSION_NAME}.extension.json"
    assert cache_file.is_file(), cache_file
    cache_data = json.loads(cache_file.read_text())
    assert cache_data["webstore_id"] == TEST_EXTENSION_WEBSTORE_ID
    return cache_data


def _probe_current_snapshot_page(chrome_session_dir: Path, env: dict) -> dict:
    base_utils = CHROME_UTILS.parent.parent / "base" / "utils.js"
    script = """
const { ensureNodeModuleResolution } = require(process.argv[1]);
ensureNodeModuleResolution(module);
const utils = require(process.argv[2]);
const chromeSessionDir = process.argv[3];
function resolvePuppeteer() {
  for (const moduleName of ['puppeteer-core', 'puppeteer']) {
    try {
      return require(moduleName);
    } catch (error) {}
  }
  throw new Error('Missing puppeteer dependency (need puppeteer-core or puppeteer)');
}
const puppeteer = resolvePuppeteer();
(async () => {
  const { browser, page, targetId } = await utils.connectToPage({
    chromeSessionDir,
    timeoutMs: 10000,
    waitForNavigationComplete: true,
    postLoadDelayMs: 200,
    puppeteer,
  });
  const title = await page.title() || await page.evaluate(() => {
    return document.title || document.querySelector('h1')?.textContent?.trim() || '';
  });
  const payload = {
    title,
    url: page.url(),
    targetId,
    actualTargetId: utils.getTargetIdFromPage(page),
    bodyText: await page.evaluate(() => document.body?.innerText || ''),
  };
  process.stdout.write(JSON.stringify(payload));
  await browser.disconnect();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
    result = subprocess.run(
        [
            env["NODE_BINARY"],
            "-e",
            script,
            str(base_utils),
            str(CHROME_UTILS),
            str(chrome_session_dir),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, (
        f"snapshot page probe should succeed:\nStdout: {result.stdout}\nStderr: {result.stderr}"
    )
    return json.loads(result.stdout)


def test_load_cached_extension_uses_runtime_browser_target():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        env = _isolated_test_env(
            tmpdir,
        )
        extensions_dir = Path(get_extensions_dir(env=env))
        cached_ext = _install_test_extension(extensions_dir, env)
        output_dir = tmpdir_path / "chrome"
        script = r"""
const chromeUtils = require(process.argv[1]);
const outputDir = process.argv[2];
const extensionJson = process.argv[3];

(async () => {
  const extension = JSON.parse(extensionJson);
  const binary = process.env.CHROME_BINARY;
  const result = await chromeUtils.launchChromium({
    binary,
    outputDir,
    enableExtensionDebugging: true,
  });
  if (!result.success) {
    throw new Error(result.error || 'Chrome launch failed');
  }
  const puppeteer = chromeUtils.resolvePuppeteerModule();
  const browser = await chromeUtils.connectToBrowserEndpoint(puppeteer, result.cdpUrl, {
    defaultViewport: null,
  });
  try {
    const extensions = [extension];
    await chromeUtils.loadUnpackedExtensionsIntoBrowser(browser, extensions, 30000);
    const targets = browser.targets()
      .filter(target => target.url().includes(extensions[0].id))
      .map(target => ({ type: target.type(), url: target.url() }));
    process.stdout.write(JSON.stringify({
      cdpUrl: result.cdpUrl,
      extension: extensions[0],
      targets,
    }));
  } finally {
    await browser.close().catch(() => {});
  }
})().catch((error) => {
  console.error(error && (error.stack || error.message || String(error)));
  process.exit(1);
});
"""
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                script,
                str(CHROME_UTILS),
                str(output_dir),
                json.dumps(cached_ext),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["extension"]["id"], payload
        target_url = payload["extension"].get("target_url", "")
        assert target_url.startswith(
            f"chrome-extension://{payload['extension']['id']}/",
        ), payload
        assert any(target.get("url") == target_url for target in payload["targets"])
        assert "load_error" not in payload["extension"], payload
        assert any(target["type"] == "service_worker" for target in payload["targets"])


def test_launch_chromium_replaces_static_user_agent_version_with_real_browser_version(
    tmp_path: Path,
):
    env = _isolated_test_env(
        tmp_path,
        CHROME_USER_AGENT="Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )
    output_dir = tmp_path / "chrome"
    script = r"""
const { execFileSync } = require('child_process');
  const chromeUtils = require(process.argv[1]);
  const outputDir = process.argv[2];

(async () => {
  const binary = process.env.CHROME_BINARY;
  const versionOutput = execFileSync(binary, ['--version'], { encoding: 'utf8' }).trim();
  const expectedVersion = chromeUtils.parseChromiumUserAgentVersion(versionOutput);
  const result = await chromeUtils.launchChromium({
    binary,
    outputDir,
  });
  if (!result.success) {
    throw new Error(result.error || 'Chrome launch failed');
  }
  const puppeteer = chromeUtils.resolvePuppeteerModule();
  const browser = await chromeUtils.connectToBrowserEndpoint(puppeteer, result.cdpUrl, {
    defaultViewport: null,
  });
  try {
    const page = await browser.newPage();
    const userAgent = await page.evaluate(() => navigator.userAgent);
    process.stdout.write(JSON.stringify({ expectedVersion, userAgent }));
  } finally {
    await browser.close().catch(() => {});
  }
})().catch((error) => {
  console.error(error && (error.stack || error.message || String(error)));
  process.exit(1);
});
"""
    result = subprocess.run(
        [
            env["NODE_BINARY"],
            "-e",
            script,
            str(CHROME_UTILS),
            str(output_dir),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["expectedVersion"]
    assert f"Chrome/{payload['expectedVersion']}" in payload["userAgent"]
    assert "Chrome/131.0.0.0" not in payload["userAgent"]


def _cleanup_launch_process(
    chrome_launch_process: subprocess.Popen[str] | None,
    chrome_dir: Path,
) -> None:
    if chrome_launch_process is not None:
        kill_chromium_session(chrome_launch_process, chrome_dir)


def _launch_keepalive_local_provider_browser(
    tmpdir: str | Path,
    *,
    crawl_dir_name: str,
) -> tuple[Path, Path, dict, str, int]:
    provider_dir = Path(tmpdir) / crawl_dir_name
    provider_dir.mkdir()
    provider_chrome_dir = provider_dir / "chrome"
    provider_chrome_dir.mkdir()

    provider_env = _isolated_test_env(
        tmpdir,
        CRAWL_DIR=str(provider_dir),
        PERSONAS_DIR=str(provider_dir / "personas"),
        CHROME_HEADLESS="true",
        CHROME_KEEPALIVE="true",
    )
    provider_launch = subprocess.run(
        [str(CHROME_LAUNCH_HOOK), f"--crawl-id={crawl_dir_name}"],
        cwd=str(provider_chrome_dir),
        capture_output=True,
        text=True,
        timeout=60,
        env=provider_env,
    )
    assert provider_launch.returncode == 0, (
        f"provider launch should succeed:\nStdout: {provider_launch.stdout}\nStderr: {provider_launch.stderr}"
    )

    cdp_url = (provider_chrome_dir / "cdp_url.txt").read_text().strip()
    pid = int((provider_chrome_dir / "chrome.pid").read_text().strip())
    assert cdp_url.startswith("ws://"), cdp_url
    assert is_pid_alive(pid), f"provider browser pid should be running: {pid}"
    return provider_dir, provider_chrome_dir, provider_env, cdp_url, pid


def _launch_snapshot_tab_allowing_optional_pid(
    *,
    snapshot_chrome_dir: Path,
    tab_env: dict[str, str],
    test_url: str,
    snapshot_id: str,
    crawl_id: str,
    require_pid: bool,
    timeout: int = 60,
) -> LoggedPopen:
    return launch_snapshot_tab(
        snapshot_chrome_dir=snapshot_chrome_dir,
        tab_env=tab_env,
        test_url=test_url,
        snapshot_id=snapshot_id,
        crawl_id=crawl_id,
        timeout=timeout,
        require_pid=require_pid,
    )


def _isolated_test_env(tmpdir: str | Path, **updates: str) -> dict:
    tmpdir = Path(tmpdir).resolve()
    env = get_test_env()

    snap_dir = tmpdir / "snap"
    crawl_dir = tmpdir / "crawl"
    personas_dir = tmpdir / "personas"
    chromewebstore_extensions_dir = tmpdir / "chromewebstore" / "extensions"
    home_dir = tmpdir / "home"
    xdg_config_home = home_dir / ".config"
    xdg_cache_home = home_dir / ".cache"
    xdg_data_home = home_dir / ".local" / "share"
    lib_dir = Path(env["ABXPKG_LIB_DIR"]).resolve()

    for path in (
        snap_dir,
        crawl_dir,
        personas_dir,
        chromewebstore_extensions_dir,
        home_dir,
        xdg_config_home,
        xdg_cache_home,
        xdg_data_home,
    ):
        path.mkdir(parents=True, exist_ok=True)

    env.update(
        {
            "SNAP_DIR": str(snap_dir),
            "CRAWL_DIR": str(crawl_dir),
            "ABXPKG_LIB_DIR": str(lib_dir),
            "PERSONAS_DIR": str(personas_dir),
            "ACTIVE_PERSONA": "Default",
            "ABXPKG_CHROMEWEBSTORE_ROOT": str(
                chromewebstore_extensions_dir.parent,
            ),
            "CHROMEWEBSTORE_EXTENSIONS_DIR": str(chromewebstore_extensions_dir),
            "HOME": str(home_dir),
            "XDG_CONFIG_HOME": str(xdg_config_home),
            "XDG_CACHE_HOME": str(xdg_cache_home),
            "XDG_DATA_HOME": str(xdg_data_home),
        },
    )
    for inherited_key in (
        "CHROME_DOWNLOADS_DIR",
        "CHROME_USER_DATA_DIR",
        "COOKIES_FILE",
    ):
        env.pop(inherited_key, None)
    env.update(updates)
    returncode, extensions_stdout, extensions_stderr = _call_chrome_utils(
        "getExtensionsDir",
        env=env,
        resolve_required_binary_env=False,
    )
    if returncode != 0 or not extensions_stdout.strip():
        raise RuntimeError(
            "chrome utils failed to resolve Chrome extensions dir: "
            f"{extensions_stderr or extensions_stdout}",
        )
    assert Path(extensions_stdout.strip()).resolve() == chromewebstore_extensions_dir
    assert_isolated_snapshot_env(env)
    return env


@pytest.fixture(scope="session", autouse=True)
def _ensure_chrome_prereqs(ensure_chromium_and_puppeteer_installed):
    """Make the shared Chrome install fixture autouse for this module."""
    return ensure_chromium_and_puppeteer_installed


def test_hook_scripts_exist():
    """Verify chrome hooks exist."""
    assert CHROME_LAUNCH_HOOK.exists(), f"Hook not found: {CHROME_LAUNCH_HOOK}"
    assert CHROME_TAB_HOOK.exists(), f"Hook not found: {CHROME_TAB_HOOK}"
    assert CHROME_NAVIGATE_HOOK.exists(), f"Hook not found: {CHROME_NAVIGATE_HOOK}"


def test_verify_chrome_available():
    """Verify Chrome is available via CHROME_BINARY env var."""
    chrome_binary = os.environ["CHROME_BINARY"]

    assert chrome_binary, "Chrome binary should be set by the abxpkg fixture"
    assert Path(chrome_binary).exists(), (
        f"Chrome binary should exist at {chrome_binary}"
    )

    # Verify it's actually a Chrome-family browser by checking version
    result = subprocess.run(
        [chrome_binary, "--version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Failed to get Chrome version: {result.stderr}"
    assert any(
        browser_name in result.stdout for browser_name in ("Chrome", "Chromium")
    ), f"Unexpected version output: {result.stdout}"
    script = "const utils = require(process.argv[1]); process.stdout.write(String(utils.isSupportedChromiumVersionOutput(process.argv[2])));"
    env = {**os.environ, **get_test_env()}
    support_result = subprocess.run(
        [env["NODE_BINARY"], "-e", script, str(CHROME_UTILS), result.stdout],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert support_result.returncode == 0, support_result.stderr
    assert support_result.stdout == "true", (
        f"Chromium >=149.0.0 is required for Extensions.loadUnpacked support, got: {result.stdout}"
    )


def test_chrome_launch_autodetects_sandbox_from_display():
    """A Linux host without a display should launch with the sandbox disabled."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir(parents=True)

        env = _isolated_test_env(
            tmpdir,
            CHROME_HEADLESS="true",
            CHROME_SANDBOX="true",
            DISPLAY="",
        )

        chrome_launch_process = None

        def assert_sandbox_args() -> None:
            cmd_contents = (chrome_dir / "cmd.sh").read_text()
            if sys.platform.startswith("linux"):
                assert "--no-sandbox" in cmd_contents, cmd_contents
                assert "--disable-setuid-sandbox" in cmd_contents, cmd_contents
            else:
                assert "--headless=new" in cmd_contents, cmd_contents

        try:
            chrome_launch_process, _cdp_url = launch_chromium_session(
                env,
                chrome_dir,
                "test-sandbox-disabled",
            )
        except RuntimeError:
            assert_sandbox_args()
        else:
            assert_sandbox_args()
        finally:
            if chrome_launch_process is not None:
                kill_chromium_session(chrome_launch_process, chrome_dir)


def test_chrome_launch_configures_downloads_via_cdp_not_profile_prefs():
    """CHROME_DOWNLOADS_DIR should be applied via CDP after launch, not by prewriting profile prefs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        chrome_dir = crawl_dir / "chrome"
        downloads_dir = Path(tmpdir) / "chrome_downloads"
        chrome_dir.mkdir(parents=True)
        downloads_dir.mkdir(parents=True)

        env = _isolated_test_env(
            tmpdir,
            CHROME_HEADLESS="true",
            CHROME_DOWNLOADS_DIR=str(downloads_dir),
        )
        user_data_dir = Path(env["PERSONAS_DIR"]) / "Default" / "chrome_profile"

        chrome_launch_process, _cdp_url = launch_chromium_session(
            env,
            chrome_dir,
            "test-downloads-via-cdp",
        )
        try:
            chrome_launch_process._stderr_handle.flush()
            stderr = chrome_launch_process._stderr_log.read_text(
                encoding="utf-8",
                errors="replace",
            )
            assert "Configured Chrome download directory via CDP" in stderr, stderr
            assert "Set Chrome download directory:" not in stderr, stderr

            prefs_path = user_data_dir / "Default" / "Preferences"
            if prefs_path.exists():
                prefs = json.loads(prefs_path.read_text())
                assert prefs.get("download", {}).get("default_directory") != str(
                    downloads_dir,
                ), prefs
        finally:
            kill_chromium_session(chrome_launch_process, chrome_dir)


def test_chrome_launch_and_tab_creation(chrome_test_url):
    """Integration test: Launch Chrome at crawl level and create tab at snapshot level."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        # Get test environment with NODE_MODULES_DIR set
        env = _isolated_test_env(
            tmpdir,
            CHROME_HEADLESS="true",
            CRAWL_DIR=str(crawl_dir),
        )

        chrome_launch_process, cdp_url = launch_chromium_session(
            env,
            chrome_dir,
            "test-crawl-123",
            timeout=45,
        )

        assert (chrome_dir / "cdp_url.txt").exists(), "cdp_url.txt should exist"
        assert (chrome_dir / "chrome.pid").exists(), "chrome.pid should exist"

        chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())

        assert cdp_url.startswith("ws://"), (
            f"CDP URL should be WebSocket URL: {cdp_url}"
        )
        assert chrome_pid > 0, "Chrome PID should be valid"

        page_probe = _probe_browser_page_via_cdp(cdp_url, env)
        assert page_probe["url"] == "about:blank", page_probe
        assert page_probe["targetId"], page_probe

        # Verify Chrome process is running
        try:
            os.kill(chrome_pid, 0)
        except OSError:
            raise AssertionError(f"Chrome process {chrome_pid} is not running")

        # Create snapshot directory and tab
        snapshot_dir = Path(tmpdir) / "snapshot1"
        snapshot_dir.mkdir()
        snapshot_chrome_dir = snapshot_dir / "chrome"
        snapshot_chrome_dir.mkdir()

        # Launch tab at snapshot level
        env["CRAWL_DIR"] = str(crawl_dir)
        env["SNAP_DIR"] = str(snapshot_dir)
        tab_process = launch_snapshot_tab(
            snapshot_chrome_dir=snapshot_chrome_dir,
            tab_env=env,
            test_url=chrome_test_url,
            snapshot_id="snap-123",
            crawl_id="test-crawl-123",
        )

        # Verify tab creation outputs
        assert (snapshot_chrome_dir / "cdp_url.txt").exists(), (
            "Snapshot cdp_url.txt should exist"
        )
        assert (snapshot_chrome_dir / "target_id.txt").exists(), (
            "target_id.txt should exist"
        )
        assert (snapshot_chrome_dir / "url.txt").exists(), "url.txt should exist"

        target_id = (snapshot_chrome_dir / "target_id.txt").read_text().strip()
        assert len(target_id) > 0, "Target ID should not be empty"

        # Cleanup: Kill Chrome and launch process
        tab_process.send_signal(signal.SIGTERM)
        tab_process.wait(timeout=10)
        _cleanup_launch_process(chrome_launch_process, chrome_dir)


def test_tab_hook_emits_single_success_result_and_stays_alive(chrome_test_url):
    """chrome_tab should emit one succeeded ArchiveResult and remain alive after startup."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        env = _isolated_test_env(
            tmpdir,
            CHROME_HEADLESS="true",
            CRAWL_DIR=str(crawl_dir),
        )

        chrome_launch_process, _cdp_url = launch_chromium_session(
            env,
            chrome_dir,
            "test-tab-single-result",
            timeout=45,
        )

        snapshot_dir = Path(tmpdir) / "snapshot1"
        snapshot_dir.mkdir()
        snapshot_chrome_dir = snapshot_dir / "chrome"
        snapshot_chrome_dir.mkdir()

        env["CRAWL_DIR"] = str(crawl_dir)
        env["SNAP_DIR"] = str(snapshot_dir)
        tab_process = None
        try:
            tab_process = launch_snapshot_tab(
                snapshot_chrome_dir=snapshot_chrome_dir,
                tab_env=env,
                test_url=chrome_test_url,
                snapshot_id="snap-single-result",
                crawl_id="test-tab-single-result",
            )

            stdout_log = snapshot_chrome_dir / "chrome_tab.stdout.log"
            assert tab_process.poll() is None
            stdout_lines = [
                line.strip()
                for line in stdout_log.read_text(
                    encoding="utf-8",
                    errors="replace",
                ).splitlines()
                if line.strip()
            ]
            archive_results = []
            for line in stdout_lines:
                if not line.startswith("{"):
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "ArchiveResult":
                    archive_results.append(record)

            assert len(archive_results) == 1, (
                f"chrome_tab should emit exactly one ArchiveResult on successful startup, got {archive_results}\n"
                f"Stdout log:\n{stdout_log.read_text(encoding='utf-8', errors='replace')}"
            )
            assert archive_results[0]["status"] == "succeeded", archive_results[0]
        finally:
            if tab_process is not None:
                tab_process.send_signal(signal.SIGTERM)
                tab_process.wait(timeout=10)
            _cleanup_launch_process(chrome_launch_process, chrome_dir)


def test_chrome_can_adopt_existing_cdp_url_without_local_pid(chrome_test_url):
    """CHROME_CDP_URL + CHROME_IS_LOCAL=false should reuse a browser without writing chrome.pid."""
    with tempfile.TemporaryDirectory() as tmpdir:
        provider_crawl_dir = Path(tmpdir) / "provider-crawl"
        provider_crawl_dir.mkdir()
        provider_chrome_dir = provider_crawl_dir / "chrome"
        provider_chrome_dir.mkdir()

        provider_env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(provider_crawl_dir),
            CHROME_HEADLESS="true",
        )
        provider_process = subprocess.Popen(
            [str(CHROME_LAUNCH_HOOK), "--crawl-id=provider-crawl"],
            cwd=str(provider_chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=provider_env,
        )

        try:
            provider_state = wait_for_chrome_session_state(
                provider_chrome_dir,
                env=provider_env,
                timeout_seconds=45,
                require_browser_ready=True,
            )
            assert provider_process.poll() is None
            provider_cdp_url = str(provider_state["cdpUrl"])
            provider_pid = int(provider_state["pid"])
            os.kill(provider_pid, 0)

            crawl_dir = Path(tmpdir) / "adopted-crawl"
            crawl_dir.mkdir()
            chrome_dir = crawl_dir / "chrome"
            chrome_dir.mkdir()

            adopt_env = _isolated_test_env(
                tmpdir,
                CRAWL_DIR=str(crawl_dir),
                CHROME_HEADLESS="true",
                CHROME_CDP_URL=provider_cdp_url,
                CHROME_IS_LOCAL="false",
                CHROME_KEEPALIVE="true",
            )

            launch = subprocess.run(
                [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-adopted-crawl"],
                cwd=str(chrome_dir),
                capture_output=True,
                text=True,
                timeout=60,
                env=adopt_env,
            )
            assert launch.returncode == 0, (
                f"Chrome adoption should succeed:\nStdout: {launch.stdout}\nStderr: {launch.stderr}"
            )
            assert (chrome_dir / "cdp_url.txt").exists(), (
                "cdp_url.txt should be published"
            )
            assert not (chrome_dir / "chrome.pid").exists(), (
                "chrome.pid should not be written for CHROME_IS_LOCAL=false"
            )
            assert (chrome_dir / "cdp_url.txt").read_text().strip() == provider_cdp_url

            wait_result = subprocess.run(
                [
                    str(CHROME_CRAWL_WAIT_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=test-adopted-snapshot",
                ],
                cwd=str(chrome_dir),
                capture_output=True,
                text=True,
                timeout=60,
                env=adopt_env,
            )
            assert wait_result.returncode == 0, (
                f"crawl wait should accept adopted session without pid:\nStdout: {wait_result.stdout}\nStderr: {wait_result.stderr}"
            )

            snapshot_dir = Path(tmpdir) / "snapshot-adopted"
            snapshot_dir.mkdir()
            snapshot_chrome_dir = snapshot_dir / "chrome"
            snapshot_chrome_dir.mkdir()

            tab_env = adopt_env | {
                "SNAP_DIR": str(snapshot_dir),
            }
            tab_process = launch_snapshot_tab(
                snapshot_chrome_dir=snapshot_chrome_dir,
                tab_env=tab_env,
                test_url=chrome_test_url,
                snapshot_id="snap-adopted-123",
                crawl_id="test-adopted-crawl",
                require_pid=False,
            )
            try:
                assert (snapshot_chrome_dir / "cdp_url.txt").exists()
                assert (snapshot_chrome_dir / "target_id.txt").exists()
                assert not (snapshot_chrome_dir / "chrome.pid").exists(), (
                    "snapshot chrome.pid should not be written for CHROME_IS_LOCAL=false"
                )

                wait_env = tab_env
                wait_result = subprocess.run(
                    [
                        str(CHROME_WAIT_HOOK),
                        f"--url={chrome_test_url}",
                        "--snapshot-id=snap-adopted-123",
                    ],
                    cwd=str(snapshot_chrome_dir),
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=wait_env,
                )
                assert wait_result.returncode == 0, (
                    f"snapshot wait should succeed for adopted session:\nStdout: {wait_result.stdout}\nStderr: {wait_result.stderr}"
                )
            finally:
                tab_process.send_signal(signal.SIGTERM)
                tab_process.wait(timeout=20)
        finally:
            provider_process.send_signal(signal.SIGTERM)
            provider_process.wait(timeout=20)


def test_crawl_isolation_external_cdp_keepalive_true_reinvocation_reuses_same_browser_without_closing_it(
    chrome_test_url,
):
    """crawl isolation + external CDP + keepalive=true should not close the same adopted browser on re-invocation in the same crawl dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (
            _provider_dir,
            provider_chrome_dir,
            _provider_env,
            provider_cdp_url,
            provider_pid,
        ) = _launch_keepalive_local_provider_browser(
            tmpdir,
            crawl_dir_name="provider-crawl-reinvoke",
        )

        adopted_dir = Path(tmpdir) / "adopted-crawl-reinvoke"
        adopted_dir.mkdir()
        adopted_chrome_dir = adopted_dir / "chrome"
        adopted_chrome_dir.mkdir()

        adopt_env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(adopted_dir),
            CHROME_HEADLESS="true",
            CHROME_CDP_URL=provider_cdp_url,
            CHROME_IS_LOCAL="false",
            CHROME_KEEPALIVE="true",
        )
        try:
            first_launch = subprocess.run(
                [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-external-reinvoke"],
                cwd=str(adopted_chrome_dir),
                capture_output=True,
                text=True,
                timeout=60,
                env=adopt_env,
            )
            assert first_launch.returncode == 0, (
                f"first adopted launch should succeed:\nStdout: {first_launch.stdout}\nStderr: {first_launch.stderr}"
            )
            assert (
                adopted_chrome_dir / "cdp_url.txt"
            ).read_text().strip() == provider_cdp_url
            assert not (adopted_chrome_dir / "chrome.pid").exists()
            assert is_pid_alive(provider_pid), (
                "provider browser should still be alive after first adopted launch"
            )

            second_launch = subprocess.run(
                [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-external-reinvoke"],
                cwd=str(adopted_chrome_dir),
                capture_output=True,
                text=True,
                timeout=60,
                env=adopt_env,
            )
            assert second_launch.returncode == 0, (
                f"second adopted launch in same crawl dir should succeed without closing the provider browser:\n"
                f"Stdout: {second_launch.stdout}\nStderr: {second_launch.stderr}"
            )
            assert (
                adopted_chrome_dir / "cdp_url.txt"
            ).read_text().strip() == provider_cdp_url
            assert not (adopted_chrome_dir / "chrome.pid").exists()
            assert is_pid_alive(provider_pid), (
                "provider browser should remain alive after re-invoking adopted keepalive launch in the same crawl dir"
            )

            crawl_wait = subprocess.run(
                [
                    str(CHROME_CRAWL_WAIT_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-external-reinvoke",
                ],
                cwd=str(adopted_chrome_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=adopt_env,
            )
            assert crawl_wait.returncode == 0, (
                f"crawl wait should still succeed after re-invocation:\nStdout: {crawl_wait.stdout}\nStderr: {crawl_wait.stderr}"
            )
        finally:
            if is_pid_alive(provider_pid):
                assert kill_chrome(provider_pid, str(provider_chrome_dir))
                assert not is_pid_alive(provider_pid), (
                    "manual cleanup should terminate adopted provider browser"
                )


def test_snapshot_isolation_launches_and_cleans_up_local_browser(chrome_test_url):
    """CHROME_ISOLATION=snapshot should launch from the snapshot launch hook and close on teardown."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        snapshot_dir = Path(tmpdir) / "snapshot"
        snapshot_dir.mkdir()
        snapshot_chrome_dir = snapshot_dir / "chrome"
        snapshot_chrome_dir.mkdir()

        tab_env = _isolated_test_env(tmpdir) | {
            "CRAWL_DIR": str(crawl_dir),
            "SNAP_DIR": str(snapshot_dir),
            "CHROME_HEADLESS": "true",
            "CHROME_ISOLATION": "snapshot",
        }
        extensions_dir = Path(get_extensions_dir(env=tab_env))
        cached_ext = _install_test_extension(extensions_dir, tab_env)

        launch_process = subprocess.Popen(
            [
                str(CHROME_SNAPSHOT_LAUNCH_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=snap-isolated-123",
                "--crawl-id=test-snapshot-isolation",
            ],
            cwd=str(snapshot_chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=tab_env,
        )

        wait_for_chrome_session_state(
            snapshot_chrome_dir,
            env=tab_env,
            timeout_seconds=60,
            require_browser_ready=True,
        )
        assert launch_process.poll() is None

        browser_file = snapshot_chrome_dir / "browser.json"
        assert browser_file.exists(), (
            "snapshot launch should publish browser metadata before tab setup"
        )
        browser_metadata = json.loads(browser_file.read_text())
        extension_entry = next(
            (
                entry
                for entry in browser_metadata.get("extensions", [])
                if entry.get("name") == TEST_EXTENSION_NAME
            ),
            None,
        )
        assert extension_entry is not None, browser_metadata
        assert "load_path" not in extension_entry, extension_entry
        assert extension_entry.get("unpacked_path") == cached_ext["unpacked_path"]
        assert Path(extension_entry["unpacked_path"]).is_dir(), extension_entry
        assert (Path(extension_entry["unpacked_path"]) / "manifest.json").is_file()

        tab_process = launch_snapshot_tab(
            snapshot_chrome_dir=snapshot_chrome_dir,
            tab_env=tab_env,
            test_url=chrome_test_url,
            snapshot_id="snap-isolated-123",
            crawl_id="test-snapshot-isolation",
            require_pid=True,
        )

        wait_result = subprocess.run(
            [
                str(CHROME_WAIT_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=snap-isolated-123",
            ],
            cwd=str(snapshot_chrome_dir),
            capture_output=True,
            text=True,
            timeout=60,
            env=tab_env,
        )
        assert wait_result.returncode == 0, (
            f"snapshot wait should succeed in snapshot isolation mode:\nStdout: {wait_result.stdout}\nStderr: {wait_result.stderr}"
        )

        navigate_result = subprocess.run(
            [
                str(CHROME_NAVIGATE_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=snap-isolated-123",
            ],
            cwd=str(snapshot_chrome_dir),
            capture_output=True,
            text=True,
            timeout=120,
            env=tab_env,
        )
        assert navigate_result.returncode == 0, (
            f"navigation should succeed in snapshot isolation mode:\nStdout: {navigate_result.stdout}\nStderr: {navigate_result.stderr}"
        )

        chrome_pid = int((snapshot_chrome_dir / "chrome.pid").read_text().strip())
        os.kill(chrome_pid, 0)
        assert (snapshot_chrome_dir / "navigation.json").exists()

        try:
            tab_process.send_signal(signal.SIGTERM)
            tab_process.wait(timeout=20)
            assert browser_file.exists(), (
                "tab cleanup must leave browser metadata for snapshot launch cleanup"
            )
        finally:
            launch_process.send_signal(signal.SIGTERM)
            launch_process.wait(timeout=20)

        with pytest.raises(OSError):
            os.kill(chrome_pid, 0)
        _assert_snapshot_chrome_state_cleared(snapshot_chrome_dir)


def test_concurrent_snapshot_isolation_loads_shared_extension_cache(chrome_test_url):
    """Concurrent snapshot browsers must load one shared abxpkg extension cache."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        base_env = _isolated_test_env(tmpdir) | {
            "CRAWL_DIR": str(crawl_dir),
            "CHROME_HEADLESS": "true",
            "CHROME_ISOLATION": "snapshot",
        }
        extensions_dir = Path(get_extensions_dir(env=base_env))
        cached_ext = _install_test_extension(extensions_dir, base_env)

        sessions: list[_ConcurrentChromeSession] = []
        for index in range(3):
            snapshot_dir = Path(tmpdir) / f"snapshot-{index}"
            chrome_dir = snapshot_dir / "chrome"
            chrome_dir.mkdir(parents=True)
            snapshot_id = f"snap-concurrent-isolated-{index}"
            env = base_env | {
                "SNAP_DIR": str(snapshot_dir),
                "PERSONAS_DIR": str(snapshot_dir / ".persona"),
            }
            stdout_handle = (chrome_dir / "snapshot_launch.stdout.log").open(
                "w+",
                encoding="utf-8",
            )
            stderr_handle = (chrome_dir / "snapshot_launch.stderr.log").open(
                "w+",
                encoding="utf-8",
            )
            launch_process = LoggedPopen(
                [
                    str(CHROME_SNAPSHOT_LAUNCH_HOOK),
                    f"--url={chrome_test_url}",
                    f"--snapshot-id={snapshot_id}",
                    "--crawl-id=test-concurrent-snapshot-isolation",
                ],
                cwd=str(chrome_dir),
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                env=env,
            )
            launch_process._stdout_handle = stdout_handle
            launch_process._stderr_handle = stderr_handle
            sessions.append(
                _ConcurrentChromeSession(
                    snapshot_id=snapshot_id,
                    chrome_dir=chrome_dir,
                    env=env,
                    launch_process=launch_process,
                ),
            )

        try:
            for session in sessions:
                wait_for_chrome_session_state(
                    session.chrome_dir,
                    env=session.env,
                    timeout_seconds=60,
                    require_browser_ready=True,
                )
                assert session.launch_process.poll() is None
                browser_metadata = json.loads(
                    (session.chrome_dir / "browser.json").read_text(),
                )
                extension_entry = next(
                    (
                        entry
                        for entry in browser_metadata.get("extensions", [])
                        if entry.get("name") == TEST_EXTENSION_NAME
                    ),
                    None,
                )
                assert extension_entry is not None, browser_metadata
                assert (
                    extension_entry.get("unpacked_path") == cached_ext["unpacked_path"]
                )
                assert extension_entry.get("id") == cached_ext["id"]
                assert "load_error" not in extension_entry, extension_entry
                session.chrome_pid = int(
                    (session.chrome_dir / "chrome.pid").read_text().strip(),
                )

            for session in sessions:
                session.tab_process = launch_snapshot_tab(
                    snapshot_chrome_dir=session.chrome_dir,
                    tab_env=session.env,
                    test_url=chrome_test_url,
                    snapshot_id=session.snapshot_id,
                    crawl_id="test-concurrent-snapshot-isolation",
                    require_pid=True,
                )
                wait_result = subprocess.run(
                    [
                        str(CHROME_WAIT_HOOK),
                        f"--url={chrome_test_url}",
                        f"--snapshot-id={session.snapshot_id}",
                    ],
                    cwd=str(session.chrome_dir),
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=session.env,
                )
                assert wait_result.returncode == 0, (
                    f"snapshot wait failed for {session.snapshot_id}:\n"
                    f"Stdout: {wait_result.stdout}\nStderr: {wait_result.stderr}"
                )
        finally:
            for session in sessions:
                tab_process = session.tab_process
                if tab_process is None:
                    continue
                try:
                    if tab_process.poll() is None:
                        tab_process.send_signal(signal.SIGTERM)
                        tab_process.wait(timeout=20)
                finally:
                    for attr in ("_stdout_handle", "_stderr_handle"):
                        handle = getattr(tab_process, attr, None)
                        if handle:
                            handle.close()
            for session in sessions:
                launch_process = session.launch_process
                try:
                    if launch_process.poll() is None:
                        launch_process.send_signal(signal.SIGTERM)
                        launch_process.wait(timeout=20)
                finally:
                    for attr in ("_stdout_handle", "_stderr_handle"):
                        handle = getattr(launch_process, attr, None)
                        if handle:
                            handle.close()

        for session in sessions:
            chrome_pid = session.chrome_pid
            assert chrome_pid is not None
            assert not is_pid_alive(chrome_pid)
            _assert_snapshot_chrome_state_cleared(session.chrome_dir)


def test_crawl_isolation_local_keepalive_true_keeps_browser_running_after_hook_exit(
    chrome_test_url,
):
    """crawl isolation + local browser + keepalive=true should leave Chrome running after the launch hook exits."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(crawl_dir),
            CHROME_HEADLESS="true",
            CHROME_KEEPALIVE="true",
        )

        launch = subprocess.run(
            [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-crawl-keepalive-true"],
            cwd=str(chrome_dir),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert launch.returncode == 0, (
            f"crawl launch should succeed with keepalive=true:\nStdout: {launch.stdout}\nStderr: {launch.stderr}"
        )
        chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())
        assert is_pid_alive(chrome_pid), (
            "Chrome should still be running after launch hook exits"
        )

        crawl_wait = subprocess.run(
            [
                str(CHROME_CRAWL_WAIT_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=snap-crawl-keepalive-true",
            ],
            cwd=str(chrome_dir),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert crawl_wait.returncode == 0, (
            f"crawl wait should still succeed after keepalive launch exits:\nStdout: {crawl_wait.stdout}\nStderr: {crawl_wait.stderr}"
        )

        snapshot_dir = Path(tmpdir) / "snapshot"
        snapshot_dir.mkdir()
        snapshot_chrome_dir = snapshot_dir / "chrome"
        snapshot_chrome_dir.mkdir()
        tab_env = env | {"SNAP_DIR": str(snapshot_dir)}
        tab_process = launch_snapshot_tab(
            snapshot_chrome_dir=snapshot_chrome_dir,
            tab_env=tab_env,
            test_url=chrome_test_url,
            snapshot_id="snap-crawl-keepalive-true",
            crawl_id="test-crawl-keepalive-true",
        )
        try:
            snapshot_wait = subprocess.run(
                [
                    str(CHROME_WAIT_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-crawl-keepalive-true",
                ],
                cwd=str(snapshot_chrome_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=tab_env,
            )
            assert snapshot_wait.returncode == 0, (
                f"snapshot wait should succeed after keepalive crawl launch exits:\nStdout: {snapshot_wait.stdout}\nStderr: {snapshot_wait.stderr}"
            )
        finally:
            tab_process.send_signal(signal.SIGTERM)
            tab_process.wait(timeout=20)

        assert is_pid_alive(chrome_pid), (
            "Chrome should remain alive after snapshot tab cleanup when crawl keepalive=true"
        )
        assert kill_chrome(chrome_pid, str(chrome_dir))
        assert not is_pid_alive(chrome_pid), (
            "manual cleanup should terminate keepalive browser"
        )


def test_snapshot_isolation_local_keepalive_true_keeps_browser_running_after_hook_exit(
    chrome_test_url,
):
    """snapshot isolation + local browser + keepalive=true should leave Chrome running after the launch hook exits."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        snapshot_dir = Path(tmpdir) / "snapshot"
        snapshot_dir.mkdir()
        snapshot_chrome_dir = snapshot_dir / "chrome"
        snapshot_chrome_dir.mkdir()

        env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(crawl_dir),
            SNAP_DIR=str(snapshot_dir),
            CHROME_HEADLESS="true",
            CHROME_ISOLATION="snapshot",
            CHROME_KEEPALIVE="true",
        )

        launch = subprocess.run(
            [
                str(CHROME_SNAPSHOT_LAUNCH_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=snap-local-keepalive-true",
                "--crawl-id=test-snapshot-keepalive-true",
            ],
            cwd=str(snapshot_chrome_dir),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert launch.returncode == 0, (
            f"snapshot launch should succeed with keepalive=true:\nStdout: {launch.stdout}\nStderr: {launch.stderr}"
        )
        chrome_pid = int((snapshot_chrome_dir / "chrome.pid").read_text().strip())
        assert is_pid_alive(chrome_pid), (
            "Chrome should still be running after snapshot launch exits"
        )

        tab_process = launch_snapshot_tab(
            snapshot_chrome_dir=snapshot_chrome_dir,
            tab_env=env,
            test_url=chrome_test_url,
            snapshot_id="snap-local-keepalive-true",
            crawl_id="test-snapshot-keepalive-true",
        )
        try:
            snapshot_wait = subprocess.run(
                [
                    str(CHROME_WAIT_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-local-keepalive-true",
                ],
                cwd=str(snapshot_chrome_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert snapshot_wait.returncode == 0, (
                f"snapshot wait should succeed for keepalive=true snapshot browser:\nStdout: {snapshot_wait.stdout}\nStderr: {snapshot_wait.stderr}"
            )
        finally:
            tab_process.send_signal(signal.SIGTERM)
            tab_process.wait(timeout=20)

        assert is_pid_alive(chrome_pid), (
            "Chrome should remain alive after snapshot tab cleanup when snapshot keepalive=true"
        )
        assert kill_chrome(chrome_pid, str(snapshot_chrome_dir))
        assert not is_pid_alive(chrome_pid), (
            "manual cleanup should terminate keepalive browser"
        )


def test_crawl_isolation_external_cdp_keepalive_false_closes_adopted_browser_on_cleanup(
    chrome_test_url,
):
    """crawl isolation + external CDP + keepalive=false should close the adopted browser on hook cleanup."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (
            _provider_dir,
            provider_chrome_dir,
            _provider_env,
            provider_cdp_url,
            provider_pid,
        ) = _launch_keepalive_local_provider_browser(
            tmpdir,
            crawl_dir_name="provider-crawl-keepalive",
        )

        adopted_dir = Path(tmpdir) / "adopted-crawl"
        adopted_dir.mkdir()
        adopted_chrome_dir = adopted_dir / "chrome"
        adopted_chrome_dir.mkdir()

        adopt_env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(adopted_dir),
            CHROME_HEADLESS="true",
            CHROME_CDP_URL=provider_cdp_url,
            CHROME_IS_LOCAL="false",
            CHROME_KEEPALIVE="false",
        )

        adopt_process = subprocess.Popen(
            [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-external-crawl-close"],
            cwd=str(adopted_chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=adopt_env,
        )
        try:
            try:
                wait_for_chrome_session_state(
                    adopted_chrome_dir,
                    env=adopt_env,
                    require_browser_ready=True,
                )
            except AssertionError as error:
                adopt_process.send_signal(signal.SIGTERM)
                stdout, stderr = adopt_process.communicate(timeout=20)
                raise AssertionError(
                    f"{error}\nAdopt hook stdout:\n{stdout}\nAdopt hook stderr:\n{stderr}",
                ) from error
            assert adopt_process.poll() is None, "adopted crawl launch exited early"

            assert (
                adopted_chrome_dir / "cdp_url.txt"
            ).read_text().strip() == provider_cdp_url
            assert not (adopted_chrome_dir / "chrome.pid").exists()
            assert is_pid_alive(provider_pid), (
                "provider browser should be alive before adopted cleanup"
            )

            crawl_wait = subprocess.run(
                [
                    str(CHROME_CRAWL_WAIT_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-external-crawl-close",
                ],
                cwd=str(adopted_chrome_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=adopt_env,
            )
            assert crawl_wait.returncode == 0, (
                f"crawl wait should succeed for adopted external browser:\nStdout: {crawl_wait.stdout}\nStderr: {crawl_wait.stderr}"
            )

            adopt_process.send_signal(signal.SIGTERM)
            adopt_process.wait(timeout=20)
            assert not is_pid_alive(provider_pid), (
                "adopted external browser should be closed when crawl keepalive=false hook shuts down"
            )
            assert not (adopted_chrome_dir / "cdp_url.txt").exists(), (
                "cdp_url.txt should be removed from crawl-owned chrome dir on teardown"
            )
            assert not (adopted_chrome_dir / "browser.json").exists(), (
                "browser.json should be removed from crawl-owned chrome dir on teardown"
            )
        finally:
            adopt_process.send_signal(signal.SIGTERM)
            adopt_process.wait(timeout=20)
            if is_pid_alive(provider_pid):
                kill_chrome(provider_pid, str(provider_chrome_dir))


def test_snapshot_isolation_external_cdp_keepalive_true_ignores_is_local_true_and_keeps_browser_running(
    chrome_test_url,
):
    """snapshot isolation + external CDP + keepalive=true should keep the adopted browser alive and treat CHROME_CDP_URL as external even if CHROME_IS_LOCAL=true."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (
            _provider_dir,
            provider_chrome_dir,
            _provider_env,
            provider_cdp_url,
            provider_pid,
        ) = _launch_keepalive_local_provider_browser(
            tmpdir,
            crawl_dir_name="provider-snapshot-keepalive",
        )

        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir(exist_ok=True)
        snapshot_dir = Path(tmpdir) / "snapshot"
        snapshot_dir.mkdir(exist_ok=True)
        snapshot_chrome_dir = snapshot_dir / "chrome"
        snapshot_chrome_dir.mkdir()

        env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(crawl_dir),
            SNAP_DIR=str(snapshot_dir),
            CHROME_HEADLESS="true",
            CHROME_ISOLATION="snapshot",
            CHROME_CDP_URL=provider_cdp_url,
            CHROME_IS_LOCAL="true",
            CHROME_KEEPALIVE="true",
        )

        launch = subprocess.run(
            [
                str(CHROME_SNAPSHOT_LAUNCH_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=snap-external-keepalive-true",
                "--crawl-id=test-external-snapshot-keepalive-true",
            ],
            cwd=str(snapshot_chrome_dir),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert launch.returncode == 0, (
            f"snapshot launch should succeed for external keepalive browser:\nStdout: {launch.stdout}\nStderr: {launch.stderr}"
        )
        assert (
            snapshot_chrome_dir / "cdp_url.txt"
        ).read_text().strip() == provider_cdp_url
        assert not (snapshot_chrome_dir / "chrome.pid").exists(), (
            "CHROME_CDP_URL should force external behavior even when CHROME_IS_LOCAL=true"
        )
        assert is_pid_alive(provider_pid), (
            "provider browser should still be alive after keepalive launch exits"
        )

        tab_process = _launch_snapshot_tab_allowing_optional_pid(
            snapshot_chrome_dir=snapshot_chrome_dir,
            tab_env=env,
            test_url=chrome_test_url,
            snapshot_id="snap-external-keepalive-true",
            crawl_id="test-external-snapshot-keepalive-true",
            require_pid=False,
        )
        try:
            snapshot_wait = subprocess.run(
                [
                    str(CHROME_WAIT_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-external-keepalive-true",
                ],
                cwd=str(snapshot_chrome_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert snapshot_wait.returncode == 0, (
                f"snapshot wait should succeed for external keepalive snapshot browser:\nStdout: {snapshot_wait.stdout}\nStderr: {snapshot_wait.stderr}"
            )
        finally:
            tab_process.send_signal(signal.SIGTERM)
            tab_process.wait(timeout=20)

        assert is_pid_alive(provider_pid), (
            "external provider browser should remain alive after snapshot tab cleanup when keepalive=true"
        )
        assert kill_chrome(provider_pid, str(provider_chrome_dir))
        assert not is_pid_alive(provider_pid), (
            "manual cleanup should terminate adopted provider browser"
        )


def test_snapshot_isolation_external_cdp_keepalive_false_closes_adopted_browser_on_cleanup(
    chrome_test_url,
):
    """snapshot isolation + external CDP + keepalive=false should close the adopted browser on hook cleanup."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (
            _provider_dir,
            provider_chrome_dir,
            _provider_env,
            provider_cdp_url,
            provider_pid,
        ) = _launch_keepalive_local_provider_browser(
            tmpdir,
            crawl_dir_name="provider-snapshot-close",
        )

        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir(exist_ok=True)
        snapshot_dir = Path(tmpdir) / "snapshot"
        snapshot_dir.mkdir(exist_ok=True)
        snapshot_chrome_dir = snapshot_dir / "chrome"
        snapshot_chrome_dir.mkdir()

        env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(crawl_dir),
            SNAP_DIR=str(snapshot_dir),
            CHROME_HEADLESS="true",
            CHROME_ISOLATION="snapshot",
            CHROME_CDP_URL=provider_cdp_url,
            CHROME_IS_LOCAL="false",
            CHROME_KEEPALIVE="false",
        )

        launch_process = subprocess.Popen(
            [
                str(CHROME_SNAPSHOT_LAUNCH_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=snap-external-keepalive-false",
                "--crawl-id=test-external-snapshot-keepalive-false",
            ],
            cwd=str(snapshot_chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        tab_process = None
        try:
            wait_for_chrome_session_state(
                snapshot_chrome_dir,
                env=env,
                require_browser_ready=True,
            )
            assert launch_process.poll() is None, "snapshot launch exited early"

            assert (
                snapshot_chrome_dir / "cdp_url.txt"
            ).read_text().strip() == provider_cdp_url
            assert not (snapshot_chrome_dir / "chrome.pid").exists()
            assert is_pid_alive(provider_pid), (
                "provider browser should be alive before snapshot cleanup"
            )

            tab_process = _launch_snapshot_tab_allowing_optional_pid(
                snapshot_chrome_dir=snapshot_chrome_dir,
                tab_env=env,
                test_url=chrome_test_url,
                snapshot_id="snap-external-keepalive-false",
                crawl_id="test-external-snapshot-keepalive-false",
                require_pid=False,
            )
            snapshot_wait = subprocess.run(
                [
                    str(CHROME_WAIT_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-external-keepalive-false",
                ],
                cwd=str(snapshot_chrome_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert snapshot_wait.returncode == 0, (
                f"snapshot wait should succeed for external browser before cleanup:\nStdout: {snapshot_wait.stdout}\nStderr: {snapshot_wait.stderr}"
            )

            tab_process.send_signal(signal.SIGTERM)
            tab_process.wait(timeout=20)
            tab_process = None
            _assert_snapshot_page_state_cleared(snapshot_chrome_dir)
            assert (
                snapshot_chrome_dir / "cdp_url.txt"
            ).read_text().strip() == provider_cdp_url
            browser_metadata = json.loads(
                (snapshot_chrome_dir / "browser.json").read_text(),
            )
            assert browser_metadata.get("ready") is True, (
                "tab teardown must leave browser metadata for the owning snapshot launch hook"
            )
            assert isinstance(browser_metadata.get("extensions"), list), (
                browser_metadata
            )
            assert not (snapshot_chrome_dir / "chrome.pid").exists()

            launch_process.send_signal(signal.SIGTERM)
            launch_process.wait(timeout=20)
            assert not is_pid_alive(provider_pid), (
                "adopted external browser should be closed when snapshot keepalive=false hook shuts down"
            )
            _assert_snapshot_chrome_state_cleared(snapshot_chrome_dir)
        finally:
            if tab_process is not None:
                tab_process.send_signal(signal.SIGTERM)
                tab_process.wait(timeout=20)
            launch_process.send_signal(signal.SIGTERM)
            launch_process.wait(timeout=20)
            if is_pid_alive(provider_pid):
                kill_chrome(provider_pid, str(provider_chrome_dir))


def test_chrome_is_local_false_requires_cdp_url_for_launch():
    """CHROME_IS_LOCAL=false without CHROME_CDP_URL should fail fast during launch."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(crawl_dir),
            CHROME_HEADLESS="true",
            CHROME_IS_LOCAL="false",
        )
        launch = subprocess.run(
            [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-local-false-without-cdp-url"],
            cwd=str(chrome_dir),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert launch.returncode != 0
        assert "CHROME_IS_LOCAL=false requires CHROME_CDP_URL" in launch.stderr


def test_cdp_url_is_published_before_extensions_metadata():
    """cdp_url.txt should appear as soon as Chrome is connectable, before extension readiness."""
    with tempfile.TemporaryDirectory() as tmpdir:
        shared_dir = Path(tmpdir) / "shared"
        shared_dir.mkdir()
        chrome_dir = shared_dir / "chrome"
        chrome_dir.mkdir()

        install_env = _isolated_test_env(tmpdir)
        extensions_dir = Path(get_extensions_dir(env=install_env))
        _install_test_extension(extensions_dir, install_env)

        env = install_env | {
            "CRAWL_DIR": str(shared_dir),
            "SNAP_DIR": str(shared_dir),
            "CHROME_HEADLESS": "true",
            "CHROME_EXTENSION_DISCOVERY_TIMEOUT_MS": "5000",
        }
        browser_file = chrome_dir / "browser.json"
        cdp_file = chrome_dir / "cdp_url.txt"
        chrome_launch_process = subprocess.Popen(
            [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-cdp-before-exts"],
            cwd=str(chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        try:
            wait_for_chrome_session_state(
                chrome_dir,
                env=env,
                require_browser_ready=True,
            )
            assert chrome_launch_process.poll() is None, "Chrome launch exited early"
            assert cdp_file.stat().st_mtime_ns <= browser_file.stat().st_mtime_ns, (
                "chrome launch should publish cdp_url.txt before browser.json"
            )
            metadata = wait_for_extensions_metadata(chrome_dir, timeout_seconds=10)
            assert any(entry["name"] == TEST_EXTENSION_NAME for entry in metadata), (
                metadata
            )
        finally:
            _cleanup_launch_process(chrome_launch_process, chrome_dir)


def test_crawl_wait_accepts_http_cdp_url_for_external_browser(chrome_test_url):
    """crawl wait should accept an adopted HTTP CDP endpoint when CHROME_IS_LOCAL=false."""
    with tempfile.TemporaryDirectory() as tmpdir:
        provider_dir = Path(tmpdir) / "provider"
        provider_dir.mkdir()
        provider_chrome_dir = provider_dir / "chrome"
        provider_chrome_dir.mkdir()

        provider_env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(provider_dir),
            CHROME_HEADLESS="true",
        )
        provider_process = subprocess.Popen(
            [str(CHROME_LAUNCH_HOOK), "--crawl-id=provider-http-adopt"],
            cwd=str(provider_chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=provider_env,
        )

        try:
            wait_for_chrome_session_state(
                provider_chrome_dir,
                env=provider_env,
                require_browser_ready=True,
            )
            assert provider_process.poll() is None, "provider launch exited early"

            provider_cdp_url = (provider_chrome_dir / "cdp_url.txt").read_text().strip()
            provider_http_url = (
                f"http://127.0.0.1:{port_from_cdp_url(provider_cdp_url)}"
            )

            adopted_dir = Path(tmpdir) / "adopted"
            adopted_dir.mkdir()
            adopted_chrome_dir = adopted_dir / "chrome"
            adopted_chrome_dir.mkdir()

            adopted_env = _isolated_test_env(
                tmpdir,
                CRAWL_DIR=str(adopted_dir),
                CHROME_HEADLESS="true",
                CHROME_CDP_URL=provider_http_url,
                CHROME_IS_LOCAL="false",
                CHROME_KEEPALIVE="true",
            )

            adopted_launch = subprocess.run(
                [str(CHROME_LAUNCH_HOOK), "--crawl-id=adopt-http-crawl"],
                cwd=str(adopted_chrome_dir),
                capture_output=True,
                text=True,
                timeout=60,
                env=adopted_env,
            )
            assert adopted_launch.returncode == 0, (
                f"adopted launch should succeed with HTTP endpoint:\n"
                f"Stdout: {adopted_launch.stdout}\nStderr: {adopted_launch.stderr}"
            )
            assert (
                adopted_chrome_dir / "cdp_url.txt"
            ).read_text().strip() == provider_http_url
            assert not (adopted_chrome_dir / "chrome.pid").exists()

            crawl_wait = subprocess.run(
                [
                    str(CHROME_CRAWL_WAIT_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-http-adopt",
                ],
                cwd=str(adopted_chrome_dir),
                capture_output=True,
                text=True,
                timeout=60,
                env=adopted_env,
            )
            assert crawl_wait.returncode == 0, (
                f"crawl wait should succeed for adopted HTTP endpoint:\n"
                f"Stdout: {crawl_wait.stdout}\nStderr: {crawl_wait.stderr}"
            )
            assert "pid=external" in crawl_wait.stdout
            assert provider_http_url in crawl_wait.stdout
        finally:
            _cleanup_launch_process(provider_process, provider_chrome_dir)


def test_cookies_imported_on_launch():
    """Integration test: COOKIES_FILE is imported at crawl start."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        cookies_file = Path(tmpdir) / "cookies.txt"
        cookies_file.write_text(
            "\n".join(
                [
                    "# Netscape HTTP Cookie File",
                    "# https://curl.se/docs/http-cookies.html",
                    "# This file was generated by a test",
                    "",
                    "example.com\tTRUE\t/\tFALSE\t2147483647\tabx_test_cookie\thello",
                    "",
                ],
            ),
        )

        env = _isolated_test_env(tmpdir)
        env.update(
            {
                "CHROME_HEADLESS": "true",
                "COOKIES_FILE": str(cookies_file),
                "CRAWL_DIR": str(crawl_dir),
            },
        )

        chrome_launch_process = subprocess.Popen(
            [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-crawl-cookies"],
            cwd=str(chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        wait_for_chrome_session_state(
            chrome_dir,
            env=env,
            require_browser_ready=True,
        )
        assert chrome_launch_process.poll() is None, "Chrome launch exited early"

        assert (chrome_dir / "cdp_url.txt").exists(), "cdp_url.txt should exist"
        int((chrome_dir / "chrome.pid").read_text().strip())
        port = port_from_cdp_url((chrome_dir / "cdp_url.txt").read_text().strip())

        cookies = get_cookies_via_cdp(port, env)
        cookie_found = any(
            c.get("name") == "abx_test_cookie" and c.get("value") == "hello"
            for c in cookies
        )

        assert cookie_found, "Imported cookie should be present in Chrome session"

        # Cleanup
        _cleanup_launch_process(chrome_launch_process, chrome_dir)


def test_chrome_navigation(chrome_test_url):
    """Integration test: Navigate to a URL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        launch_env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(crawl_dir),
            CHROME_HEADLESS="true",
        )
        chrome_launch_process, _cdp_url = launch_chromium_session(
            launch_env,
            chrome_dir,
            "test-crawl-nav",
        )

        # Create snapshot and tab
        snapshot_dir = Path(tmpdir) / "snapshot1"
        snapshot_dir.mkdir()
        snapshot_chrome_dir = snapshot_dir / "chrome"
        snapshot_chrome_dir.mkdir()

        tab_env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(crawl_dir),
            SNAP_DIR=str(snapshot_dir),
            CHROME_HEADLESS="true",
        )
        tab_process = launch_snapshot_tab(
            snapshot_chrome_dir=snapshot_chrome_dir,
            tab_env=tab_env,
            test_url=chrome_test_url,
            snapshot_id="snap-nav-123",
            crawl_id="test-crawl-nav",
        )

        # Navigate to URL
        nav_env = _isolated_test_env(
            tmpdir,
            SNAP_DIR=str(snapshot_dir),
            CHROME_PAGELOAD_TIMEOUT="30",
            CHROME_WAIT_FOR="load",
        )
        result = subprocess.run(
            [
                str(CHROME_NAVIGATE_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=snap-nav-123",
            ],
            cwd=str(snapshot_chrome_dir),
            capture_output=True,
            text=True,
            timeout=120,
            env=nav_env,
        )

        assert result.returncode == 0, (
            f"Navigation failed: {result.stderr}\nStdout: {result.stdout}"
        )

        # Verify navigation outputs
        assert (snapshot_chrome_dir / "navigation.json").exists(), (
            "navigation.json should exist"
        )

        nav_data = json.loads((snapshot_chrome_dir / "navigation.json").read_text())
        assert nav_data.get("status") in [200, 301, 302], (
            f"Should get valid HTTP status: {nav_data}"
        )
        assert nav_data.get("finalUrl"), "Should have final URL"

        # Cleanup
        tab_process.send_signal(signal.SIGTERM)
        tab_process.wait(timeout=10)
        _cleanup_launch_process(chrome_launch_process, chrome_dir)


def test_shared_dir_crawl_snapshot_file_order_and_gating(chrome_test_url):
    """Shared SNAP_DIR/CRAWL_DIR should preserve crawl-vs-snapshot file boundaries in order."""
    with tempfile.TemporaryDirectory() as tmpdir:
        shared_dir = Path(tmpdir) / "shared"
        shared_dir.mkdir()
        chrome_dir = shared_dir / "chrome"
        chrome_dir.mkdir()

        env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(shared_dir),
            SNAP_DIR=str(shared_dir),
            CHROME_HEADLESS="true",
        )

        shared_files = {
            "cdp_url": chrome_dir / "cdp_url.txt",
            "chrome_pid": chrome_dir / "chrome.pid",
        }
        browser_file = chrome_dir / "browser.json"
        snapshot_files = {
            "target": chrome_dir / "target_id.txt",
            "url": chrome_dir / "url.txt",
            "navigation": chrome_dir / "navigation.json",
        }
        chrome_launch_process = None
        tab_process = None
        try:
            chrome_launch_process = subprocess.Popen(
                [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-shared-order"],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            wait_for_chrome_session_state(
                chrome_dir,
                env=env,
                require_browser_ready=True,
            )
            assert chrome_launch_process.poll() is None, "Chrome launch exited early"

            assert all(path.exists() for path in shared_files.values()), (
                f"Crawl-scoped files should exist after launch: {shared_files}"
            )
            assert not any(path.exists() for path in snapshot_files.values()), (
                "Launch hook should not create snapshot-scoped files in shared chrome dir"
            )

            cdp_url_before = shared_files["cdp_url"].read_text().strip()
            chrome_pid_before = shared_files["chrome_pid"].read_text().strip()
            browser_before = browser_file.read_text() if browser_file.exists() else None
            assert cdp_url_before.startswith(("ws://127.0.0.1:", "ws://localhost:")), (
                cdp_url_before
            )
            port_before = str(port_from_cdp_url(cdp_url_before))
            os.kill(int(chrome_pid_before), 0)
            assert fetch_devtools_targets(cdp_url_before), (
                "crawl launch should expose a live DevTools target list"
            )

            crawl_wait = subprocess.run(
                [
                    str(CHROME_CRAWL_WAIT_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-shared-order",
                ],
                cwd=str(chrome_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert crawl_wait.returncode == 0, (
                f"crawl wait should succeed before snapshot setup:\n"
                f"Stdout: {crawl_wait.stdout}\nStderr: {crawl_wait.stderr}"
            )
            assert f"pid={chrome_pid_before}" in crawl_wait.stdout
            assert f":{port_before}" in crawl_wait.stdout
            assert not any(path.exists() for path in snapshot_files.values()), (
                "crawl wait should not create snapshot-scoped files"
            )

            snapshot_wait_before_tab = subprocess.run(
                [
                    str(CHROME_WAIT_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-shared-order",
                ],
                cwd=str(chrome_dir),
                capture_output=True,
                text=True,
                timeout=15,
                env=env | {"CHROME_TAB_TIMEOUT": "1", "CHROME_TIMEOUT": "1"},
            )
            assert snapshot_wait_before_tab.returncode != 0, (
                "snapshot wait should fail before snapshot tab creates target_id.txt"
            )
            assert not snapshot_files["target"].exists(), (
                "snapshot wait must not synthesize target_id.txt before chrome_tab runs"
            )

            delayed_snapshot_wait = subprocess.Popen(
                [
                    str(CHROME_WAIT_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-shared-order-delayed",
                ],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env | {"CHROME_TAB_TIMEOUT": "5", "CHROME_TIMEOUT": "5"},
            )
            assert delayed_snapshot_wait.poll() is None, (
                "snapshot wait must remain blocked before tab readiness is published"
            )

            tab_process = launch_snapshot_tab(
                snapshot_chrome_dir=chrome_dir,
                tab_env=env,
                test_url=chrome_test_url,
                snapshot_id="snap-shared-order",
                crawl_id="test-shared-order",
            )
            delayed_wait_stdout, delayed_wait_stderr = (
                delayed_snapshot_wait.communicate(timeout=15)
            )
            assert delayed_snapshot_wait.returncode == 0, (
                "snapshot wait should block until tab markers appear, not fail immediately when it starts before chrome_tab:\n"
                f"Stdout: {delayed_wait_stdout}\nStderr: {delayed_wait_stderr}"
            )

            target_id_before_wait = snapshot_files["target"].read_text().strip()
            url_before_wait = snapshot_files["url"].read_text().strip()
            assert url_before_wait == chrome_test_url
            if browser_before is None:
                assert browser_file.exists(), (
                    "chrome_tab should wait for crawl launch to publish browser.json"
                )
                copied_browser = json.loads(browser_file.read_text())
                assert copied_browser.get("ready") is True, copied_browser
            else:
                assert browser_file.read_text() == browser_before
            assert not snapshot_files["navigation"].exists(), (
                "chrome_tab should not create navigation.json before navigate"
            )
            assert shared_files["cdp_url"].read_text().strip() == cdp_url_before
            assert shared_files["chrome_pid"].read_text().strip() == chrome_pid_before

            tab_targets = fetch_devtools_targets(cdp_url_before)
            tab_target = next(
                (
                    target
                    for target in tab_targets
                    if target.get("id") == target_id_before_wait
                ),
                None,
            )
            assert tab_target is not None, "chrome_tab should create a live page target"
            assert tab_target.get("type") == "page"
            assert tab_target.get("url") == "about:blank"

            snapshot_wait = subprocess.run(
                [
                    str(CHROME_WAIT_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-shared-order",
                ],
                cwd=str(chrome_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert snapshot_wait.returncode == 0, (
                f"snapshot wait should succeed after chrome_tab:\n"
                f"Stdout: {snapshot_wait.stdout}\nStderr: {snapshot_wait.stderr}"
            )
            assert f"target={target_id_before_wait}" in snapshot_wait.stdout
            assert f"port={port_before}" in snapshot_wait.stdout
            assert snapshot_files["target"].read_text().strip() == target_id_before_wait
            assert snapshot_files["url"].read_text().strip() == chrome_test_url

            navigate = subprocess.run(
                [
                    str(CHROME_NAVIGATE_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-shared-order",
                ],
                cwd=str(chrome_dir),
                capture_output=True,
                text=True,
                timeout=60,
                env=env | {"CHROME_PAGELOAD_TIMEOUT": "30", "CHROME_WAIT_FOR": "load"},
            )
            assert navigate.returncode == 0, (
                f"navigate should succeed after snapshot wait:\n"
                f"Stdout: {navigate.stdout}\nStderr: {navigate.stderr}"
            )
            nav_data = json.loads(snapshot_files["navigation"].read_text())
            assert nav_data["url"] == chrome_test_url
            final_url = nav_data["finalUrl"]
            assert nav_data["status"] == 200
            assert final_url.rstrip("/") == chrome_test_url.rstrip("/")
            assert shared_files["cdp_url"].read_text().strip() == cdp_url_before
            assert shared_files["chrome_pid"].read_text().strip() == chrome_pid_before
            if browser_before is not None:
                assert browser_file.read_text() == browser_before
            assert snapshot_files["target"].read_text().strip() == target_id_before_wait

            navigated_targets = fetch_devtools_targets(cdp_url_before)
            navigated_target = next(
                (
                    target
                    for target in navigated_targets
                    if target.get("id") == target_id_before_wait
                ),
                None,
            )
            assert navigated_target is not None, (
                "navigation should keep the same target alive"
            )
            assert navigated_target.get("url", "").rstrip(
                "/",
            ) == chrome_test_url.rstrip("/")
        finally:
            if tab_process is not None:
                tab_process.send_signal(signal.SIGTERM)
                tab_process.wait(timeout=10)
            assert not snapshot_files["target"].exists(), (
                "target_id.txt should be removed after snapshot tab teardown"
            )
            if chrome_launch_process is not None:
                chrome_launch_process.send_signal(signal.SIGTERM)
                chrome_launch_process.wait(timeout=10)
            assert not shared_files["chrome_pid"].exists(), (
                "chrome.pid should be removed after crawl teardown"
            )


def test_shared_dir_extensions_metadata_created_and_preserved_when_enabled(
    chrome_test_url,
):
    """Shared crawl/snapshot setup should create correct browser.json when extensions are enabled."""
    with tempfile.TemporaryDirectory() as tmpdir:
        shared_dir = Path(tmpdir) / "shared"
        shared_dir.mkdir()
        chrome_dir = shared_dir / "chrome"
        chrome_dir.mkdir()

        install_env = _isolated_test_env(tmpdir)
        extensions_dir = Path(get_extensions_dir(env=install_env))
        cached_ext = _install_test_extension(extensions_dir, install_env)
        extension_cache = extensions_dir / f"{TEST_EXTENSION_NAME}.extension.json"
        assert extension_cache.exists(), "test extension cache should exist"

        env = install_env | {
            "CRAWL_DIR": str(shared_dir),
            "SNAP_DIR": str(shared_dir),
            "CHROME_HEADLESS": "true",
        }
        browser_file = chrome_dir / "browser.json"
        chrome_launch_process = None
        tab_process = None
        try:
            chrome_launch_process = subprocess.Popen(
                [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-shared-exts"],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            wait_for_chrome_session_state(
                chrome_dir,
                env=env,
                require_browser_ready=True,
            )
            assert chrome_launch_process.poll() is None, "Chrome launch exited early"

            assert browser_file.exists(), (
                "chrome launch should create browser.json when extensions are enabled"
            )
            crawl_browser_text = browser_file.read_text()
            crawl_browser = json.loads(crawl_browser_text)
            assert crawl_browser.get("ready") is True, crawl_browser
            crawl_extensions = crawl_browser.get("extensions")
            assert isinstance(crawl_extensions, list), crawl_browser
            extension_entry = next(
                (
                    entry
                    for entry in crawl_extensions
                    if entry.get("name") == TEST_EXTENSION_NAME
                ),
                None,
            )
            assert extension_entry is not None, crawl_extensions
            assert extension_entry.get("webstore_id") == cached_ext["webstore_id"]
            assert extension_entry.get("unpacked_path") == cached_ext["unpacked_path"]
            assert extension_entry.get("id"), extension_entry
            assert extension_entry.get("id") == cached_ext.get("id"), extension_entry
            assert "load_error" not in extension_entry, extension_entry
            assert "load_path" not in extension_entry, extension_entry
            assert Path(extension_entry["unpacked_path"]).is_dir(), extension_entry
            assert (Path(extension_entry["unpacked_path"]) / "manifest.json").is_file()
            assert (
                wait_for_extensions_metadata(chrome_dir, timeout_seconds=10)
                == crawl_extensions
            )

            tab_process = launch_snapshot_tab(
                snapshot_chrome_dir=chrome_dir,
                tab_env=env,
                test_url=chrome_test_url,
                snapshot_id="snap-shared-exts",
                crawl_id="test-shared-exts",
            )
            assert json.loads(browser_file.read_text()) == crawl_browser
            assert browser_file.read_text() == crawl_browser_text

            snapshot_wait = subprocess.run(
                [
                    str(CHROME_WAIT_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-shared-exts",
                ],
                cwd=str(chrome_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert snapshot_wait.returncode == 0, (
                f"snapshot wait should succeed with extensions enabled:\n"
                f"Stdout: {snapshot_wait.stdout}\nStderr: {snapshot_wait.stderr}"
            )
            assert json.loads(browser_file.read_text()) == crawl_browser
            assert browser_file.read_text() == crawl_browser_text
        finally:
            if tab_process is not None:
                tab_process.send_signal(signal.SIGTERM)
                tab_process.wait(timeout=10)
            if chrome_launch_process is not None:
                chrome_launch_process.send_signal(signal.SIGTERM)
                chrome_launch_process.wait(timeout=10)


def test_chrome_wait_rejects_stale_cdp_markers(chrome_test_url):
    """chrome_wait should not treat stale marker files as a live CDP session."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with chrome_session(
            Path(tmpdir),
            snapshot_id="snap-wait-stale",
            test_url=chrome_test_url,
            navigate=True,
            timeout=45,
        ) as (_process, chrome_pid, snapshot_chrome_dir, wait_env):
            assert kill_chrome(chrome_pid)
            assert not is_pid_alive(chrome_pid)
            wait_env["CHROME_TAB_TIMEOUT"] = "1"
            wait_env["CHROME_TIMEOUT"] = "1"
            result = subprocess.run(
                [
                    str(CHROME_WAIT_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-wait-stale",
                ],
                cwd=str(snapshot_chrome_dir),
                capture_output=True,
                text=True,
                timeout=10,
                env=wait_env,
            )

        assert result.returncode == 1, (
            f"chrome_wait should fail for stale CDP markers: {result.stderr}\nStdout: {result.stdout}"
        )
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["status"] == "failed"
        assert (
            payload["output_str"]
            == "No Chrome session found (chrome plugin must run first)"
        )


def test_crawl_wait_reacts_when_published_cdp_endpoint_changes(
    chrome_test_url,
):
    """crawl wait should revalidate the session when its published CDP URL changes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (
            _stale_provider_dir,
            _stale_provider_chrome_dir,
            stale_provider_env,
            stale_provider_cdp_url,
            stale_provider_pid,
        ) = _launch_keepalive_local_provider_browser(
            tmpdir,
            crawl_dir_name="stale-provider-crawl-wait-file-change",
        )
        (
            _provider_dir,
            provider_chrome_dir,
            provider_env,
            provider_cdp_url,
            provider_pid,
        ) = _launch_keepalive_local_provider_browser(
            tmpdir,
            crawl_dir_name="live-provider-crawl-wait-file-change",
        )
        assert kill_chrome(stale_provider_pid)
        assert not is_pid_alive(stale_provider_pid)
        wait_process = None
        try:
            adopted_dir = Path(tmpdir) / "adopted-crawl-wait-file-change"
            adopted_dir.mkdir()
            adopted_chrome_dir = adopted_dir / "chrome"
            adopted_chrome_dir.mkdir()
            adopted_chrome_dir.joinpath("cdp_url.txt").write_text(
                stale_provider_cdp_url,
            )
            write_browser_metadata(adopted_chrome_dir, env=stale_provider_env)

            adopted_env = _isolated_test_env(
                tmpdir,
                CRAWL_DIR=str(adopted_dir),
                CHROME_IS_LOCAL="false",
                CHROME_TIMEOUT="5",
                CHROME_TAB_TIMEOUT="5",
            )

            wait_process = subprocess.Popen(
                [
                    str(CHROME_CRAWL_WAIT_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-crawl-wait-file-change",
                ],
                cwd=str(adopted_chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=adopted_env,
            )
            assert wait_process.stdout is not None
            startup_line = wait_process.stdout.readline()
            assert startup_line.startswith("waiting for "), startup_line
            assert wait_process.poll() is None, (
                "crawl wait should still be blocked on the unreachable endpoint"
            )

            adopted_chrome_dir.joinpath("cdp_url.txt").write_text(provider_cdp_url)

            stdout, stderr = wait_process.communicate(timeout=15)
            assert wait_process.returncode == 0, (
                "crawl wait should accept the newly published connectable endpoint:\n"
                f"Stdout: {stdout}\nStderr: {stderr}"
            )
            assert "ready pid=external" in stdout.lower(), stdout
        finally:
            if wait_process is not None:
                wait_process.send_signal(signal.SIGTERM)
                wait_process.wait(timeout=15)
            assert kill_chrome(provider_pid, str(provider_chrome_dir))
            assert not is_pid_alive(provider_pid)


def test_cleanup_stale_chrome_session_artifacts_only_when_stale():
    """Stale chrome markers should be removed, but only when they are actually stale."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _provider_dir, session_dir, env, _cdp_url, pid = (
            _launch_keepalive_local_provider_browser(
                tmpdir,
                crawl_dir_name="provider-stale-cleanup",
            )
        )
        assert kill_chrome(pid)
        assert not is_pid_alive(pid)
        result = _cleanup_session_artifacts(
            session_dir,
            env,
        )

        assert result["hasArtifacts"] is True
        assert result["stale"] is True
        assert "cdp_url.txt" in result["cleanedFiles"]
        assert not (session_dir / "cdp_url.txt").exists()


def test_cleanup_stale_chrome_session_artifacts_keeps_live_session():
    """Healthy Chrome sessions should not have their marker files removed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(crawl_dir),
            CHROME_HEADLESS="true",
        )
        chrome_launch_process = subprocess.Popen(
            [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-stale-cleanup"],
            cwd=str(chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        try:
            wait_for_chrome_session_state(
                chrome_dir,
                env=env,
                require_browser_ready=True,
            )
            assert chrome_launch_process.poll() is None, "Chrome launch exited early"

            result = _cleanup_session_artifacts(chrome_dir, env)

            assert result["hasArtifacts"] is True
            assert result["stale"] is False
            assert result["cleanedFiles"] == []
            assert (chrome_dir / "cdp_url.txt").exists()
            assert (chrome_dir / "chrome.pid").exists()
        finally:
            _cleanup_launch_process(chrome_launch_process, chrome_dir)


def test_tab_cleanup_on_sigterm(chrome_test_url):
    """Integration test: Tab cleanup when receiving SIGTERM."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        launch_env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(crawl_dir),
            CHROME_HEADLESS="true",
        )
        chrome_launch_process, _cdp_url = launch_chromium_session(
            launch_env,
            chrome_dir,
            "test-cleanup",
        )
        chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())

        # Create snapshot and tab - run in background
        snapshot_dir = Path(tmpdir) / "snapshot1"
        snapshot_dir.mkdir()
        snapshot_chrome_dir = snapshot_dir / "chrome"
        snapshot_chrome_dir.mkdir()

        tab_env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(crawl_dir),
            SNAP_DIR=str(snapshot_dir),
            CHROME_HEADLESS="true",
        )
        tab_process = launch_snapshot_tab(
            snapshot_chrome_dir=snapshot_chrome_dir,
            tab_env=tab_env,
            test_url=chrome_test_url,
            snapshot_id="snap-cleanup",
            crawl_id="test-cleanup",
        )

        # Send SIGTERM to tab process
        tab_process.send_signal(signal.SIGTERM)
        stdout, stderr = tab_process.communicate(timeout=10)

        assert tab_process.returncode == 0, f"Tab process should exit cleanly: {stderr}"
        _assert_snapshot_page_state_cleared(snapshot_chrome_dir)
        assert (snapshot_chrome_dir / "cdp_url.txt").exists(), (
            "tab cleanup must leave the browser CDP marker for crawl-level browser ownership"
        )

        # Chrome should still be running
        try:
            os.kill(chrome_pid, 0)
        except OSError:
            raise AssertionError("Chrome should still be running after tab cleanup")

        # Cleanup
        _cleanup_launch_process(chrome_launch_process, chrome_dir)


def test_tab_cleanup_on_sigterm_interrupts_inflight_navigation(httpserver):
    """Tab cleanup must not wait for a separate navigation client's lifecycle."""
    navigation_started = threading.Event()
    navigation_release = threading.Event()

    def blocked_navigation(_request):
        navigation_started.set()
        if not navigation_release.wait(timeout=30):
            return Response("navigation release was not signaled", status=500)
        return Response("<html><body>navigation released</body></html>")

    httpserver.expect_oneshot_request("/tab-cleanup").respond_with_handler(
        blocked_navigation,
    )
    navigation_url = httpserver.url_for("/tab-cleanup")

    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        crawl_chrome_dir = crawl_dir / "chrome"
        crawl_chrome_dir.mkdir()
        snapshot_dir = Path(tmpdir) / "snapshot"
        snapshot_dir.mkdir()
        snapshot_chrome_dir = snapshot_dir / "chrome"
        snapshot_chrome_dir.mkdir()

        env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(crawl_dir),
            SNAP_DIR=str(snapshot_dir),
            CHROME_HEADLESS="true",
            CHROME_WAIT_FOR="load",
        )
        chrome_launch_process, _cdp_url = launch_chromium_session(
            env,
            crawl_chrome_dir,
            "test-tab-cleanup-navigation",
        )
        chrome_pid = int((crawl_chrome_dir / "chrome.pid").read_text().strip())
        tab_process = launch_snapshot_tab(
            snapshot_chrome_dir=snapshot_chrome_dir,
            tab_env=env,
            test_url=navigation_url,
            snapshot_id="snap-tab-cleanup-navigation",
            crawl_id="test-tab-cleanup-navigation",
        )
        navigate_process = subprocess.Popen(
            [
                str(CHROME_NAVIGATE_HOOK),
                f"--url={navigation_url}",
                "--snapshot-id=snap-tab-cleanup-navigation",
            ],
            cwd=str(snapshot_chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env | {"CHROME_PAGELOAD_TIMEOUT": "30"},
        )

        try:
            assert navigation_started.wait(timeout=30), (
                "navigation hook did not request the blocking test page"
            )
            tab_process.send_signal(signal.SIGTERM)
            _tab_stdout, tab_stderr = tab_process.communicate(timeout=10)
            assert tab_process.returncode == 0, tab_stderr
            navigate_stdout, navigate_stderr = navigate_process.communicate(timeout=10)
            assert navigate_process.returncode != 0, (
                f"closing the tab should interrupt its active navigation:\n"
                f"Stdout: {navigate_stdout}\nStderr: {navigate_stderr}"
            )
            assert not (snapshot_chrome_dir / "target_id.txt").exists()
            assert not (snapshot_chrome_dir / "url.txt").exists()
            navigation_state = json.loads(
                (snapshot_chrome_dir / "navigation.json").read_text(),
            )
            assert (
                "Target" in navigation_state["error"]
                or "closed" in navigation_state["error"]
                or "detached" in navigation_state["error"]
            ), navigation_state
            assert is_pid_alive(chrome_pid), (
                "closing the snapshot tab must not close its crawl browser"
            )
        finally:
            navigation_release.set()
            if navigate_process.poll() is None:
                navigate_process.send_signal(signal.SIGTERM)
            navigate_process.communicate(timeout=10)
            if tab_process.poll() is None:
                tab_process.send_signal(signal.SIGTERM)
                tab_process.communicate(timeout=10)
            _cleanup_launch_process(chrome_launch_process, crawl_chrome_dir)


def test_snapshot_wait_survives_idle_delay_with_shared_dirs(chrome_test_url):
    """Snapshot tab should remain connectable even when SNAP_DIR and CRAWL_DIR are shared."""
    with tempfile.TemporaryDirectory() as tmpdir:
        shared_dir = Path(tmpdir) / "shared"
        shared_dir.mkdir()
        chrome_dir = shared_dir / "chrome"
        chrome_dir.mkdir()

        env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(shared_dir),
            SNAP_DIR=str(shared_dir),
            CHROME_HEADLESS="true",
        )

        chrome_launch_process = subprocess.Popen(
            [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-shared-dirs"],
            cwd=str(chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        wait_for_chrome_session_state(
            chrome_dir,
            env=env,
            require_browser_ready=True,
        )
        assert chrome_launch_process.poll() is None, "Chrome launch exited early"

        int((chrome_dir / "chrome.pid").read_text().strip())
        tab_process = launch_snapshot_tab(
            snapshot_chrome_dir=chrome_dir,
            tab_env=env,
            test_url=chrome_test_url,
            snapshot_id="snap-shared",
            crawl_id="test-shared-dirs",
        )

        assert not threading.Event().wait(timeout=8), (
            "idle interval event should remain unset"
        )

        result = subprocess.run(
            [
                str(CHROME_WAIT_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=snap-shared",
            ],
            cwd=str(chrome_dir),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

        assert result.returncode == 0, (
            f"chrome_wait should reconnect after idle delay when dirs are shared:\n"
            f"Stdout: {result.stdout}\nStderr: {result.stderr}"
        )

        tab_process.send_signal(signal.SIGTERM)
        tab_process.wait(timeout=10)
        _cleanup_launch_process(chrome_launch_process, chrome_dir)


def test_concurrent_same_dir_reuses_one_browser_and_one_target(chrome_test_url):
    """Concurrent same-dir launch/tab calls should converge on one live browser and one canonical target."""
    with tempfile.TemporaryDirectory() as tmpdir:
        shared_dir = Path(tmpdir) / "shared"
        shared_dir.mkdir()
        chrome_dir = shared_dir / "chrome"
        chrome_dir.mkdir()
        env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(shared_dir),
            SNAP_DIR=str(shared_dir),
            CHROME_HEADLESS="true",
        )

        launch_a = launch_b = tab_a = tab_b = None
        try:
            launch_a = subprocess.Popen(
                [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-concurrent"],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            launch_b = subprocess.Popen(
                [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-concurrent"],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            wait_for_chrome_session_state(
                chrome_dir,
                env=env,
                require_browser_ready=True,
            )
            assert launch_a.poll() is None or launch_b.poll() is None, (
                "at least one concurrent launch hook must own the live browser"
            )

            cdp_url = (chrome_dir / "cdp_url.txt").read_text().strip()
            chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())
            os.kill(chrome_pid, 0)
            page_targets_before = {
                target["id"]
                for target in fetch_devtools_targets(cdp_url)
                if target.get("type") == "page" and target.get("id")
            }

            tab_a = subprocess.Popen(
                [
                    str(CHROME_TAB_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-concurrent",
                    "--crawl-id=test-concurrent",
                ],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            tab_b = subprocess.Popen(
                [
                    str(CHROME_TAB_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-concurrent",
                    "--crawl-id=test-concurrent",
                ],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            wait_for_chrome_session_state(
                chrome_dir,
                env=env,
                require_target_id=True,
                require_browser_ready=True,
            )

            wait_result = subprocess.run(
                [
                    str(CHROME_WAIT_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=snap-concurrent",
                ],
                cwd=str(chrome_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert wait_result.returncode == 0, (
                f"snapshot wait should succeed after concurrent tab setup:\n"
                f"Stdout: {wait_result.stdout}\nStderr: {wait_result.stderr}"
            )

            target_id = (chrome_dir / "target_id.txt").read_text().strip()
            page_targets_after = {
                target["id"]
                for target in fetch_devtools_targets(cdp_url)
                if target.get("type") == "page" and target.get("id")
            }
            assert target_id in page_targets_after
            assert len(page_targets_after - page_targets_before) == 1, (
                f"Concurrent same-dir tab setup should create exactly one new page target: before={page_targets_before} after={page_targets_after}"
            )
            assert any(proc.poll() is None for proc in (launch_a, launch_b))
            assert tab_a.poll() is None and tab_b.poll() is None
        finally:
            for proc in (tab_a, tab_b, launch_a, launch_b):
                if proc is None:
                    continue
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)


def test_tab_teardown_preserves_completed_navigation_result(chrome_test_url):
    with tempfile.TemporaryDirectory() as tmpdir:
        shared_dir = Path(tmpdir) / "shared"
        chrome_dir = shared_dir / "chrome"
        chrome_dir.mkdir(parents=True)
        env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(shared_dir),
            SNAP_DIR=str(shared_dir),
            CHROME_HEADLESS="true",
        )
        chrome_launch_process = None
        tab_process = None
        navigation_state = {
            "url": chrome_test_url,
            "finalUrl": chrome_test_url,
            "status": 200,
        }
        navigation_file = chrome_dir / "navigation.json"
        try:
            chrome_launch_process, _cdp_url = launch_chromium_session(
                env=env,
                chrome_dir=chrome_dir,
                crawl_id="test-navigation-ownership",
            )
            tab_process = launch_snapshot_tab(
                snapshot_chrome_dir=chrome_dir,
                tab_env=env,
                test_url=chrome_test_url,
                snapshot_id="snap-navigation-ownership",
                crawl_id="test-navigation-ownership",
            )
            navigation_file.write_text(json.dumps(navigation_state))

            tab_process.send_signal(signal.SIGTERM)
            tab_process.wait(timeout=10)
            tab_process = None

            assert json.loads(navigation_file.read_text()) == navigation_state
            assert not (chrome_dir / "target_id.txt").exists()
            assert not (chrome_dir / "url.txt").exists()
        finally:
            for proc in (tab_process, chrome_launch_process):
                if proc is None:
                    continue
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)


def test_target_crash_mid_navigation_recovers_with_fresh_tab(
    chrome_test_urls,
    httpserver,
):
    """If the canonical target disappears mid-run, navigation should fail clearly and the next tab setup should recreate it."""
    navigation_started = threading.Event()
    navigation_release = threading.Event()

    def blocked_navigation(_request):
        navigation_started.set()
        if not navigation_release.wait(timeout=30):
            return Response("navigation release was not signaled", status=500)
        return Response("<html><body>target crash</body></html>")

    httpserver.expect_oneshot_request("/target-crash").respond_with_handler(
        blocked_navigation,
    )
    crash_url = httpserver.url_for("/target-crash")

    with tempfile.TemporaryDirectory() as tmpdir:
        shared_dir = Path(tmpdir) / "shared"
        shared_dir.mkdir()
        chrome_dir = shared_dir / "chrome"
        chrome_dir.mkdir()
        env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(shared_dir),
            SNAP_DIR=str(shared_dir),
            CHROME_HEADLESS="true",
            CHROME_WAIT_FOR="load",
        )

        chrome_launch_process = None
        tab_process = None
        replacement_tab_process = None
        navigate_process = None
        try:
            chrome_launch_process, cdp_url = launch_chromium_session(
                env=env,
                chrome_dir=chrome_dir,
                crawl_id="test-target-crash",
            )
            tab_process = launch_snapshot_tab(
                snapshot_chrome_dir=chrome_dir,
                tab_env=env,
                test_url=crash_url,
                snapshot_id="snap-target-crash",
                crawl_id="test-target-crash",
            )
            target_before = (chrome_dir / "target_id.txt").read_text().strip()

            navigate_process = subprocess.Popen(
                [
                    str(CHROME_NAVIGATE_HOOK),
                    f"--url={crash_url}",
                    "--snapshot-id=snap-target-crash",
                ],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env | {"CHROME_PAGELOAD_TIMEOUT": "15"},
            )
            assert navigation_started.wait(timeout=30), (
                "navigation hook did not request the blocking test page"
            )
            try:
                close_target_and_wait_destroyed(cdp_url, target_before, env)
            finally:
                navigation_release.set()
            remaining_targets = {
                target["id"]
                for target in fetch_devtools_targets(cdp_url)
                if target.get("type") == "page" and target.get("id")
            }
            assert target_before not in remaining_targets
            stdout, stderr = navigate_process.communicate(timeout=30)
            assert navigate_process.returncode != 0, (
                "navigate should fail if the canonical target is closed mid-run"
            )
            nav_data = json.loads((chrome_dir / "navigation.json").read_text())
            assert "error" in nav_data, nav_data
            assert (
                "Target" in nav_data["error"]
                or "closed" in nav_data["error"]
                or "detached" in nav_data["error"]
            ), nav_data
            assert not nav_data.get("finalUrl")

            replacement_url = chrome_test_urls["base_url"]
            replacement_tab_process = launch_snapshot_tab(
                snapshot_chrome_dir=chrome_dir,
                tab_env=env,
                test_url=replacement_url,
                snapshot_id="snap-target-crash",
                crawl_id="test-target-crash",
            )
            replacement_target = (chrome_dir / "target_id.txt").read_text().strip()
            assert replacement_target != target_before

            tab_process.send_signal(signal.SIGTERM)
            tab_process.wait(timeout=10)
            tab_process = None
            assert (
                chrome_dir / "target_id.txt"
            ).read_text().strip() == replacement_target
            assert replacement_target in {
                target["id"]
                for target in fetch_devtools_targets(cdp_url)
                if target.get("type") == "page" and target.get("id")
            }

            wait_result = subprocess.run(
                [
                    str(CHROME_WAIT_HOOK),
                    f"--url={replacement_url}",
                    "--snapshot-id=snap-target-crash",
                ],
                cwd=str(chrome_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert wait_result.returncode == 0, (
                f"snapshot wait should recover after replacing dead target:\n"
                f"Stdout: {wait_result.stdout}\nStderr: {wait_result.stderr}"
            )
        finally:
            navigation_release.set()
            for proc in (
                navigate_process,
                replacement_tab_process,
                tab_process,
                chrome_launch_process,
            ):
                if proc is None:
                    continue
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)


def test_published_target_is_resolvable_from_fresh_cdp_connections(chrome_test_url):
    """Fresh CDP clients must resolve the exact target_id.txt target before navigation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        shared_dir = Path(tmpdir) / "shared"
        shared_dir.mkdir()
        chrome_dir = shared_dir / "chrome"
        chrome_dir.mkdir()
        env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(shared_dir),
            SNAP_DIR=str(shared_dir),
            CHROME_HEADLESS="true",
        )

        chrome_launch_process = None
        tab_process = None
        try:
            chrome_launch_process, cdp_url = launch_chromium_session(
                env=env,
                chrome_dir=chrome_dir,
                crawl_id="test-fresh-target-resolve",
                timeout=60,
            )
            tab_process = launch_snapshot_tab(
                snapshot_chrome_dir=chrome_dir,
                tab_env=env,
                test_url=chrome_test_url,
                snapshot_id="snap-fresh-target-resolve",
                crawl_id="test-fresh-target-resolve",
            )
            target_id = (chrome_dir / "target_id.txt").read_text().strip()
            raw_targets = fetch_devtools_targets(cdp_url)
            assert any(target.get("id") == target_id for target in raw_targets)

            script = f"""
const chromeUtils = require({json.dumps(str(CHROME_UTILS))});
const chromeSessionDir = process.argv[1];
const expectedTargetId = process.argv[2];

(async () => {{
  const puppeteer = chromeUtils.resolvePuppeteerModule();
  const results = await Promise.all(Array.from({{ length: 4 }}, async () => {{
    const connection = await chromeUtils.connectToPage({{
      chromeSessionDir,
      timeoutMs: 5000,
      requireTargetId: true,
      puppeteer,
    }});
    try {{
      const actualTargetId = chromeUtils.getTargetIdFromPage(connection.page);
      await connection.page.title();
      return actualTargetId;
    }} finally {{
      connection.browser.disconnect();
    }}
  }}));
  if (!results.every((targetId) => targetId === expectedTargetId)) {{
    throw new Error(`resolved unexpected targets: ${{JSON.stringify(results)}} expected=${{expectedTargetId}}`);
  }}
  console.log(JSON.stringify(results));
}})().catch((error) => {{
  console.error(error && error.stack || error);
  process.exit(1);
}});
"""
            result = subprocess.run(
                [env["NODE_BINARY"], "-e", script, str(chrome_dir), target_id],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert result.returncode == 0, (
                f"fresh CDP clients should resolve the published target:\n"
                f"Stdout: {result.stdout}\nStderr: {result.stderr}"
            )
        finally:
            for proc in (tab_process, chrome_launch_process):
                if proc is None:
                    continue
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)


def test_popup_focus_theft_keeps_followup_hooks_on_canonical_target(chrome_test_urls):
    """Popup windows stealing focus must not move follow-up hooks off the canonical snapshot target."""
    with tempfile.TemporaryDirectory() as tmpdir:
        shared_dir = Path(tmpdir) / "shared"
        shared_dir.mkdir()
        chrome_dir = shared_dir / "chrome"
        chrome_dir.mkdir()
        env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(shared_dir),
            SNAP_DIR=str(shared_dir),
            CHROME_HEADLESS="true",
            CHROME_WAIT_FOR="load",
        )

        chrome_launch_process = None
        tab_process = None
        try:
            chrome_launch_process = subprocess.Popen(
                [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-popup-focus"],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            wait_for_chrome_session_state(
                chrome_dir,
                env=env,
                require_browser_ready=True,
            )
            assert chrome_launch_process.poll() is None, "Chrome launch exited early"

            cdp_url = (chrome_dir / "cdp_url.txt").read_text().strip()
            tab_process = launch_snapshot_tab(
                snapshot_chrome_dir=chrome_dir,
                tab_env=env,
                test_url=chrome_test_urls["popup_parent_url"],
                snapshot_id="snap-popup-focus",
                crawl_id="test-popup-focus",
            )
            navigate = subprocess.run(
                [
                    str(CHROME_NAVIGATE_HOOK),
                    f"--url={chrome_test_urls['popup_parent_url']}",
                    "--snapshot-id=snap-popup-focus",
                ],
                cwd=str(chrome_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert navigate.returncode == 0, (
                f"navigate should succeed on popup page:\nStdout: {navigate.stdout}\nStderr: {navigate.stderr}"
            )
            popup_target_info = create_target_via_cdp(
                cdp_url,
                chrome_test_urls["popup_child_url"],
            )

            target_id = (chrome_dir / "target_id.txt").read_text().strip()
            targets = fetch_devtools_targets(cdp_url)
            canonical_target = next(
                (target for target in targets if target.get("id") == target_id),
                None,
            )
            popup_target = next(
                (
                    target
                    for target in targets
                    if target.get("id") == popup_target_info.get("id")
                ),
                None,
            )
            assert canonical_target is not None, targets
            assert canonical_target.get("url", "").rstrip("/") == chrome_test_urls[
                "popup_parent_url"
            ].rstrip("/")
            assert popup_target is not None, targets

            probed_page = _probe_current_snapshot_page(chrome_dir, env)
            assert probed_page["title"] == "Popup Parent", probed_page
            assert probed_page["url"].rstrip("/") == chrome_test_urls[
                "popup_parent_url"
            ].rstrip("/"), probed_page
        finally:
            for proc in (tab_process, chrome_launch_process):
                if proc is None:
                    continue
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)


def test_multiple_snapshots_share_chrome(chrome_test_urls):
    """Integration test: Multiple snapshots share one Chrome instance."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        launch_env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(crawl_dir),
            CHROME_HEADLESS="true",
        )
        chrome_launch_process = None
        try:
            chrome_launch_process, crawl_cdp_url = launch_chromium_session(
                launch_env,
                chrome_dir,
                "test-multi-crawl",
            )
            chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())

            # Create multiple snapshots that share this Chrome
            snapshot_dirs = []
            target_ids = []

            for snap_num in range(3):
                snapshot_dir = Path(tmpdir) / f"snapshot{snap_num}"
                snapshot_dir.mkdir()
                snapshot_chrome_dir = snapshot_dir / "chrome"
                snapshot_chrome_dir.mkdir()
                snapshot_dirs.append(snapshot_chrome_dir)

                # Create tab for this snapshot
                tab_url = f"{chrome_test_urls['origin']}/snapshot-{snap_num}"
                tab_env = _isolated_test_env(
                    tmpdir,
                    CRAWL_DIR=str(crawl_dir),
                    SNAP_DIR=str(snapshot_dir),
                    CHROME_HEADLESS="true",
                )
                tab_process = launch_snapshot_tab(
                    snapshot_chrome_dir=snapshot_chrome_dir,
                    tab_env=tab_env,
                    test_url=tab_url,
                    snapshot_id=f"snap-{snap_num}",
                    crawl_id="test-multi-crawl",
                )

                # Verify each snapshot has its own target_id but same Chrome PID
                assert (snapshot_chrome_dir / "target_id.txt").exists()
                assert (snapshot_chrome_dir / "cdp_url.txt").exists()
                assert (snapshot_chrome_dir / "chrome.pid").exists()

                target_id = (snapshot_chrome_dir / "target_id.txt").read_text().strip()
                snapshot_cdp_url = (
                    (snapshot_chrome_dir / "cdp_url.txt").read_text().strip()
                )
                snapshot_pid = int(
                    (snapshot_chrome_dir / "chrome.pid").read_text().strip(),
                )

                target_ids.append(target_id)

                # All snapshots should share same Chrome
                assert snapshot_pid == chrome_pid, (
                    f"Snapshot {snap_num} should use crawl Chrome PID"
                )
                assert snapshot_cdp_url == crawl_cdp_url, (
                    f"Snapshot {snap_num} should use crawl CDP URL"
                )
                tab_process.send_signal(signal.SIGTERM)
                tab_process.wait(timeout=10)

            # All target IDs should be unique (different tabs)
            assert len(set(target_ids)) == 3, (
                f"All snapshots should have unique tabs: {target_ids}"
            )

            # Chrome should still be running with all 3 tabs
            try:
                os.kill(chrome_pid, 0)
            except OSError:
                raise AssertionError(
                    "Chrome should still be running after creating 3 tabs",
                )
        finally:
            if chrome_launch_process is not None:
                _cleanup_launch_process(chrome_launch_process, chrome_dir)


def test_chrome_cleanup_on_crawl_end():
    """Integration test: Chrome cleanup at end of crawl."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        launch_env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(crawl_dir),
            CHROME_HEADLESS="true",
        )
        # Launch Chrome in background
        chrome_launch_process = subprocess.Popen(
            [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-crawl-end"],
            cwd=str(chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=launch_env,
        )

        wait_for_chrome_session_state(
            chrome_dir,
            env=launch_env,
            require_browser_ready=True,
        )
        assert chrome_launch_process.poll() is None, "Chrome launch exited early"

        # Verify Chrome is running
        assert (chrome_dir / "chrome.pid").exists(), "Chrome PID file should exist"
        chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())

        try:
            os.kill(chrome_pid, 0)
        except OSError:
            raise AssertionError("Chrome should be running")

        # Send SIGTERM to chrome launch process
        chrome_launch_process.send_signal(signal.SIGTERM)
        stdout, stderr = chrome_launch_process.communicate(timeout=15)

        assert chrome_launch_process.returncode == 0, stderr
        assert {"succeeded": True, "skipped": False} in [
            json.loads(line) for line in stdout.splitlines() if line.startswith("{")
        ]
        assert not is_pid_alive(chrome_pid), "Chrome should be killed after SIGTERM"

        assert not (chrome_dir / "chrome.pid").exists(), (
            "chrome.pid should be removed during Chrome cleanup"
        )
        assert not (chrome_dir / "cdp_url.txt").exists(), (
            "cdp_url.txt should be removed during Chrome cleanup"
        )


@pytest.mark.parametrize(
    ("isolation", "launch_hook"),
    [
        pytest.param("crawl", CHROME_LAUNCH_HOOK, id="crawl"),
        pytest.param("snapshot", CHROME_SNAPSHOT_LAUNCH_HOOK, id="snapshot"),
    ],
)
def test_chrome_cleanup_during_launch_uses_persisted_session_state(
    chrome_test_url,
    isolation,
    launch_hook,
):
    """SIGTERM after chrome.pid publication must stop the in-progress browser."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        snapshot_dir = Path(tmpdir) / "snapshot"
        chrome_dir = (
            crawl_dir / "chrome" if isolation == "crawl" else snapshot_dir / "chrome"
        )
        chrome_dir.mkdir(parents=True)
        launch_env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(crawl_dir),
            SNAP_DIR=str(snapshot_dir),
            CHROME_HEADLESS="true",
            CHROME_ISOLATION=isolation,
        )
        extensions_dir = Path(get_extensions_dir(env=launch_env))
        _install_test_extension(extensions_dir, launch_env)
        launch_env["CHROMEWEBSTORE_EXTENSIONS_DIR"] = str(extensions_dir)

        stdout_handle = (chrome_dir / "early_cleanup.stdout.log").open(
            "w+",
            encoding="utf-8",
        )
        launch_process = subprocess.Popen(
            [
                str(launch_hook),
                f"--url={chrome_test_url}",
                "--snapshot-id=snap-early-cleanup",
                "--crawl-id=test-early-cleanup",
            ],
            cwd=str(chrome_dir),
            stdout=stdout_handle,
            stderr=subprocess.PIPE,
            text=True,
            env=launch_env,
        )
        stderr_stream = launch_process.stderr
        assert stderr_stream is not None
        launch_boundary = threading.Event()
        stderr_lines: list[str] = []

        def collect_stderr() -> None:
            try:
                for line in stderr_stream:
                    stderr_lines.append(line)
                    if "[*] Waiting for debug port " in line:
                        launch_boundary.set()
            finally:
                launch_boundary.set()

        stderr_thread = threading.Thread(target=collect_stderr, daemon=True)
        stderr_thread.start()
        chrome_pid = None
        try:
            assert launch_boundary.wait(timeout=30), (
                "Chrome launch did not reach its post-spawn debug-port boundary"
            )
            stderr = "".join(stderr_lines)
            assert "[*] Waiting for debug port " in stderr, stderr
            chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())
            assert is_pid_alive(chrome_pid)

            launch_process.send_signal(signal.SIGTERM)
            launch_process.wait(timeout=15)
            stderr_thread.join(timeout=5)
            assert not stderr_thread.is_alive()
            stdout_handle.flush()
            stdout = (chrome_dir / "early_cleanup.stdout.log").read_text()
            stderr = "".join(stderr_lines)

            assert launch_process.returncode == 0, (
                f"early cleanup failed:\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
            assert "Cleaning up in-progress local Chrome from persisted state" in stderr
            if isolation == "crawl":
                assert "[+] chromium session started" not in stderr
            else:
                assert '"status":"succeeded"' not in stdout
            assert not is_pid_alive(chrome_pid)
            _assert_snapshot_browser_state_cleared(chrome_dir)
        finally:
            if launch_process.poll() is None:
                launch_process.send_signal(signal.SIGTERM)
                launch_process.wait(timeout=15)
            if chrome_pid is not None and is_pid_alive(chrome_pid):
                assert kill_chrome(chrome_pid, str(chrome_dir))
            stderr_stream.close()
            stderr_thread.join(timeout=5)
            stdout_handle.close()


def test_zombie_prevention_hook_killed():
    """Integration test: Chrome is killed even if hook process is SIGKILL'd."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        launch_env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(crawl_dir),
            CHROME_HEADLESS="true",
        )
        # Launch Chrome
        chrome_launch_process = subprocess.Popen(
            [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-zombie"],
            cwd=str(chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=launch_env,
        )

        wait_for_chrome_session_state(
            chrome_dir,
            env=launch_env,
            require_browser_ready=True,
        )
        assert chrome_launch_process.poll() is None, "Chrome launch exited early"

        assert (chrome_dir / "chrome.pid").exists(), "Chrome PID file should exist"

        chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())
        hook_pid = (
            chrome_launch_process.pid
        )  # Use the Popen process PID instead of hook.pid file

        # Verify both Chrome and hook are running
        try:
            os.kill(chrome_pid, 0)
            os.kill(hook_pid, 0)
        except OSError:
            raise AssertionError("Both Chrome and hook should be running")

        # Simulate hook getting SIGKILL'd (can't cleanup)
        os.kill(hook_pid, signal.SIGKILL)
        chrome_launch_process.wait(timeout=10)

        # Chrome should still be running (orphaned)
        try:
            os.kill(chrome_pid, 0)
        except OSError:
            raise AssertionError("Chrome should still be running after hook SIGKILL")

        # Simulate Crawl.cleanup() using the shared Chrome cleanup logic.
        assert kill_chrome(chrome_pid, str(chrome_dir)), (
            "shared kill_chrome cleanup should terminate the orphaned browser"
        )

        # Chrome should now be dead
        with pytest.raises(ProcessLookupError):
            os.kill(chrome_pid, 0)


def test_kill_zombie_chrome_respects_live_crawl_heartbeat():
    """Zombie cleanup must not kill Chrome while the owning crawl heartbeat is live."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root_dir = Path(tmpdir)
        crawl_dir = root_dir / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        launch_env = _isolated_test_env(
            tmpdir,
            CRAWL_DIR=str(crawl_dir),
            CHROME_HEADLESS="true",
        )
        chrome_launch_process = subprocess.Popen(
            [str(CHROME_LAUNCH_HOOK), "--crawl-id=test-live-heartbeat"],
            cwd=str(chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=launch_env,
        )

        try:
            wait_for_chrome_session_state(
                chrome_dir,
                env=launch_env,
                require_browser_ready=True,
            )
            assert chrome_launch_process.poll() is None, "Chrome launch exited early"

            assert (chrome_dir / "chrome.pid").exists(), "Chrome PID file should exist"
            chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())
            os.kill(chrome_pid, 0)

            (crawl_dir / ".heartbeat.json").write_text(
                json.dumps(
                    {
                        "runtime": "abx-dl",
                        "crawl_id": "test-live-heartbeat",
                        "owner_pid": os.getpid(),
                        "last_alive_at": time.time(),
                        "kill_after_seconds": 180,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )

            returncode, stdout, stderr = _call_chrome_utils(
                "killZombieChrome",
                str(root_dir),
                env=get_test_env(),
            )
            assert returncode == 0, stderr
            assert stdout.strip() == "0", stdout
            os.kill(chrome_pid, 0)
        finally:
            _cleanup_launch_process(chrome_launch_process, chrome_dir)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
