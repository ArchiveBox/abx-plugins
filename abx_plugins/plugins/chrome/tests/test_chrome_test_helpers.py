"""
Tests for chrome_test_helpers.py functions.

These tests verify the Python helper functions used across Chrome plugin tests.
"""

import json
import os
import subprocess
import pytest
import tempfile
from pathlib import Path

from abx_plugins.plugins.base.test_utils import (
    get_hook_script,
    get_plugin_dir,
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
    find_chromium_binary,
    install_chromium_with_hooks,
    setup_test_env,
)

TEST_URL = "https://example.com"


def _write_fake_browser_binary(path: Path, label: str = "Chromium") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho '{label} 123.0.0.0'\n")
    path.chmod(0o755)


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


def test_get_lib_dir_with_env_var():
    """Test get_lib_dir() respects LIB_DIR env var."""
    with tempfile.TemporaryDirectory() as tmpdir:
        custom_lib = Path(tmpdir) / "custom_lib"
        custom_lib.mkdir()

        old_lib_dir = os.environ.get("LIB_DIR")
        try:
            os.environ["LIB_DIR"] = str(custom_lib)
            lib_dir = get_lib_dir()
            assert lib_dir == custom_lib
        finally:
            if old_lib_dir:
                os.environ["LIB_DIR"] = old_lib_dir
            else:
                os.environ.pop("LIB_DIR", None)


def test_get_node_modules_dir_with_env_var():
    """Test get_node_modules_dir() respects NODE_MODULES_DIR env var."""
    with tempfile.TemporaryDirectory() as tmpdir:
        custom_nm = Path(tmpdir) / "node_modules"
        custom_nm.mkdir()

        old_nm_dir = os.environ.get("NODE_MODULES_DIR")
        try:
            os.environ["NODE_MODULES_DIR"] = str(custom_nm)
            nm_dir = get_node_modules_dir()
            assert nm_dir == custom_nm
        finally:
            if old_nm_dir:
                os.environ["NODE_MODULES_DIR"] = old_nm_dir
            else:
                os.environ.pop("NODE_MODULES_DIR", None)


def test_get_extensions_dir_default():
    """Test get_extensions_dir() returns expected path format."""
    ext_dir = get_extensions_dir()
    assert isinstance(ext_dir, str)
    assert "personas" in ext_dir
    assert "chrome_extensions" in ext_dir


def test_get_extensions_dir_with_custom_persona():
    """Test get_extensions_dir() respects ACTIVE_PERSONA env var."""
    old_persona = os.environ.get("ACTIVE_PERSONA")
    old_personas_dir = os.environ.get("PERSONAS_DIR")
    try:
        os.environ["ACTIVE_PERSONA"] = "TestPersona"
        os.environ["PERSONAS_DIR"] = "/tmp/test-personas"
        ext_dir = get_extensions_dir()
        assert "TestPersona" in ext_dir
        assert "/tmp/test-personas" in ext_dir
    finally:
        if old_persona:
            os.environ["ACTIVE_PERSONA"] = old_persona
        else:
            os.environ.pop("ACTIVE_PERSONA", None)
        if old_personas_dir:
            os.environ["PERSONAS_DIR"] = old_personas_dir
        else:
            os.environ.pop("PERSONAS_DIR", None)


def test_get_test_env_returns_dict():
    """Test get_test_env() returns properly formatted environment dict."""
    env = get_test_env()
    assert isinstance(env, dict)

    # Should include key paths
    assert "MACHINE_TYPE" in env
    assert "LIB_DIR" in env
    assert "NODE_MODULES_DIR" in env
    assert "NODE_PATH" in env  # Critical for module resolution
    assert "NPM_BIN_DIR" in env
    assert "CHROME_EXTENSIONS_DIR" in env

    # Verify NODE_PATH equals NODE_MODULES_DIR (for Node.js module resolution)
    assert env["NODE_PATH"] == env["NODE_MODULES_DIR"]


def test_get_test_env_paths_are_absolute():
    """Test that get_test_env() returns absolute paths."""
    env = get_test_env()

    # All path-like values should be absolute
    assert Path(env["LIB_DIR"]).is_absolute()
    assert Path(env["NODE_MODULES_DIR"]).is_absolute()
    assert Path(env["NODE_PATH"]).is_absolute()


def test_find_chromium_binary():
    """Test find_chromium_binary() returns a path or None."""
    binary = find_chromium_binary()
    if binary:
        assert isinstance(binary, str)
        # Should be an absolute path if found
        assert os.path.isabs(binary)


def test_find_chromium_uses_canonical_managed_puppeteer_cache_dir(tmp_path: Path):
    """findChromium() should resolve binaries from LIB_DIR/puppeteer/chrome."""
    binary_path = (
        tmp_path
        / "lib"
        / "puppeteer"
        / "chrome"
        / "chromium"
        / "123456"
        / "chrome-linux64"
        / "chrome"
    )
    _write_fake_browser_binary(binary_path)

    env = os.environ.copy()
    env.update(
        {
            "LIB_DIR": str(tmp_path / "lib"),
            "HOME": str(tmp_path / "home"),
        }
    )
    env.pop("CHROME_BINARY", None)

    returncode, stdout, stderr = _call_chrome_utils("findChromium", env=env)

    assert returncode == 0, stderr
    assert stdout.strip() == str(binary_path)


