"""
Tests for chrome_test_helpers.py functions.

These tests verify the Python helper functions used across Chrome plugin tests.
"""

import json
import os
import subprocess
import sys
import pytest
import tempfile
from pathlib import Path

from abx_plugins.plugins.base.testing import (
    get_hook_script,
    get_plugin_dir,
    install_binary_with_abxpkg,
    parse_jsonl_output,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    _call_chrome_utils,
    CHROME_UTILS,
    chrome_session,
    get_test_env,
    get_machine_type,
    get_lib_dir,
    get_node_modules_dir,
    get_extensions_dir,
    install_chromium_with_abxpkg,
    setup_test_env,
)

TEST_URL = "https://example.com"


def _python_binary() -> str:
    loaded = install_binary_with_abxpkg("python3", binproviders="env,apt,brew")
    assert loaded.loaded_abspath is not None
    return str(loaded.loaded_abspath)


@pytest.fixture(scope="module")
def real_chromium_binary(ensure_chrome_test_prereqs) -> Path:
    path = Path(str(ensure_chrome_test_prereqs))
    assert path.exists()
    assert _is_supported_browser_path(path)
    return path


def _is_supported_browser_path(path: Path) -> bool:
    env = {**os.environ, **get_test_env()}
    returncode, stdout, _stderr = _call_chrome_utils(
        "isSupportedChromiumBinary",
        str(path),
        env=env,
    )
    return returncode == 0 and stdout.strip().lower() == "true"


def test_get_machine_type():
    """Test get_machine_type() returns valid format."""
    machine_type = get_machine_type()
    assert isinstance(machine_type, str)
    assert "-" in machine_type, "Machine type should be in format: arch-os"
    # Should be one of the expected formats
    assert any(x in machine_type for x in ["arm64", "x86_64"]), (
        "Should contain valid architecture"
    )
    assert any(x in machine_type for x in ["darwin", "linux", "win32"]), (
        "Should contain valid OS"
    )


