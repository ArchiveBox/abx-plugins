"""
Integration tests for chrome plugin

Tests verify:
1. Chromium install via @puppeteer/browsers
2. Verify deps with abx-pkg
3. Chrome hooks exist
4. Chromium launches at crawl level
5. Tab creation at snapshot level
6. Tab navigation works
7. Tab cleanup on SIGTERM
8. Chromium cleanup on crawl end

NOTE: We use Chromium instead of Chrome because Chrome 137+ removed support for
--load-extension and --disable-extensions-except flags, which are needed for
loading unpacked extensions in headless mode.
"""

import json
import os
import signal
import subprocess
import time
import urllib.request
import urllib.parse
from pathlib import Path
import pytest

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")
import tempfile

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    get_test_env,
    find_chromium_binary,
    launch_snapshot_tab,
    wait_for_extensions_metadata,
    CHROME_LAUNCH_HOOK,
    CHROME_CRAWL_WAIT_HOOK,
    CHROME_TAB_HOOK,
    CHROME_WAIT_HOOK,
CHROME_NAVIGATE_HOOK,
    CHROME_UTILS,
)

UBLOCK_INSTALL_HOOK = (
    CHROME_UTILS.parent.parent / "ublock" / "on_Crawl__80_install_ublock_extension.js"
)
SEO_HOOK = CHROME_UTILS.parent.parent / "seo" / "on_Snapshot__38_seo.js"