@pytest.mark.parametrize(
    ("browser_name", "label"),
    [("chrome", "Google Chrome"), ("chromium", "Chromium")],
)
def test_find_chromium_accepts_command_name_chrome_binary(
    tmp_path: Path, browser_name: str, label: str
):
    """CHROME_BINARY should accept command names, not only filesystem paths."""
    binary_path = tmp_path / "bin" / browser_name
    _write_fake_browser_binary(binary_path, label=label)

    env = os.environ.copy()
    env.update(
        {
            "CHROME_BINARY": browser_name,
            "PATH": f"{binary_path.parent}{os.pathsep}{env.get('PATH', '')}",
        }
    )

    returncode, stdout, stderr = _call_chrome_utils("findChromium", env=env)

    assert returncode == 0, stderr
    assert stdout.strip() == str(binary_path)


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
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

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

    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      if (fs.existsSync(expectedPath) && fs.statSync(expectedPath).size > 0) {
        process.stdout.write(JSON.stringify({
          ok,
          expectedPath,
          pageUrl: page.url(),
          content: fs.readFileSync(expectedPath, 'utf8'),
        }));
        return;
      }
      await sleep(200);
    }
    throw new Error(`Timed out waiting for download at ${expectedPath}`);
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
                ["node", "-e", script, str(CHROME_UTILS), str(snapshot_chrome_dir)],
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
    hook = get_hook_script(CHROME_PLUGIN_DIR, "on_Crawl__*_chrome_launch.*")

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


def test_machine_type_consistency():
    """Test that machine type is consistent across calls."""
    mt1 = get_machine_type()
    mt2 = get_machine_type()
    assert mt1 == mt2, "Machine type should be stable across calls"


def test_lib_dir_is_directory():
    """Test that lib_dir points to an actual directory when HOME is set."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = tmpdir
            lib_dir = Path(tmpdir) / ".config" / "abx" / "lib"
            lib_dir.mkdir(parents=True, exist_ok=True)

            result = get_lib_dir()
            assert isinstance(result, Path)
        finally:
            if old_home:
                os.environ["HOME"] = old_home
            else:
                os.environ.pop("HOME", None)


def test_install_chromium_with_hooks_reuses_existing_chromium_via_env(tmp_path: Path):
    """Use public env inputs only: existing CHROME_BINARY should be reused."""
    chromium_path = tmp_path / "chromium"
    chromium_path.write_text("#!/bin/sh\nexit 0\n")
    chromium_path.chmod(0o755)

    # Provide a minimal local puppeteer package so require.resolve('puppeteer')
    # succeeds without network installs.
    node_modules_dir = tmp_path / "lib" / "npm" / "node_modules"
    puppeteer_dir = node_modules_dir / "puppeteer"
    puppeteer_dir.mkdir(parents=True, exist_ok=True)
    (puppeteer_dir / "package.json").write_text(
        '{"name":"puppeteer","version":"0.0.0","main":"index.js"}\n'
    )
    (puppeteer_dir / "index.js").write_text("module.exports = {};\n")

    env = get_test_env()
    env.update(
        {
            "CHROME_BINARY": str(chromium_path),
            "LIB_DIR": str(tmp_path / "lib"),
            "NODE_MODULES_DIR": str(node_modules_dir),
            "NODE_PATH": str(node_modules_dir),
        }
    )
    resolved = install_chromium_with_hooks(env, timeout=1)

    assert resolved == str(chromium_path)
    assert env["CHROME_BINARY"] == str(chromium_path)


def test_setup_test_env_provisions_extension_runtime_dirs(tmp_path: Path):
    """Extension test env should include explicit downloads and user-data dirs."""
    env = setup_test_env(tmp_path)

    downloads_dir = Path(env["CHROME_DOWNLOADS_DIR"])
    user_data_dir = Path(env["CHROME_USER_DATA_DIR"])
    extensions_dir = Path(env["CHROME_EXTENSIONS_DIR"])

    assert downloads_dir.is_dir()
    assert user_data_dir.is_dir()
    assert extensions_dir.is_dir()
    assert downloads_dir.parent == extensions_dir.parent
    assert user_data_dir.parent == extensions_dir.parent


def test_session_fixture_preserves_runtime_chrome_binary_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Session fixture should default CHROME_BINARY, not overwrite explicit overrides."""
    import abx_plugins.plugins.chrome.tests.chrome_test_helpers as helpers

    runtime_binary = tmp_path / "runtime-chromium"
    installed_binary = tmp_path / "hook-chromium"
    _write_fake_browser_binary(runtime_binary)
    _write_fake_browser_binary(installed_binary)

    class DummyTmpPathFactory:
        def mktemp(self, name: str) -> Path:
            path = tmp_path / name
            path.mkdir(parents=True, exist_ok=True)
            return path

    monkeypatch.setenv("CHROME_BINARY", str(runtime_binary))
    monkeypatch.delenv("SNAP_DIR", raising=False)
    monkeypatch.delenv("PERSONAS_DIR", raising=False)
    monkeypatch.setattr(helpers, "get_test_env", lambda: {})
    monkeypatch.setattr(
        helpers,
        "install_chromium_with_hooks",
        lambda env: str(installed_binary),
    )

    resolved = helpers.ensure_chromium_and_puppeteer_installed_impl(
        DummyTmpPathFactory()
    )

    assert resolved == str(installed_binary)
    assert os.environ["CHROME_BINARY"] == str(runtime_binary)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