def test_get_lib_dir_with_env_var(tmp_path: Path):
    """Test get_lib_dir() respects ABXPKG_LIB_DIR env var."""
    custom_lib = tmp_path / "custom_lib"
    custom_lib.mkdir()
    env = os.environ.copy()
    env["ABXPKG_LIB_DIR"] = str(custom_lib)
    result = subprocess.run(
        [
            _python_binary(),
            "-c",
            "from abx_plugins.plugins.chrome.tests.chrome_test_helpers import get_lib_dir; print(get_lib_dir())",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == custom_lib


def test_get_node_modules_dir_resolves_runtime_env(ensure_chrome_test_prereqs):
    """Test get_node_modules_dir() uses the provider-built Chrome hook env."""
    nm_dir = get_node_modules_dir()
    assert nm_dir.is_dir()
    assert nm_dir.name == "node_modules"

    returncode, stdout, stderr = _call_chrome_utils(
        "getNodeModulesDir",
        env=get_test_env(),
    )
    assert returncode == 0, stderr
    assert Path(stdout.strip()) == nm_dir


def test_get_extensions_dir_default():
    """Test get_extensions_dir() returns expected path format."""
    ext_dir = get_extensions_dir()
    assert isinstance(ext_dir, str)
    ext_path = Path(ext_dir)
    assert ext_path.is_absolute()
    assert ext_path.name == "extensions"
    assert ext_path.parent.name == "chromewebstore"


def test_get_extensions_dir_ignores_persona_by_default(tmp_path: Path):
    """Test get_extensions_dir() uses the provider-managed ABXPKG_LIB_DIR path by default."""
    personas_dir = tmp_path / "test-personas"
    env = os.environ.copy()
    env.update(
        {
            "ACTIVE_PERSONA": "TestPersona",
            "PERSONAS_DIR": str(personas_dir),
        },
    )
    result = subprocess.run(
        [
            _python_binary(),
            "-c",
            "from abx_plugins.plugins.chrome.tests.chrome_test_helpers import get_extensions_dir; print(get_extensions_dir())",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    ext_dir = result.stdout.strip()
    assert "TestPersona" not in ext_dir
    assert str(personas_dir) not in ext_dir
    assert Path(ext_dir).name == "extensions"
    assert Path(ext_dir).parent.name == "chromewebstore"


def test_chrome_extension_install_env_isolates_inherited_extensions_dir(
    tmp_path: Path,
):
    """An explicit install root must not reuse the process-wide extension cache."""
    inherited_extensions_dir = tmp_path / "global-lib" / "chromewebstore" / "extensions"
    child_env = os.environ.copy()
    child_env["CHROMEWEBSTORE_EXTENSIONS_DIR"] = str(inherited_extensions_dir)
    script = """
import json
import sys
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_extension_install_env
env, extensions_dir = chrome_extension_install_env(sys.argv[1])
print(json.dumps({'env': env, 'extensions_dir': str(extensions_dir)}))
"""
    result = subprocess.run(
        [_python_binary(), "-c", script, str(tmp_path / "isolated")],
        capture_output=True,
        text=True,
        timeout=30,
        env=child_env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    env = payload["env"]
    extensions_dir = Path(payload["extensions_dir"])

    expected = tmp_path / "isolated" / "lib" / "chromewebstore" / "extensions"
    assert extensions_dir == expected.resolve()
    assert "CHROMEWEBSTORE_EXTENSIONS_DIR" not in env


def test_call_chrome_utils_builds_chrome_required_binary_exec_env(
    ensure_chrome_test_prereqs,
    tmp_path: Path,
):
    """Direct chrome_utils.js test calls must get the same env Chrome hooks get."""
    env = os.environ.copy()
    env["ABXPKG_LIB_DIR"] = os.environ["ABXPKG_LIB_DIR"]
    env["SNAP_DIR"] = str(tmp_path / "snap")
    env["CRAWL_DIR"] = str(tmp_path / "crawl")
    env["PERSONAS_DIR"] = str(tmp_path / "personas")
    env["ACTIVE_PERSONA"] = "Default"
    for path_key in ("SNAP_DIR", "CRAWL_DIR", "PERSONAS_DIR"):
        Path(env[path_key]).mkdir(parents=True, exist_ok=True)
    for inherited_key in (
        "NODE_MODULES_DIR",
        "NODE_PATH",
        "PNPM_HOME",
        "PNPM_BIN_DIR",
        "NPM_BIN_DIR",
        "CHROMEWEBSTORE_EXTENSIONS_DIR",
    ):
        env.pop(inherited_key, None)

    returncode, stdout, stderr = _call_chrome_utils("getNodeModulesDir", env=env)

    assert returncode == 0, stderr
    assert Path(stdout.strip()).is_dir()
    assert Path(stdout.strip()).name == "node_modules"


def test_get_test_env_returns_dict():
    """Test get_test_env() returns properly formatted environment dict."""
    env = get_test_env()
    assert isinstance(env, dict)

    # Should include key paths
    assert "MACHINE_TYPE" in env
    assert "ABXPKG_LIB_DIR" in env
    assert "NODE_MODULES_DIR" in env
    assert "NODE_PATH" in env  # Critical for module resolution
    assert "NPM_BIN_DIR" in env
    assert "CHROMEWEBSTORE_EXTENSIONS_DIR" in env
    assert Path(env["CHROMEWEBSTORE_EXTENSIONS_DIR"]).is_absolute()

    # The provider-built NODE_PATH can include several real package roots
    # (chrome, playwright, abxbus, etc.). The important contract for test
    # subprocesses is that the chrome package root remains present so imports
    # resolve exactly as they do under the runtime hook/shebang path.
    assert env["NODE_MODULES_DIR"] in env["NODE_PATH"].split(os.pathsep)


def test_get_test_env_paths_are_absolute():
    """Test that get_test_env() returns absolute paths."""
    env = get_test_env()

    # All path-like values should be absolute
    assert Path(env["ABXPKG_LIB_DIR"]).is_absolute()
    assert Path(env["NODE_MODULES_DIR"]).is_absolute()
    assert Path(env["NODE_PATH"]).is_absolute()


def test_find_chromium_uses_abxpkg_resolved_browser(real_chromium_binary: Path):
    """findChromium() should return the exact path prepared by abxpkg."""
    env = {
        **os.environ,
        **get_test_env(),
        "CHROME_BINARY": str(real_chromium_binary),
    }

    returncode, stdout, stderr = _call_chrome_utils("findChromium", env=env)

    assert returncode == 0, stderr
    assert stdout.strip() == str(real_chromium_binary)
    resolved = Path(stdout.strip())
    assert resolved.samefile(real_chromium_binary)
    assert _is_supported_browser_path(resolved)


def test_set_browser_download_behavior_downloads_file_with_live_page(
    ensure_chrome_test_prereqs,
):
    """setBrowserDownloadBehavior() should drive a real download on a live browser page."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with chrome_session(
            Path(tmpdir),
            crawl_id="test-download-config",
            snapshot_id="snap-download-config",
            test_url=TEST_URL,
            navigate=True,
            timeout=45,
        ) as (_chrome_proc, _chrome_pid, snapshot_chrome_dir, env):
            download_dir = snapshot_chrome_dir.parent / "download-test"
            download_dir.mkdir(parents=True, exist_ok=True)
            script = """
const fs = require('fs');
const path = require('path');
const chromeUtils = require(process.argv[1]);
const chromeSessionDir = process.argv[2];
const downloadDir = process.argv[3];
const filename = 'abx-download.txt';
const expectedPath = path.join(downloadDir, filename);

function waitForDownload(filename) {
  return new Promise((resolve, reject) => {
    const watcher = fs.watch(downloadDir, (_eventType, changedName) => {
      if (changedName && changedName.toString() === filename) {
        clearTimeout(timeout);
        watcher.close();
        resolve();
      }
    });
    const timeout = setTimeout(() => {
      watcher.close();
      reject(new Error(`Timed out waiting for download event for ${filename}`));
    }, 15000);
  });
}

(async () => {
  const { browser, page } = await chromeUtils.connectToPage({
    chromeSessionDir,
    timeoutMs: 30000,
  });
  try {
    const ok = await chromeUtils.setBrowserDownloadBehavior({
      page,
      downloadPath: downloadDir,
    });
    const downloadCompleted = waitForDownload(filename);
    await page.bringToFront();
    await page.evaluate((name) => {
      const blob = new Blob(['archivebox-download-ok'], { type: 'text/plain' });
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    }, filename);
    await downloadCompleted;
    process.stdout.write(JSON.stringify({
      ok,
      expectedPath,
      pageUrl: page.url(),
      content: fs.readFileSync(expectedPath, 'utf8'),
    }));
  } finally {
    await browser.disconnect();
  }
})().catch((error) => {
  console.error(error.stack || error.message);
  process.exit(1);
});
"""
            result = subprocess.run(
                [
                    "node",
                    "-e",
                    script,
                    str(CHROME_UTILS),
                    str(snapshot_chrome_dir),
                    str(download_dir),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )

            assert result.returncode == 0, result.stderr
            payload = json.loads(result.stdout)
            assert payload["ok"] is True
            assert payload["pageUrl"].startswith(TEST_URL)
            assert payload["content"] == "archivebox-download-ok"
            assert Path(payload["expectedPath"]).exists()


def test_set_browser_download_behavior_keeps_shared_dir_stable_for_live_pages(
    ensure_chrome_test_prereqs,
):
    """Two pages in one browser should download into one stable shared dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with chrome_session(
            Path(tmpdir),
            crawl_id="test-download-pages",
            snapshot_id="snap-download-pages",
            test_url=TEST_URL,
            navigate=True,
            timeout=45,
        ) as (_chrome_proc, _chrome_pid, snapshot_chrome_dir, env):
            download_dir = snapshot_chrome_dir.parent / "downloads"
            download_dir.mkdir(parents=True, exist_ok=True)
            script = """
const fs = require('fs');
const path = require('path');
const chromeUtils = require(process.argv[1]);
const chromeSessionDir = process.argv[2];
const downloadDir = process.argv[3];

async function triggerDownload(page, filename, content) {
  await page.bringToFront();
  await page.evaluate((name, body) => {
    const blob = new Blob([body], { type: 'text/plain' });
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }, filename, content);
}

function waitForDownloads(filenames) {
  return new Promise((resolve, reject) => {
    const remaining = new Set(filenames);
    const watcher = fs.watch(downloadDir, (_eventType, changedName) => {
      if (!changedName) return;
      remaining.delete(changedName.toString());
      if (remaining.size === 0) {
        clearTimeout(timeout);
        watcher.close();
        resolve();
      }
    });
    const timeout = setTimeout(() => {
      watcher.close();
      reject(new Error(`Timed out waiting for download events: ${[...remaining]}`));
    }, 15000);
  });
}

(async () => {
  const { browser, page: pageOne } = await chromeUtils.connectToPage({
    chromeSessionDir,
    timeoutMs: 30000,
  });
  const pageTwo = await browser.newPage();
  try {
    await pageTwo.goto('data:text/html,<html><body>two</body></html>');
    await chromeUtils.setBrowserDownloadBehavior({
      page: pageOne,
      downloadPath: downloadDir,
    });
    await chromeUtils.setBrowserDownloadBehavior({
      page: pageTwo,
      downloadPath: downloadDir,
    });

    const downloadsCompleted = waitForDownloads(['one.txt', 'two.txt']);
    await triggerDownload(pageOne, 'one.txt', 'page-one-ok');
    await triggerDownload(pageTwo, 'two.txt', 'page-two-ok');
    await downloadsCompleted;

    const onePath = path.join(downloadDir, 'one.txt');
    const twoPath = path.join(downloadDir, 'two.txt');

    process.stdout.write(JSON.stringify({
      onePath,
      twoPath,
      oneContent: fs.readFileSync(onePath, 'utf8'),
      twoContent: fs.readFileSync(twoPath, 'utf8'),
    }));
  } finally {
    await browser.disconnect();
  }
})().catch((error) => {
  console.error(error.stack || error.message);
  process.exit(1);
});
"""
            result = subprocess.run(
                [
                    "node",
                    "-e",
                    script,
                    str(CHROME_UTILS),
                    str(snapshot_chrome_dir),
                    str(download_dir),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )

            assert result.returncode == 0, result.stderr
            payload = json.loads(result.stdout)
            assert payload["oneContent"] == "page-one-ok"
            assert payload["twoContent"] == "page-two-ok"
            assert Path(payload["onePath"]).exists()
            assert Path(payload["twoPath"]).exists()


def test_set_browser_download_behavior_requires_download_path_with_live_page(
    ensure_chrome_test_prereqs,
):
    """Download setup failures must hard-fail for snapshot-level download extractors."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with chrome_session(
            Path(tmpdir),
            crawl_id="test-download-config-missing",
            snapshot_id="snap-download-config-missing",
            test_url=TEST_URL,
            navigate=True,
            timeout=45,
        ) as (_chrome_proc, _chrome_pid, snapshot_chrome_dir, env):
            script = """
const chromeUtils = require(process.argv[1]);
const chromeSessionDir = process.argv[2];

(async () => {
  const { browser, page } = await chromeUtils.connectToPage({
    chromeSessionDir,
    timeoutMs: 30000,
  });
  try {
    await chromeUtils.setBrowserDownloadBehavior({ page });
    process.stdout.write(JSON.stringify({ ok: true }));
  } catch (error) {
    process.stdout.write(JSON.stringify({
      ok: false,
      error: error.message,
      pageUrl: page.url(),
    }));
  } finally {
    await browser.disconnect();
  }
})().catch((error) => {
  console.error(error.stack || error.message);
  process.exit(1);
});
"""
            result = subprocess.run(
                [
                    env["NODE_BINARY"],
                    "-e",
                    script,
                    str(CHROME_UTILS),
                    str(snapshot_chrome_dir),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )

            assert result.returncode == 0, result.stderr
            payload = json.loads(result.stdout)
            assert payload["ok"] is False
            assert (
                payload["error"] == "setBrowserDownloadBehavior requires downloadPath"
            )
            assert payload["pageUrl"].startswith(TEST_URL)


def test_get_plugin_dir():
    """Test get_plugin_dir() finds correct plugin directory."""
    # Use this test file's path
    test_file = __file__
    plugin_dir = get_plugin_dir(test_file)

    assert plugin_dir.exists()
    assert plugin_dir.is_dir()
    # Should be the chrome plugin directory
    assert plugin_dir.name == "chrome"
    assert plugin_dir.parent.name == "plugins"


def test_get_hook_script_finds_existing_hook():
    """Test get_hook_script() can find an existing hook."""
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import CHROME_PLUGIN_DIR

    # Try to find the chrome launch hook
    hook = get_hook_script(CHROME_PLUGIN_DIR, "on_CrawlSetup__*_chrome_launch.*")

    if hook:  # May not exist in all test environments
        assert hook.exists()
        assert hook.is_file()
        assert "chrome_launch" in hook.name


def test_get_hook_script_returns_none_for_missing():
    """Test get_hook_script() returns None for non-existent hooks."""
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import CHROME_PLUGIN_DIR

    hook = get_hook_script(CHROME_PLUGIN_DIR, "nonexistent_hook_*_pattern.*")
    assert hook is None


def test_parse_jsonl_output_valid():
    """Test parse_jsonl_output() parses valid JSONL."""
    jsonl_output = """{"type": "ArchiveResult", "status": "succeeded", "output": "test1"}
{"type": "ArchiveResult", "status": "failed", "error": "test2"}
"""

    # Returns first match only
    result = parse_jsonl_output(jsonl_output)
    assert result is not None
    assert result["type"] == "ArchiveResult"
    assert result["status"] == "succeeded"
    assert result["output"] == "test1"


def test_parse_jsonl_output_with_non_json_lines():
    """Test parse_jsonl_output() skips non-JSON lines."""
    mixed_output = """Some non-JSON output
{"type": "ArchiveResult", "status": "succeeded"}
More non-JSON
{"type": "ArchiveResult", "status": "failed"}
"""

    result = parse_jsonl_output(mixed_output)
    assert result is not None
    assert result["type"] == "ArchiveResult"
    assert result["status"] == "succeeded"


def test_parse_jsonl_output_empty():
    """Test parse_jsonl_output() handles empty input."""
    result = parse_jsonl_output("")
    assert result is None


def test_parse_jsonl_output_filters_by_type():
    """Test parse_jsonl_output() can filter by record type."""
    jsonl_output = """{"type": "LogEntry", "data": "log1"}
{"type": "ArchiveResult", "data": "result1"}
{"type": "ArchiveResult", "data": "result2"}
"""

    # Should return first ArchiveResult, not LogEntry
    result = parse_jsonl_output(jsonl_output, record_type="ArchiveResult")
    assert result is not None
    assert result["type"] == "ArchiveResult"
    assert result["data"] == "result1"  # First ArchiveResult


def test_parse_jsonl_output_filters_custom_type():
    """Test parse_jsonl_output() can filter by custom record type."""
    jsonl_output = """{"type": "ArchiveResult", "data": "result1"}
{"type": "LogEntry", "data": "log1"}
{"type": "ArchiveResult", "data": "result2"}
"""

    result = parse_jsonl_output(jsonl_output, record_type="LogEntry")
    assert result is not None
    assert result["type"] == "LogEntry"
    assert result["data"] == "log1"


def test_get_lib_dir_uses_platform_user_config_dir_by_default(
    tmp_path: Path,
):
    """Default ABXPKG_LIB_DIR should follow the platform user config root."""
    home_dir = tmp_path / "home"
    xdg_config_home = tmp_path / "xdg-config"
    home_dir.mkdir()
    xdg_config_home.mkdir()

    env = os.environ.copy()
    env.pop("ABXPKG_LIB_DIR", None)
    env.pop("NODE_MODULES_DIR", None)
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(xdg_config_home)
    result = subprocess.run(
        [
            _python_binary(),
            "-c",
            "from abx_plugins.plugins.chrome.tests.chrome_test_helpers import get_lib_dir; print(get_lib_dir())",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stderr

    if sys.platform == "darwin":
        expected = home_dir / "Library" / "Application Support" / "abx" / "lib"
    elif sys.platform == "win32":
        expected = home_dir / "AppData" / "Roaming" / "abx" / "lib"
    else:
        expected = xdg_config_home / "abx" / "lib"
    assert Path(result.stdout.strip()) == expected.resolve()


def test_install_chromium_with_abxpkg_links_existing_chrome_into_managed_env(
    tmp_path: Path,
    real_chromium_binary: Path,
):
    """abxpkg exposes a discovered host browser through its managed env bin."""
    env = get_test_env()
    env.update(
        {
            "CHROME_BINARY": str(real_chromium_binary),
            "ABXPKG_LIB_DIR": str(tmp_path / "lib"),
        },
    )
    resolved = install_chromium_with_abxpkg(env, timeout=120)

    managed_browser = Path(resolved)
    assert env["CHROME_BINARY"] == resolved
    assert managed_browser.parent == tmp_path / "lib" / "env" / "bin"
    assert managed_browser.is_symlink()
    assert managed_browser.samefile(real_chromium_binary)


def test_setup_test_env_uses_derived_runtime_dirs(tmp_path: Path):
    """Extension test env should let runtime config derive browser state dirs."""
    env = setup_test_env(tmp_path)

    extensions_dir = Path(get_extensions_dir(env=env))
    expected_user_data_dir = (
        Path(env["PERSONAS_DIR"]) / env["ACTIVE_PERSONA"] / "chrome_profile"
    )

    assert "CHROME_DOWNLOADS_DIR" not in env
    assert "CHROME_USER_DATA_DIR" not in env
    assert Path(env["CHROMEWEBSTORE_EXTENSIONS_DIR"]) == extensions_dir
    assert env["ACTIVE_PERSONA"] == "Default"
    assert Path(env["PERSONAS_DIR"]).is_dir()
    assert extensions_dir.is_dir()

    script = (
        f"const chromeUtils = require({json.dumps(str(CHROME_UTILS))});\n"
        "process.stdout.write(JSON.stringify(chromeUtils.resolveChromeLaunchOptions({})));\n"
    )
    result = subprocess.run(
        [env["NODE_BINARY"], "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    resolved = json.loads(result.stdout)
    assert Path(resolved["CHROME_USER_DATA_DIR"]) == expected_user_data_dir
    assert Path(resolved["CHROME_DOWNLOADS_DIR"]) == (
        Path(env["PERSONAS_DIR"]) / env["ACTIVE_PERSONA"] / "chrome_downloads"
    )
    assert Path(resolved["CHROMEWEBSTORE_EXTENSIONS_DIR"]) == extensions_dir


def test_session_fixture_exports_abxpkg_resolved_chrome_binary(
    tmp_path: Path,
    real_chromium_binary: Path,
):
    """Session setup should export exactly the path returned by abxpkg."""
    env = os.environ.copy()
    env.update(
        {
            "CHROME_BINARY": str(real_chromium_binary),
            "ABXPKG_LIB_DIR": str(get_lib_dir()),
            "SNAP_DIR": str(tmp_path / "snap"),
            "PERSONAS_DIR": str(tmp_path / "personas"),
            "HOME": str(tmp_path / "home"),
        },
    )
    script = """
import json
import os
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import get_test_env, install_chromium_with_abxpkg
runtime = get_test_env()
resolved = install_chromium_with_abxpkg(runtime)
os.environ['CHROME_BINARY'] = resolved
print(json.dumps({'resolved': resolved, 'exported': os.environ['CHROME_BINARY']}))
"""
    result = subprocess.run(
        [_python_binary(), "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["exported"] == payload["resolved"]
    assert Path(payload["resolved"]).samefile(real_chromium_binary)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
