"""Live end-to-end tests for browser selection via CHROME_BINARY."""

import os
import platform
import signal
import subprocess
from pathlib import Path


from abx_plugins.plugins.base.testing import (
    parse_jsonl_output,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    get_extensions_dir,
    install_chromium_with_abxpkg,
    wait_for_chrome_session_state,
)


SCREENSHOT_PLUGIN_DIR = Path(__file__).resolve().parent.parent
CHROME_PLUGIN_DIR = SCREENSHOT_PLUGIN_DIR.parent / "chrome"
SCREENSHOT_HOOK = next(SCREENSHOT_PLUGIN_DIR.glob("on_Snapshot__*_screenshot.*"))
CHROME_LAUNCH_HOOK = CHROME_PLUGIN_DIR / "on_CrawlSetup__90_chrome_launch.daemon.bg.js"
CHROME_TAB_HOOK = CHROME_PLUGIN_DIR / "on_Snapshot__10_chrome_tab.daemon.bg.js"
CHROME_NAVIGATE_HOOK = CHROME_PLUGIN_DIR / "on_Snapshot__30_chrome_navigate.js"


def _machine_type() -> str:
    machine = platform.machine().lower()
    system = platform.system().lower()
    if machine in ("arm64", "aarch64"):
        machine = "arm64"
    elif machine in ("x86_64", "amd64"):
        machine = "x86_64"
    return f"{machine}-{system}"


def _run_hook(
    hook: Path,
    env: dict[str, str],
    cwd: Path,
    *args: str,
    timeout: int = 300,
) -> tuple[int, str, str]:
    result = subprocess.run(
        [str(hook), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def test_live_install_and_screenshot_extraction_respects_chrome_binary(
    tmp_path: Path,
    chrome_test_url: str,
):
    browser_name = "chromium"
    machine_type = _machine_type()
    crawl_id = f"browser-install-{browser_name}"
    snapshot_id = f"browser-install-{browser_name}"
    root_dir = tmp_path / browser_name
    crawl_dir = root_dir / "crawl" / crawl_id
    snapshot_dir = root_dir / "snap" / snapshot_id
    chrome_dir = crawl_dir / "chrome"
    snapshot_chrome_dir = snapshot_dir / "chrome"
    screenshot_dir = snapshot_dir / "screenshot"
    lib_dir = root_dir / "lib" / machine_type
    personas_dir = root_dir / "personas"

    chrome_dir.mkdir(parents=True, exist_ok=True)
    snapshot_chrome_dir.mkdir(parents=True, exist_ok=True)
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(
        {
            "CHROME_BINARY": browser_name,
            "CHROME_HEADLESS": "true",
            "CHROME_KEEPALIVE": "false",
            "CRAWL_DIR": str(crawl_dir),
            "SNAP_DIR": str(snapshot_dir),
            "PERSONAS_DIR": str(personas_dir),
            "ABXPKG_LIB_DIR": str(lib_dir),
            "MACHINE_TYPE": machine_type,
        },
    )
    if os.name == "posix" and os.geteuid() == 0:
        env["CHROME_SANDBOX"] = "false"

    resolved_browser = install_chromium_with_abxpkg(env, timeout=600)
    extensions_dir = Path(get_extensions_dir(env=env))
    extensions_dir.mkdir(parents=True, exist_ok=True)
    env["CHROMEWEBSTORE_EXTENSIONS_DIR"] = str(extensions_dir)
    env["CHROME_KEEPALIVE"] = "false"
    installed_browser = Path(env["CHROME_BINARY"]).resolve()
    assert installed_browser.exists(), env["CHROME_BINARY"]
    assert installed_browser.samefile(resolved_browser)
    chrome_launch_process = subprocess.Popen(
        [str(CHROME_LAUNCH_HOOK), f"--crawl-id={crawl_id}"],
        cwd=str(chrome_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    tab_process = None
    try:
        wait_for_chrome_session_state(
            chrome_dir,
            env=env,
            timeout_seconds=90,
            require_browser_ready=True,
            require_connectable=True,
        )

        tab_process = subprocess.Popen(
            [
                str(CHROME_TAB_HOOK),
                f"--url={chrome_test_url}",
                f"--snapshot-id={snapshot_id}",
                f"--crawl-id={crawl_id}",
            ],
            cwd=str(snapshot_chrome_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        wait_for_chrome_session_state(
            snapshot_chrome_dir,
            env=env,
            timeout_seconds=90,
            require_target_id=True,
            require_connectable=True,
        )

        navigate_result = subprocess.run(
            [
                str(CHROME_NAVIGATE_HOOK),
                f"--url={chrome_test_url}",
                f"--snapshot-id={snapshot_id}",
            ],
            cwd=str(snapshot_chrome_dir),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        assert navigate_result.returncode == 0, (
            f"navigation failed\nstdout:\n{navigate_result.stdout}\nstderr:\n{navigate_result.stderr}"
        )
        assert (snapshot_chrome_dir / "navigation.json").exists()

        screenshot_result = subprocess.run(
            [
                str(SCREENSHOT_HOOK),
                f"--url={chrome_test_url}",
                f"--snapshot-id={snapshot_id}",
            ],
            cwd=str(screenshot_dir),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        assert screenshot_result.returncode == 0, (
            f"screenshot failed\nstdout:\n{screenshot_result.stdout}\nstderr:\n{screenshot_result.stderr}"
        )

        screenshot_record = parse_jsonl_output(screenshot_result.stdout)
        assert screenshot_record and screenshot_record["status"] == "succeeded"
        screenshot_file = screenshot_dir / "screenshot.png"
        assert screenshot_file.exists()
        assert screenshot_file.stat().st_size > 1000
        assert screenshot_file.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        if tab_process is not None:
            tab_process.send_signal(signal.SIGTERM)
            tab_process.communicate(timeout=10)
        chrome_launch_process.send_signal(signal.SIGTERM)
        chrome_launch_process.communicate(timeout=10)
