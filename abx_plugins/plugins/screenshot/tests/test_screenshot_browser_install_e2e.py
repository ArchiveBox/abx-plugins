"""Live end-to-end tests for browser selection via CHROME_BINARY."""

import json
import os
import platform
import signal
import subprocess
import time
from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import parse_jsonl_output, parse_jsonl_records


SCREENSHOT_PLUGIN_DIR = Path(__file__).resolve().parent.parent
CHROME_PLUGIN_DIR = SCREENSHOT_PLUGIN_DIR.parent / "chrome"
PUPPETEER_PLUGIN_DIR = SCREENSHOT_PLUGIN_DIR.parent / "puppeteer"
NPM_PLUGIN_DIR = SCREENSHOT_PLUGIN_DIR.parent / "npm"

SCREENSHOT_HOOK = next(SCREENSHOT_PLUGIN_DIR.glob("on_Snapshot__*_screenshot.*"))
CHROME_INSTALL_HOOK = CHROME_PLUGIN_DIR / "on_Crawl__70_chrome_install.finite.bg.py"
CHROME_LAUNCH_HOOK = CHROME_PLUGIN_DIR / "on_Crawl__90_chrome_launch.daemon.bg.js"
CHROME_TAB_HOOK = CHROME_PLUGIN_DIR / "on_Snapshot__10_chrome_tab.daemon.bg.js"
CHROME_NAVIGATE_HOOK = CHROME_PLUGIN_DIR / "on_Snapshot__30_chrome_navigate.js"
PUPPETEER_CRAWL_HOOK = PUPPETEER_PLUGIN_DIR / "on_Crawl__60_puppeteer_install.py"
PUPPETEER_BINARY_HOOK = PUPPETEER_PLUGIN_DIR / "on_Binary__12_puppeteer_install.py"
NPM_BINARY_HOOK = NPM_PLUGIN_DIR / "on_Binary__10_npm_install.py"


def _machine_type() -> str:
    machine = platform.machine().lower()
    system = platform.system().lower()
    if machine in ("arm64", "aarch64"):
        machine = "arm64"
    elif machine in ("x86_64", "amd64"):
        machine = "x86_64"
    return f"{machine}-{system}"


def _apply_machine_updates(records: list[dict], env: dict[str, str]) -> None:
    for record in records:
        if record.get("type") != "Machine":
            continue
        config = record.get("config")
        if not isinstance(config, dict):
            continue
        env.update({str(key): str(value) for key, value in config.items()})


def _browserless_path(tmp_path: Path, browser_name: str) -> str:
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir(parents=True, exist_ok=True)
    for blocked_name in ("chrome", "chromium"):
        shim_path = tool_dir / blocked_name
        shim_path.write_text("#!/bin/sh\nexit 127\n")
        shim_path.chmod(0o755)

    return os.pathsep.join([str(tool_dir), os.environ.get("PATH", "")])


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


def _wait_for_file(path: Path, process: subprocess.Popen[str], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(
                f"{path.name} was not created.\nstdout:\n{stdout}\nstderr:\n{stderr}",
            )
        time.sleep(0.25)
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        stdout = stderr = "(process still running)"
    pytest.fail(f"Timed out waiting for {path}.\nstdout:\n{stdout}\nstderr:\n{stderr}")


@pytest.mark.parametrize("browser_name", ["chrome", "chromium"])
def test_live_install_and_screenshot_extraction_respects_chrome_binary(
    tmp_path: Path,
    chrome_test_url: str,
    browser_name: str,
):
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
    default_extensions_dir = personas_dir / "Default" / "chrome_extensions"

    chrome_dir.mkdir(parents=True, exist_ok=True)
    snapshot_chrome_dir.mkdir(parents=True, exist_ok=True)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    default_extensions_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(
        {
            "CHROME_BINARY": browser_name,
            "CHROME_HEADLESS": "true",
            "CRAWL_DIR": str(crawl_dir),
            "SNAP_DIR": str(snapshot_dir),
            "PERSONAS_DIR": str(personas_dir),
            "LIB_DIR": str(lib_dir),
            "MACHINE_TYPE": machine_type,
            "PUPPETEER_CACHE_DIR": str(lib_dir / "puppeteer" / "chrome"),
            "PATH": _browserless_path(tmp_path, browser_name),
        },
    )
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        env["CHROME_SANDBOX"] = "false"

    returncode, stdout, stderr = _run_hook(
        PUPPETEER_CRAWL_HOOK,
        env,
        root_dir,
        timeout=60,
    )
    assert returncode == 0, stderr
    puppeteer_record = next(
        (
            record
            for record in parse_jsonl_records(stdout)
            if record.get("type") == "Binary" and record.get("name") == "puppeteer"
        ),
        None,
    )
    assert puppeteer_record, stdout

    npm_result = subprocess.run(
        [
            str(NPM_BINARY_HOOK),
            "--machine-id=test-machine",
            "--binary-id=test-puppeteer",
            "--plugin-name=puppeteer",
            "--hook-name=on_Crawl__60_puppeteer_install",
            "--name=puppeteer",
            f"--binproviders={puppeteer_record.get('binproviders', '*')}",
            "--overrides=" + json.dumps(puppeteer_record.get("overrides") or {}),
        ],
        cwd=str(root_dir),
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    assert npm_result.returncode == 0, (
        f"puppeteer npm install failed\nstdout:\n{npm_result.stdout}\nstderr:\n{npm_result.stderr}"
    )
    _apply_machine_updates(parse_jsonl_records(npm_result.stdout), env)

    returncode, stdout, stderr = _run_hook(
        CHROME_INSTALL_HOOK,
        env,
        chrome_dir,
        timeout=60,
    )
    assert returncode == 0, stderr
    chrome_record = next(
        (
            record
            for record in parse_jsonl_records(stdout)
            if record.get("type") == "Binary"
        ),
        None,
    )
    assert chrome_record, stdout
    assert chrome_record["name"] == browser_name

    browser_result = subprocess.run(
        [
            str(PUPPETEER_BINARY_HOOK),
            "--machine-id=test-machine",
            f"--binary-id=test-{browser_name}",
            "--plugin-name=chrome",
            "--hook-name=on_Crawl__70_chrome_install.finite.bg",
            f"--name={chrome_record['name']}",
            f"--binproviders={chrome_record.get('binproviders', '*')}",
            "--overrides=" + json.dumps(chrome_record.get("overrides") or {}),
        ],
        cwd=str(root_dir),
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    assert browser_result.returncode == 0, (
        f"{browser_name} install failed\nstdout:\n{browser_result.stdout}\nstderr:\n{browser_result.stderr}"
    )

    install_records = parse_jsonl_records(browser_result.stdout)
    _apply_machine_updates(install_records, env)
    installed_browser = Path(env["CHROME_BINARY"]).resolve()
    assert installed_browser.exists(), env["CHROME_BINARY"]
    assert str(installed_browser).startswith(str(lib_dir.resolve())), installed_browser
    assert env["PUPPETEER_CACHE_DIR"] == str(lib_dir / "puppeteer" / "chrome")

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
        _wait_for_file(chrome_dir / "cdp_url.txt", chrome_launch_process, timeout=90)

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
        _wait_for_file(snapshot_chrome_dir / "target_id.txt", tab_process, timeout=90)

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
            try:
                tab_process.send_signal(signal.SIGTERM)
                tab_process.communicate(timeout=10)
            except Exception:
                pass
        try:
            chrome_launch_process.send_signal(signal.SIGTERM)
            chrome_launch_process.communicate(timeout=10)
        except Exception:
            pass