def _get_cookies_via_cdp(port: int, env: dict) -> list[dict]:
    result = subprocess.run(
        ["node", str(CHROME_UTILS), "getCookiesViaCdp", str(port)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, (
        f"Failed to read cookies via CDP: {result.stderr}\nStdout: {result.stdout}"
    )
    return json.loads(result.stdout or "[]")


def _port_from_cdp_url(cdp_url: str) -> int:
    return int(cdp_url.split(":")[2].split("/")[0])


def _fetch_devtools_targets(cdp_url: str) -> list[dict]:
    port = _port_from_cdp_url(cdp_url)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _close_target_via_cdp(cdp_url: str, target_id: str) -> None:
    port = _port_from_cdp_url(cdp_url)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/json/close/{target_id}",
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=10):
        return


def _create_target_via_cdp(cdp_url: str, url: str) -> dict:
    port = _port_from_cdp_url(cdp_url)
    encoded_url = urllib.parse.quote(url, safe="")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/json/new?{encoded_url}",
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _cleanup_session_artifacts(session_dir: Path, env: dict, *, require_target_id: bool = False) -> dict:
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
        ["node", "-e", script, str(CHROME_UTILS), str(session_dir), str(require_target_id).lower()],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    assert result.returncode == 0, (
        f"cleanupStaleChromeSessionArtifacts failed: {result.stderr}\nStdout: {result.stdout}"
    )
    return json.loads(result.stdout.strip())


@pytest.fixture(scope="session", autouse=True)
def _ensure_chrome_prereqs(ensure_chromium_and_puppeteer_installed):
    """Make the shared chromium install fixture autouse for this module."""
    return ensure_chromium_and_puppeteer_installed


def test_hook_scripts_exist():
    """Verify chrome hooks exist."""
    assert CHROME_LAUNCH_HOOK.exists(), f"Hook not found: {CHROME_LAUNCH_HOOK}"
    assert CHROME_TAB_HOOK.exists(), f"Hook not found: {CHROME_TAB_HOOK}"
    assert CHROME_NAVIGATE_HOOK.exists(), f"Hook not found: {CHROME_NAVIGATE_HOOK}"


def test_verify_chromium_available():
    """Verify Chromium is available via CHROME_BINARY env var."""
    chromium_binary = os.environ.get("CHROME_BINARY") or find_chromium_binary()

    assert chromium_binary, (
        "Chromium binary should be available (set by fixture or found)"
    )
    assert Path(chromium_binary).exists(), (
        f"Chromium binary should exist at {chromium_binary}"
    )

    # Verify it's actually Chromium by checking version
    result = subprocess.run(
        [chromium_binary, "--version"], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, f"Failed to get Chromium version: {result.stderr}"
    assert "Chromium" in result.stdout or "Chrome" in result.stdout, (
        f"Unexpected version output: {result.stdout}"
    )


def test_chrome_launch_and_tab_creation(chrome_test_url):
    """Integration test: Launch Chrome at crawl level and create tab at snapshot level."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        # Get test environment with NODE_MODULES_DIR set
        env = get_test_env()
        env["CHROME_HEADLESS"] = "true"
        # chrome_launch writes to <CRAWL_DIR>/chrome, not cwd.
        env["CRAWL_DIR"] = str(crawl_dir)

        # Launch Chrome at crawl level (background process)
        chrome_launch_process = subprocess.Popen(
            ["node", str(CHROME_LAUNCH_HOOK), "--crawl-id=test-crawl-123"],
            cwd=str(chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        # Wait for Chrome to launch (check process isn't dead and files exist).
        # launchChromium() itself waits up to 30s for CDP readiness, so allow
        # additional headroom here to avoid CI false negatives on cold runners.
        launch_wait_seconds = 45
        for i in range(launch_wait_seconds):
            if chrome_launch_process.poll() is not None:
                stdout, stderr = chrome_launch_process.communicate()
                pytest.fail(
                    f"Chrome launch process exited early:\nStdout: {stdout}\nStderr: {stderr}"
                )
            if (chrome_dir / "cdp_url.txt").exists():
                break
            time.sleep(1)

        # Verify Chrome launch outputs - if it failed, get the error from the process
        if not (chrome_dir / "cdp_url.txt").exists():
            # Try to get output from the process
            try:
                stdout, stderr = chrome_launch_process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                # Process still running, try to read available output
                stdout = stderr = "(process still running)"

            # Check what files exist
            if chrome_dir.exists():
                files = list(chrome_dir.iterdir())
                # Check if Chrome process is still alive
                if (chrome_dir / "chrome.pid").exists():
                    chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())
                    try:
                        os.kill(chrome_pid, 0)
                        chrome_alive = "yes"
                    except OSError:
                        chrome_alive = "no"
                    pytest.fail(
                        f"cdp_url.txt missing after {launch_wait_seconds}s. Chrome dir files: {files}. Chrome process {chrome_pid} alive: {chrome_alive}\nLaunch stdout: {stdout}\nLaunch stderr: {stderr}"
                    )
                else:
                    pytest.fail(
                        f"cdp_url.txt missing. Chrome dir exists with files: {files}\nLaunch stdout: {stdout}\nLaunch stderr: {stderr}"
                    )
            else:
                pytest.fail(
                    f"Chrome dir {chrome_dir} doesn't exist\nLaunch stdout: {stdout}\nLaunch stderr: {stderr}"
                )

        assert (chrome_dir / "cdp_url.txt").exists(), "cdp_url.txt should exist"
        assert (chrome_dir / "chrome.pid").exists(), "chrome.pid should exist"
        assert (chrome_dir / "port.txt").exists(), "port.txt should exist"

        cdp_url = (chrome_dir / "cdp_url.txt").read_text().strip()
        chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())

        assert cdp_url.startswith("ws://"), (
            f"CDP URL should be WebSocket URL: {cdp_url}"
        )
        assert chrome_pid > 0, "Chrome PID should be valid"

        # Verify Chrome process is running
        try:
            os.kill(chrome_pid, 0)
        except OSError:
            pytest.fail(f"Chrome process {chrome_pid} is not running")

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
        try:
            tab_process.send_signal(signal.SIGTERM)
            tab_process.wait(timeout=10)
        except Exception:
            pass
        try:
            chrome_launch_process.send_signal(signal.SIGTERM)
            chrome_launch_process.wait(timeout=5)
        except Exception:
            pass
        try:
            os.kill(chrome_pid, signal.SIGKILL)
        except OSError:
            pass


def test_cookies_imported_on_launch():
    """Integration test: COOKIES_TXT_FILE is imported at crawl start."""
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
                ]
            )
        )

        profile_dir = Path(tmpdir) / "profile"
        env = get_test_env()
        env.update(
            {
                "CHROME_HEADLESS": "true",
                "CHROME_USER_DATA_DIR": str(profile_dir),
                "COOKIES_TXT_FILE": str(cookies_file),
                "CRAWL_DIR": str(crawl_dir),
            }
        )

        chrome_launch_process = subprocess.Popen(
            ["node", str(CHROME_LAUNCH_HOOK), "--crawl-id=test-crawl-cookies"],
            cwd=str(chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        for _ in range(15):
            if (chrome_dir / "port.txt").exists():
                break
            time.sleep(1)

        assert (chrome_dir / "port.txt").exists(), "port.txt should exist"
        chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())
        port = int((chrome_dir / "port.txt").read_text().strip())

        cookie_found = False
        for _ in range(15):
            cookies = _get_cookies_via_cdp(port, env)
            cookie_found = any(
                c.get("name") == "abx_test_cookie" and c.get("value") == "hello"
                for c in cookies
            )
            if cookie_found:
                break
            time.sleep(1)

        assert cookie_found, "Imported cookie should be present in Chrome session"

        # Cleanup
        try:
            chrome_launch_process.send_signal(signal.SIGTERM)
            chrome_launch_process.wait(timeout=5)
        except Exception:
            pass
        try:
            os.kill(chrome_pid, signal.SIGKILL)
        except OSError:
            pass


def test_chrome_navigation(chrome_test_url):
    """Integration test: Navigate to a URL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        launch_env = get_test_env() | {
            "CRAWL_DIR": str(crawl_dir),
            "CHROME_HEADLESS": "true",
        }
        # Launch Chrome (background process)
        chrome_launch_process = subprocess.Popen(
            ["node", str(CHROME_LAUNCH_HOOK), "--crawl-id=test-crawl-nav"],
            cwd=str(chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=launch_env,
        )

        # Wait for Chrome to launch
        time.sleep(3)

        chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())

        # Create snapshot and tab
        snapshot_dir = Path(tmpdir) / "snapshot1"
        snapshot_dir.mkdir()
        snapshot_chrome_dir = snapshot_dir / "chrome"
        snapshot_chrome_dir.mkdir()

        tab_env = get_test_env() | {
            "CRAWL_DIR": str(crawl_dir),
            "SNAP_DIR": str(snapshot_dir),
            "CHROME_HEADLESS": "true",
        }
        tab_process = launch_snapshot_tab(
            snapshot_chrome_dir=snapshot_chrome_dir,
            tab_env=tab_env,
            test_url=chrome_test_url,
            snapshot_id="snap-nav-123",
            crawl_id="test-crawl-nav",
        )

        # Navigate to URL
        nav_env = get_test_env() | {
            "SNAP_DIR": str(snapshot_dir),
            "CHROME_PAGELOAD_TIMEOUT": "30",
            "CHROME_WAIT_FOR": "load",
        }
        result = subprocess.run(
            [
                "node",
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
        assert (snapshot_chrome_dir / "page_loaded.txt").exists(), (
            "page_loaded.txt should exist"
        )

        nav_data = json.loads((snapshot_chrome_dir / "navigation.json").read_text())
        assert nav_data.get("status") in [200, 301, 302], (
            f"Should get valid HTTP status: {nav_data}"
        )
        assert nav_data.get("finalUrl"), "Should have final URL"

        # Cleanup
        try:
            tab_process.send_signal(signal.SIGTERM)
            tab_process.wait(timeout=10)
        except Exception:
            pass
        try:
            chrome_launch_process.send_signal(signal.SIGTERM)
            chrome_launch_process.wait(timeout=5)
        except Exception:
            pass
        try:
            os.kill(chrome_pid, signal.SIGKILL)
        except OSError:
            pass


def test_shared_dir_crawl_snapshot_file_order_and_gating(chrome_test_url):
    """Shared SNAP_DIR/CRAWL_DIR should preserve crawl-vs-snapshot file boundaries in order."""
    with tempfile.TemporaryDirectory() as tmpdir:
        shared_dir = Path(tmpdir) / "shared"
        shared_dir.mkdir()
        chrome_dir = shared_dir / "chrome"
        chrome_dir.mkdir()

        env = get_test_env() | {
            "CRAWL_DIR": str(shared_dir),
            "SNAP_DIR": str(shared_dir),
            "CHROME_HEADLESS": "true",
        }

        shared_files = {
            "cdp_url": chrome_dir / "cdp_url.txt",
            "chrome_pid": chrome_dir / "chrome.pid",
            "port": chrome_dir / "port.txt",
        }
        extensions_file = chrome_dir / "extensions.json"
        snapshot_files = {
            "target": chrome_dir / "target_id.txt",
            "url": chrome_dir / "url.txt",
            "navigation": chrome_dir / "navigation.json",
            "page_loaded": chrome_dir / "page_loaded.txt",
            "final_url": chrome_dir / "final_url.txt",
        }
        chrome_launch_process = None
        tab_process = None
        try:
            chrome_launch_process = subprocess.Popen(
                ["node", str(CHROME_LAUNCH_HOOK), "--crawl-id=test-shared-order"],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            for _ in range(30):
                if chrome_launch_process.poll() is not None:
                    stdout, stderr = chrome_launch_process.communicate()
                    pytest.fail(
                        f"Chrome launch exited early:\nStdout: {stdout}\nStderr: {stderr}"
                    )
                if all(path.exists() for path in shared_files.values()):
                    break
                time.sleep(1)

            assert all(path.exists() for path in shared_files.values()), (
                f"Crawl-scoped files should exist after launch: {shared_files}"
            )
            assert not any(path.exists() for path in snapshot_files.values()), (
                "Launch hook should not create snapshot-scoped files in shared chrome dir"
            )

            cdp_url_before = shared_files["cdp_url"].read_text().strip()
            chrome_pid_before = shared_files["chrome_pid"].read_text().strip()
            port_before = shared_files["port"].read_text().strip()
            extensions_before = (
                extensions_file.read_text() if extensions_file.exists() else None
            )
            assert cdp_url_before.startswith("ws://127.0.0.1:"), cdp_url_before
            assert port_before == str(_port_from_cdp_url(cdp_url_before))
            os.kill(int(chrome_pid_before), 0)
            assert _fetch_devtools_targets(cdp_url_before), (
                "crawl launch should expose a live DevTools target list"
            )

            crawl_wait = subprocess.run(
                [
                    "node",
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
            assert f"port={port_before}" in crawl_wait.stdout
            assert not any(path.exists() for path in snapshot_files.values()), (
                "crawl wait should not create snapshot-scoped files"
            )

            snapshot_wait_before_tab = subprocess.run(
                [
                    "node",
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

            tab_process = launch_snapshot_tab(
                snapshot_chrome_dir=chrome_dir,
                tab_env=env,
                test_url=chrome_test_url,
                snapshot_id="snap-shared-order",
                crawl_id="test-shared-order",
            )

            target_id_before_wait = snapshot_files["target"].read_text().strip()
            url_before_wait = snapshot_files["url"].read_text().strip()
            assert url_before_wait == chrome_test_url
            if extensions_before is None:
                assert not extensions_file.exists(), (
                    "chrome_tab should not synthesize extensions.json when crawl launch did not create it"
                )
            else:
                assert extensions_file.read_text() == extensions_before
            assert not snapshot_files["navigation"].exists(), (
                "chrome_tab should not create navigation.json before navigate"
            )
            assert not snapshot_files["page_loaded"].exists(), (
                "chrome_tab should not create page_loaded.txt before navigate"
            )
            assert not snapshot_files["final_url"].exists(), (
                "chrome_tab should not create final_url.txt before navigate"
            )
            assert shared_files["cdp_url"].read_text().strip() == cdp_url_before
            assert shared_files["chrome_pid"].read_text().strip() == chrome_pid_before
            assert shared_files["port"].read_text().strip() == port_before

            tab_targets = _fetch_devtools_targets(cdp_url_before)
            tab_target = next(
                (target for target in tab_targets if target.get("id") == target_id_before_wait),
                None,
            )
            assert tab_target is not None, "chrome_tab should create a live page target"
            assert tab_target.get("type") == "page"
            assert tab_target.get("url") == "about:blank"

            snapshot_wait = subprocess.run(
                [
                    "node",
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
                    "node",
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
            final_url = snapshot_files["final_url"].read_text().strip()
            assert snapshot_files["page_loaded"].read_text().strip()
            assert nav_data["url"] == chrome_test_url
            assert nav_data["finalUrl"] == final_url
            assert nav_data["status"] == 200
            assert final_url.rstrip("/") == chrome_test_url.rstrip("/")
            assert shared_files["cdp_url"].read_text().strip() == cdp_url_before
            assert shared_files["chrome_pid"].read_text().strip() == chrome_pid_before
            assert shared_files["port"].read_text().strip() == port_before
            if extensions_before is not None:
                assert extensions_file.read_text() == extensions_before
            assert snapshot_files["target"].read_text().strip() == target_id_before_wait

            navigated_targets = _fetch_devtools_targets(cdp_url_before)
            navigated_target = next(
                (target for target in navigated_targets if target.get("id") == target_id_before_wait),
                None,
            )
            assert navigated_target is not None, "navigation should keep the same target alive"
            assert navigated_target.get("url", "").rstrip("/") == chrome_test_url.rstrip("/")
        finally:
            if tab_process is not None:
                try:
                    tab_process.send_signal(signal.SIGTERM)
                    tab_process.wait(timeout=10)
                except Exception:
                    pass
            assert not snapshot_files["target"].exists(), (
                "target_id.txt should be removed after snapshot tab teardown"
            )
            if chrome_launch_process is not None:
                try:
                    chrome_launch_process.send_signal(signal.SIGTERM)
                    chrome_launch_process.wait(timeout=10)
                except Exception:
                    pass
            assert not shared_files["chrome_pid"].exists(), (
                "chrome.pid should be removed after crawl teardown"
            )
            assert not shared_files["port"].exists(), (
                "port.txt should be removed after crawl teardown"
            )


def test_shared_dir_extensions_metadata_created_and_preserved_when_enabled(
    chrome_test_url,
):
    """Shared crawl/snapshot setup should create correct extensions.json when extensions are enabled."""
    with tempfile.TemporaryDirectory() as tmpdir:
        shared_dir = Path(tmpdir) / "shared"
        shared_dir.mkdir()
        chrome_dir = shared_dir / "chrome"
        chrome_dir.mkdir()
        extensions_dir = Path(tmpdir) / "chrome_extensions"
        extensions_dir.mkdir()

        install_env = get_test_env() | {"CHROME_EXTENSIONS_DIR": str(extensions_dir)}
        install = subprocess.run(
            ["node", str(UBLOCK_INSTALL_HOOK)],
            capture_output=True,
            text=True,
            timeout=120,
            env=install_env,
        )
        assert install.returncode == 0, (
            f"ublock install should succeed:\nStdout: {install.stdout}\nStderr: {install.stderr}"
        )
        ublock_cache = extensions_dir / "ublock.extension.json"
        assert ublock_cache.exists(), "ublock install should create extension cache"
        cached_ext = json.loads(ublock_cache.read_text())

        env = install_env | {
            "CRAWL_DIR": str(shared_dir),
            "SNAP_DIR": str(shared_dir),
            "CHROME_HEADLESS": "true",
        }
        extensions_file = chrome_dir / "extensions.json"
        chrome_launch_process = None
        tab_process = None
        try:
            chrome_launch_process = subprocess.Popen(
                ["node", str(CHROME_LAUNCH_HOOK), "--crawl-id=test-shared-exts"],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for _ in range(30):
                if chrome_launch_process.poll() is not None:
                    stdout, stderr = chrome_launch_process.communicate()
                    pytest.fail(
                        f"Chrome launch exited early:\nStdout: {stdout}\nStderr: {stderr}"
                    )
                if extensions_file.exists() and (chrome_dir / "cdp_url.txt").exists():
                    break
                time.sleep(1)

            assert extensions_file.exists(), (
                "chrome launch should create extensions.json when extensions are enabled"
            )
            crawl_extensions_text = extensions_file.read_text()
            crawl_extensions = json.loads(crawl_extensions_text)
            ublock_entry = next(
                (entry for entry in crawl_extensions if entry.get("name") == "ublock"),
                None,
            )
            assert ublock_entry is not None, crawl_extensions
            assert ublock_entry.get("webstore_id") == cached_ext["webstore_id"]
            assert ublock_entry.get("unpacked_path") == cached_ext["unpacked_path"]
            assert ublock_entry.get("id"), ublock_entry
            assert wait_for_extensions_metadata(chrome_dir, timeout_seconds=10) == crawl_extensions

            tab_process = launch_snapshot_tab(
                snapshot_chrome_dir=chrome_dir,
                tab_env=env,
                test_url=chrome_test_url,
                snapshot_id="snap-shared-exts",
                crawl_id="test-shared-exts",
            )
            assert json.loads(extensions_file.read_text()) == crawl_extensions
            assert extensions_file.read_text() == crawl_extensions_text

            snapshot_wait = subprocess.run(
                [
                    "node",
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
            assert json.loads(extensions_file.read_text()) == crawl_extensions
            assert extensions_file.read_text() == crawl_extensions_text
        finally:
            if tab_process is not None:
                try:
                    tab_process.send_signal(signal.SIGTERM)
                    tab_process.wait(timeout=10)
                except Exception:
                    pass
            if chrome_launch_process is not None:
                try:
                    chrome_launch_process.send_signal(signal.SIGTERM)
                    chrome_launch_process.wait(timeout=10)
                except Exception:
                    pass


def test_chrome_wait_rejects_stale_cdp_markers(chrome_test_url):
    """chrome_wait should not treat stale marker files as a live CDP session."""
    with tempfile.TemporaryDirectory() as tmpdir:
        snapshot_dir = Path(tmpdir) / "snapshot1"
        snapshot_dir.mkdir()
        snapshot_chrome_dir = snapshot_dir / "chrome"
        snapshot_chrome_dir.mkdir()

        snapshot_chrome_dir.joinpath("cdp_url.txt").write_text(
            "ws://127.0.0.1:9/devtools/browser/stale-session"
        )
        snapshot_chrome_dir.joinpath("target_id.txt").write_text("stale-target-id")

        wait_env = get_test_env() | {
            "SNAP_DIR": str(snapshot_dir),
            "CHROME_TAB_TIMEOUT": "1",
            "CHROME_TIMEOUT": "1",
        }
        result = subprocess.run(
            [
                "node",
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
        payload = json.loads(result.stdout.strip())
        assert payload["status"] == "failed"
        assert payload["output_str"] == "No Chrome session found (chrome plugin must run first)"


def test_cleanup_stale_chrome_session_artifacts_only_when_stale():
    """Stale chrome markers should be removed, but only when they are actually stale."""
    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir) / "chrome"
        session_dir.mkdir()
        session_dir.joinpath("cdp_url.txt").write_text(
            "ws://127.0.0.1:9/devtools/browser/stale-session"
        )
        session_dir.joinpath("target_id.txt").write_text("stale-target-id")
        session_dir.joinpath("chrome.pid").write_text("999999")
        session_dir.joinpath("port.txt").write_text("9")

        result = _cleanup_session_artifacts(
            session_dir, get_test_env(), require_target_id=True
        )

        assert result["hasArtifacts"] is True
        assert result["stale"] is True
        assert "cdp_url.txt" in result["cleanedFiles"]
        assert "target_id.txt" in result["cleanedFiles"]
        assert not (session_dir / "cdp_url.txt").exists()
        assert not (session_dir / "target_id.txt").exists()


def test_cleanup_stale_chrome_session_artifacts_keeps_live_session():
    """Healthy Chrome sessions should not have their marker files removed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        env = get_test_env() | {
            "CRAWL_DIR": str(crawl_dir),
            "CHROME_HEADLESS": "true",
        }
        chrome_launch_process = subprocess.Popen(
            ["node", str(CHROME_LAUNCH_HOOK), "--crawl-id=test-stale-cleanup"],
            cwd=str(chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        try:
            for _ in range(30):
                if (chrome_dir / "cdp_url.txt").exists() and (chrome_dir / "chrome.pid").exists():
                    break
                if chrome_launch_process.poll() is not None:
                    stdout, stderr = chrome_launch_process.communicate()
                    pytest.fail(
                        f"Chrome launch exited early:\nStdout: {stdout}\nStderr: {stderr}"
                    )
                time.sleep(1)

            result = _cleanup_session_artifacts(chrome_dir, env)

            assert result["hasArtifacts"] is True
            assert result["stale"] is False
            assert result["cleanedFiles"] == []
            assert (chrome_dir / "cdp_url.txt").exists()
            assert (chrome_dir / "chrome.pid").exists()
        finally:
            if chrome_launch_process.poll() is None:
                try:
                    chrome_launch_process.send_signal(signal.SIGTERM)
                    chrome_launch_process.wait(timeout=5)
                except Exception:
                    pass
            if (chrome_dir / "chrome.pid").exists():
                try:
                    os.kill(int((chrome_dir / "chrome.pid").read_text().strip()), signal.SIGKILL)
                except OSError:
                    pass


def test_tab_cleanup_on_sigterm(chrome_test_url):
    """Integration test: Tab cleanup when receiving SIGTERM."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        launch_env = get_test_env() | {
            "CRAWL_DIR": str(crawl_dir),
            "CHROME_HEADLESS": "true",
        }
        # Launch Chrome (background process)
        chrome_launch_process = subprocess.Popen(
            ["node", str(CHROME_LAUNCH_HOOK), "--crawl-id=test-cleanup"],
            cwd=str(chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=launch_env,
        )

        # Wait for Chrome to launch
        time.sleep(3)

        chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())

        # Create snapshot and tab - run in background
        snapshot_dir = Path(tmpdir) / "snapshot1"
        snapshot_dir.mkdir()
        snapshot_chrome_dir = snapshot_dir / "chrome"
        snapshot_chrome_dir.mkdir()

        tab_env = get_test_env() | {
            "CRAWL_DIR": str(crawl_dir),
            "SNAP_DIR": str(snapshot_dir),
            "CHROME_HEADLESS": "true",
        }
        tab_process = subprocess.Popen(
            [
                "node",
                str(CHROME_TAB_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=snap-cleanup",
                "--crawl-id=test-cleanup",
            ],
            cwd=str(snapshot_chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=tab_env,
        )

        # Wait for tab to be created
        time.sleep(3)

        # Send SIGTERM to tab process
        tab_process.send_signal(signal.SIGTERM)
        stdout, stderr = tab_process.communicate(timeout=10)

        assert tab_process.returncode == 0, f"Tab process should exit cleanly: {stderr}"
        assert not (snapshot_chrome_dir / "target_id.txt").exists(), (
            "target_id.txt should be removed when the snapshot tab is cleaned up"
        )

        # Chrome should still be running
        try:
            os.kill(chrome_pid, 0)
        except OSError:
            pytest.fail("Chrome should still be running after tab cleanup")

        # Cleanup
        try:
            chrome_launch_process.send_signal(signal.SIGTERM)
            chrome_launch_process.wait(timeout=5)
        except Exception:
            pass
        try:
            os.kill(chrome_pid, signal.SIGKILL)
        except OSError:
            pass


def test_snapshot_wait_survives_idle_delay_with_shared_dirs(chrome_test_url):
    """Snapshot tab should remain connectable even when SNAP_DIR and CRAWL_DIR are shared."""
    with tempfile.TemporaryDirectory() as tmpdir:
        shared_dir = Path(tmpdir) / "shared"
        shared_dir.mkdir()
        chrome_dir = shared_dir / "chrome"
        chrome_dir.mkdir()

        env = get_test_env() | {
            "CRAWL_DIR": str(shared_dir),
            "SNAP_DIR": str(shared_dir),
            "CHROME_HEADLESS": "true",
        }

        chrome_launch_process = subprocess.Popen(
            ["node", str(CHROME_LAUNCH_HOOK), "--crawl-id=test-shared-dirs"],
            cwd=str(chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        for _ in range(30):
            if (chrome_dir / "cdp_url.txt").exists() and (chrome_dir / "chrome.pid").exists():
                break
            if chrome_launch_process.poll() is not None:
                stdout, stderr = chrome_launch_process.communicate()
                pytest.fail(
                    f"Chrome launch exited early:\nStdout: {stdout}\nStderr: {stderr}"
                )
            time.sleep(1)

        chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())
        tab_process = launch_snapshot_tab(
            snapshot_chrome_dir=chrome_dir,
            tab_env=env,
            test_url=chrome_test_url,
            snapshot_id="snap-shared",
            crawl_id="test-shared-dirs",
        )

        time.sleep(8)

        result = subprocess.run(
            [
                "node",
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

        try:
            tab_process.send_signal(signal.SIGTERM)
            tab_process.wait(timeout=10)
        except Exception:
            pass
        try:
            chrome_launch_process.send_signal(signal.SIGTERM)
            chrome_launch_process.wait(timeout=5)
        except Exception:
            pass
        try:
            os.kill(chrome_pid, signal.SIGKILL)
        except OSError:
            pass


def test_concurrent_same_dir_reuses_one_browser_and_one_target(chrome_test_url):
    """Concurrent same-dir launch/tab calls should converge on one live browser and one canonical target."""
    with tempfile.TemporaryDirectory() as tmpdir:
        shared_dir = Path(tmpdir) / "shared"
        shared_dir.mkdir()
        chrome_dir = shared_dir / "chrome"
        chrome_dir.mkdir()
        env = get_test_env() | {
            "CRAWL_DIR": str(shared_dir),
            "SNAP_DIR": str(shared_dir),
            "CHROME_HEADLESS": "true",
        }

        launch_a = launch_b = tab_a = tab_b = None
        try:
            launch_a = subprocess.Popen(
                ["node", str(CHROME_LAUNCH_HOOK), "--crawl-id=test-concurrent"],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            launch_b = subprocess.Popen(
                ["node", str(CHROME_LAUNCH_HOOK), "--crawl-id=test-concurrent"],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            for _ in range(30):
                if (chrome_dir / "cdp_url.txt").exists() and (chrome_dir / "chrome.pid").exists():
                    break
                time.sleep(1)

            cdp_url = (chrome_dir / "cdp_url.txt").read_text().strip()
            chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())
            os.kill(chrome_pid, 0)
            page_targets_before = {
                target["id"]
                for target in _fetch_devtools_targets(cdp_url)
                if target.get("type") == "page" and target.get("id")
            }

            tab_a = subprocess.Popen(
                ["node", str(CHROME_TAB_HOOK), f"--url={chrome_test_url}", "--snapshot-id=snap-concurrent", "--crawl-id=test-concurrent"],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            tab_b = subprocess.Popen(
                ["node", str(CHROME_TAB_HOOK), f"--url={chrome_test_url}", "--snapshot-id=snap-concurrent", "--crawl-id=test-concurrent"],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            for _ in range(30):
                if (chrome_dir / "target_id.txt").exists():
                    target_id = (chrome_dir / "target_id.txt").read_text().strip()
                    page_targets_after = {
                        target["id"]
                        for target in _fetch_devtools_targets(cdp_url)
                        if target.get("type") == "page" and target.get("id")
                    }
                    if target_id in page_targets_after:
                        break
                time.sleep(1)

            wait_result = subprocess.run(
                ["node", str(CHROME_WAIT_HOOK), f"--url={chrome_test_url}", "--snapshot-id=snap-concurrent"],
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
                for target in _fetch_devtools_targets(cdp_url)
                if target.get("type") == "page" and target.get("id")
            }
            assert target_id in page_targets_after
            assert len(page_targets_after - page_targets_before) == 1, (
                f"Concurrent same-dir tab setup should create exactly one new page target: before={page_targets_before} after={page_targets_after}"
            )
            assert launch_a.poll() is None and launch_b.poll() is None
            assert tab_a.poll() is None and tab_b.poll() is None
        finally:
            for proc in (tab_a, tab_b, launch_a, launch_b):
                if proc is None:
                    continue
                try:
                    proc.send_signal(signal.SIGTERM)
                    proc.wait(timeout=10)
                except Exception:
                    pass


def test_target_crash_mid_navigation_recovers_with_fresh_tab(chrome_test_urls):
    """If the canonical target disappears mid-run, navigation should fail clearly and the next tab setup should recreate it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        shared_dir = Path(tmpdir) / "shared"
        shared_dir.mkdir()
        chrome_dir = shared_dir / "chrome"
        chrome_dir.mkdir()
        env = get_test_env() | {
            "CRAWL_DIR": str(shared_dir),
            "SNAP_DIR": str(shared_dir),
            "CHROME_HEADLESS": "true",
            "CHROME_WAIT_FOR": "load",
        }

        chrome_launch_process = None
        tab_process = None
        replacement_tab_process = None
        navigate_process = None
        try:
            chrome_launch_process = subprocess.Popen(
                ["node", str(CHROME_LAUNCH_HOOK), "--crawl-id=test-target-crash"],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for _ in range(30):
                if (chrome_dir / "cdp_url.txt").exists() and (chrome_dir / "chrome.pid").exists():
                    break
                time.sleep(1)

            cdp_url = (chrome_dir / "cdp_url.txt").read_text().strip()
            tab_process = launch_snapshot_tab(
                snapshot_chrome_dir=chrome_dir,
                tab_env=env,
                test_url=chrome_test_urls["slow_url"],
                snapshot_id="snap-target-crash",
                crawl_id="test-target-crash",
            )
            target_before = (chrome_dir / "target_id.txt").read_text().strip()

            navigate_process = subprocess.Popen(
                ["node", str(CHROME_NAVIGATE_HOOK), f"--url={chrome_test_urls['slow_url']}", "--snapshot-id=snap-target-crash"],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env | {"CHROME_PAGELOAD_TIMEOUT": "15"},
            )
            time.sleep(1.0)
            _close_target_via_cdp(cdp_url, target_before)
            for _ in range(30):
                remaining_targets = {
                    target["id"]
                    for target in _fetch_devtools_targets(cdp_url)
                    if target.get("type") == "page" and target.get("id")
                }
                if target_before not in remaining_targets:
                    break
                time.sleep(0.1)
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
            assert not (chrome_dir / "page_loaded.txt").exists()
            assert not (chrome_dir / "final_url.txt").exists()

            replacement_url = chrome_test_urls["base_url"]
            replacement_tab_process = launch_snapshot_tab(
                snapshot_chrome_dir=chrome_dir,
                tab_env=env,
                test_url=replacement_url,
                snapshot_id="snap-target-crash",
                crawl_id="test-target-crash",
            )
            wait_result = subprocess.run(
                ["node", str(CHROME_WAIT_HOOK), f"--url={replacement_url}", "--snapshot-id=snap-target-crash"],
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
            for proc in (replacement_tab_process, tab_process, chrome_launch_process):
                if proc is None:
                    continue
                try:
                    proc.send_signal(signal.SIGTERM)
                    proc.wait(timeout=10)
                except Exception:
                    pass


def test_popup_focus_theft_keeps_followup_hooks_on_canonical_target(chrome_test_urls):
    """Popup windows stealing focus must not move follow-up hooks off the canonical snapshot target."""
    with tempfile.TemporaryDirectory() as tmpdir:
        shared_dir = Path(tmpdir) / "shared"
        shared_dir.mkdir()
        chrome_dir = shared_dir / "chrome"
        chrome_dir.mkdir()
        env = get_test_env() | {
            "CRAWL_DIR": str(shared_dir),
            "SNAP_DIR": str(shared_dir),
            "CHROME_HEADLESS": "true",
            "CHROME_WAIT_FOR": "load",
        }

        chrome_launch_process = None
        tab_process = None
        try:
            chrome_launch_process = subprocess.Popen(
                ["node", str(CHROME_LAUNCH_HOOK), "--crawl-id=test-popup-focus"],
                cwd=str(chrome_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for _ in range(30):
                if (chrome_dir / "cdp_url.txt").exists():
                    break
                time.sleep(1)

            cdp_url = (chrome_dir / "cdp_url.txt").read_text().strip()
            tab_process = launch_snapshot_tab(
                snapshot_chrome_dir=chrome_dir,
                tab_env=env,
                test_url=chrome_test_urls["popup_parent_url"],
                snapshot_id="snap-popup-focus",
                crawl_id="test-popup-focus",
            )
            navigate = subprocess.run(
                ["node", str(CHROME_NAVIGATE_HOOK), f"--url={chrome_test_urls['popup_parent_url']}", "--snapshot-id=snap-popup-focus"],
                cwd=str(chrome_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert navigate.returncode == 0, (
                f"navigate should succeed on popup page:\nStdout: {navigate.stdout}\nStderr: {navigate.stderr}"
            )
            popup_target_info = _create_target_via_cdp(cdp_url, chrome_test_urls["popup_child_url"])
            time.sleep(0.5)

            target_id = (chrome_dir / "target_id.txt").read_text().strip()
            targets = _fetch_devtools_targets(cdp_url)
            canonical_target = next((target for target in targets if target.get("id") == target_id), None)
            popup_target = next(
                (
                    target
                    for target in targets
                    if target.get("id") == popup_target_info.get("id")
                ),
                None,
            )
            assert canonical_target is not None, targets
            assert canonical_target.get("url", "").rstrip("/") == chrome_test_urls["popup_parent_url"].rstrip("/")
            assert popup_target is not None, targets

            seo = subprocess.run(
                ["node", str(SEO_HOOK), f"--url={chrome_test_urls['popup_parent_url']}", "--snapshot-id=snap-popup-focus"],
                cwd=str(shared_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert seo.returncode == 0, (
                f"seo hook should stay on canonical popup parent target:\nStdout: {seo.stdout}\nStderr: {seo.stderr}"
            )
            seo_data = json.loads((shared_dir / "seo" / "seo.json").read_text())
            assert seo_data["title"] == "Popup Parent"
            assert seo_data["url"].rstrip("/") == chrome_test_urls["popup_parent_url"].rstrip("/")
        finally:
            for proc in (tab_process, chrome_launch_process):
                if proc is None:
                    continue
                try:
                    proc.send_signal(signal.SIGTERM)
                    proc.wait(timeout=10)
                except Exception:
                    pass


def test_multiple_snapshots_share_chrome(chrome_test_urls):
    """Integration test: Multiple snapshots share one Chrome instance."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        launch_env = get_test_env() | {
            "CRAWL_DIR": str(crawl_dir),
            "CHROME_HEADLESS": "true",
        }
        # Launch Chrome at crawl level
        chrome_launch_process = subprocess.Popen(
            ["node", str(CHROME_LAUNCH_HOOK), "--crawl-id=test-multi-crawl"],
            cwd=str(chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=launch_env,
        )

        # Wait for Chrome to launch
        for i in range(15):
            if (chrome_dir / "cdp_url.txt").exists():
                break
            time.sleep(1)

        chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())
        crawl_cdp_url = (chrome_dir / "cdp_url.txt").read_text().strip()

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
            tab_env = get_test_env() | {
                "CRAWL_DIR": str(crawl_dir),
                "SNAP_DIR": str(snapshot_dir),
                "CHROME_HEADLESS": "true",
            }
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
            snapshot_cdp_url = (snapshot_chrome_dir / "cdp_url.txt").read_text().strip()
            snapshot_pid = int((snapshot_chrome_dir / "chrome.pid").read_text().strip())

            target_ids.append(target_id)

            # All snapshots should share same Chrome
            assert snapshot_pid == chrome_pid, (
                f"Snapshot {snap_num} should use crawl Chrome PID"
            )
            assert snapshot_cdp_url == crawl_cdp_url, (
                f"Snapshot {snap_num} should use crawl CDP URL"
            )
            try:
                tab_process.send_signal(signal.SIGTERM)
                tab_process.wait(timeout=10)
            except Exception:
                pass

        # All target IDs should be unique (different tabs)
        assert len(set(target_ids)) == 3, (
            f"All snapshots should have unique tabs: {target_ids}"
        )

        # Chrome should still be running with all 3 tabs
        try:
            os.kill(chrome_pid, 0)
        except OSError:
            pytest.fail("Chrome should still be running after creating 3 tabs")

        # Cleanup
        try:
            chrome_launch_process.send_signal(signal.SIGTERM)
            chrome_launch_process.wait(timeout=5)
        except Exception:
            pass
        try:
            os.kill(chrome_pid, signal.SIGKILL)
        except OSError:
            pass


def test_chrome_cleanup_on_crawl_end():
    """Integration test: Chrome cleanup at end of crawl."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        launch_env = get_test_env() | {
            "CRAWL_DIR": str(crawl_dir),
            "CHROME_HEADLESS": "true",
        }
        # Launch Chrome in background
        chrome_launch_process = subprocess.Popen(
            ["node", str(CHROME_LAUNCH_HOOK), "--crawl-id=test-crawl-end"],
            cwd=str(chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=launch_env,
        )

        # Wait for Chrome launch state files and fail fast on early hook exit.
        for _ in range(15):
            if chrome_launch_process.poll() is not None:
                stdout, stderr = chrome_launch_process.communicate()
                pytest.fail(
                    f"Chrome launch process exited early:\nStdout: {stdout}\nStderr: {stderr}"
                )
            if (chrome_dir / "cdp_url.txt").exists() and (
                chrome_dir / "chrome.pid"
            ).exists():
                break
            time.sleep(1)

        # Verify Chrome is running
        assert (chrome_dir / "chrome.pid").exists(), "Chrome PID file should exist"
        chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())

        try:
            os.kill(chrome_pid, 0)
        except OSError:
            pytest.fail("Chrome should be running")

        # Send SIGTERM to chrome launch process
        chrome_launch_process.send_signal(signal.SIGTERM)
        stdout, stderr = chrome_launch_process.communicate(timeout=10)

        # Wait for cleanup
        time.sleep(3)

        # Verify Chrome process is killed
        try:
            os.kill(chrome_pid, 0)
            pytest.fail("Chrome should be killed after SIGTERM")
        except OSError:
            # Expected - Chrome should be dead
            pass

        assert not (chrome_dir / "chrome.pid").exists(), (
            "chrome.pid should be removed during Chrome cleanup"
        )
        assert not (chrome_dir / "port.txt").exists(), (
            "port.txt should be removed during Chrome cleanup"
        )


def test_zombie_prevention_hook_killed():
    """Integration test: Chrome is killed even if hook process is SIGKILL'd."""
    with tempfile.TemporaryDirectory() as tmpdir:
        crawl_dir = Path(tmpdir) / "crawl"
        crawl_dir.mkdir()
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir()

        launch_env = get_test_env() | {
            "CRAWL_DIR": str(crawl_dir),
            "CHROME_HEADLESS": "true",
        }
        # Launch Chrome
        chrome_launch_process = subprocess.Popen(
            ["node", str(CHROME_LAUNCH_HOOK), "--crawl-id=test-zombie"],
            cwd=str(chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=launch_env,
        )

        # Wait for Chrome to launch
        for i in range(15):
            if (chrome_dir / "chrome.pid").exists():
                break
            time.sleep(1)

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
            pytest.fail("Both Chrome and hook should be running")

        # Simulate hook getting SIGKILL'd (can't cleanup)
        os.kill(hook_pid, signal.SIGKILL)
        time.sleep(1)

        # Chrome should still be running (orphaned)
        try:
            os.kill(chrome_pid, 0)
        except OSError:
            pytest.fail("Chrome should still be running after hook SIGKILL")

        # Simulate Crawl.cleanup() using the actual cleanup logic
        def is_process_alive(pid):
            """Check if a process exists."""
            try:
                os.kill(pid, 0)
                return True
            except (OSError, ProcessLookupError):
                return False

        for pid_file in chrome_dir.glob("**/*.pid"):
            try:
                pid = int(pid_file.read_text().strip())

                # Step 1: SIGTERM for graceful shutdown
                try:
                    try:
                        os.killpg(pid, signal.SIGTERM)
                    except (OSError, ProcessLookupError):
                        os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pid_file.unlink(missing_ok=True)
                    continue

                # Step 2: Wait for graceful shutdown
                time.sleep(2)

                # Step 3: Check if still alive
                if not is_process_alive(pid):
                    pid_file.unlink(missing_ok=True)
                    continue

                # Step 4: Force kill ENTIRE process group with SIGKILL
                try:
                    try:
                        # Always kill entire process group with SIGKILL
                        os.killpg(pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pid_file.unlink(missing_ok=True)
                    continue

                # Step 5: Wait and verify death
                time.sleep(1)

                if not is_process_alive(pid):
                    pid_file.unlink(missing_ok=True)

            except (ValueError, OSError):
                pass

        # Chrome should now be dead
        try:
            os.kill(chrome_pid, 0)
            pytest.fail("Chrome should be killed after cleanup")
        except OSError:
            # Expected - Chrome is dead
            pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
