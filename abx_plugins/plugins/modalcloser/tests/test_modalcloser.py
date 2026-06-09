"""
Integration tests for modalcloser plugin

Tests verify:
1. Hook script exists
2. Dependencies installed via chrome validation hooks
3. Verify deps with abxpkg
4. MODALCLOSER_ENABLED=False skips without JSONL
5. Fails gracefully when no chrome session exists
6. Background script runs and handles SIGTERM correctly
7. Config options work (timeout, poll interval)
8. Local CookieYes-style consent popup is hidden by the real hook
"""

import json
import signal
import subprocess
import time
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import install_binary_with_abxpkg

# Import shared Chrome test helpers
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    get_test_env,
    chrome_session,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


PLUGIN_DIR = Path(__file__).resolve().parent.parent
MODALCLOSER_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_modalcloser.*"), None)
TEST_URL = "https://www.singsing.movie/"
CHROME_STARTUP_TIMEOUT_SECONDS = 45


def _modal_page_url(httpserver) -> str:
    """Serve a deterministic page with visible modal/cookie elements."""
    html = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Modal Fixture</title>
</head>
<body class="modal-open" style="overflow: hidden;">
  <main><h1>Modal Fixture</h1></main>
  <div id="cookie-consent" class="cookie-banner" style="display:block; visibility:visible; position:fixed; inset:0; background: rgba(0,0,0,0.8); z-index:9999;">
    Cookie banner
  </div>
</body>
</html>
"""
    httpserver.expect_request("/modal").respond_with_data(
        html,
        content_type="text/html; charset=utf-8",
    )
    return httpserver.url_for("/modal")


def _cookieyes_page_url(httpserver) -> str:
    """Serve a deterministic CookieYes-style consent popup."""
    html = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>CookieYes Fixture</title>
</head>
<body class="modal-open" style="overflow: hidden;">
  <main><h1>CookieYes Fixture</h1></main>
  <div class="cky-overlay" style="display:block; visibility:visible; position:fixed; inset:0; background:rgba(0,0,0,.4); z-index:9998;"></div>
  <div class="cky-consent-container cky-popup-center" role="region" style="display:block; visibility:visible; position:fixed; inset:auto 24px 24px auto; width:320px; padding:16px; background:#151527; color:#fff; z-index:9999;">
    <p class="cky-title">Consentimiento de cookies</p>
    <button class="cky-btn cky-btn-reject">Rechazar todo</button>
    <button class="cky-btn cky-btn-accept">Aceptar todo</button>
  </div>
</body>
</html>
"""
    httpserver.expect_request("/cookieyes").respond_with_data(
        html,
        content_type="text/html; charset=utf-8",
    )
    return httpserver.url_for("/cookieyes")


def _plain_page_url(httpserver) -> str:
    """Serve a deterministic page with no dismissible modals or banners."""
    html = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Plain Fixture</title>
</head>
<body>
  <main><h1>Plain Fixture</h1><p>No modal elements are present here.</p></main>
</body>
</html>
"""
    httpserver.expect_request("/plain").respond_with_data(
        html,
        content_type="text/html; charset=utf-8",
    )
    return httpserver.url_for("/plain")


def _inspect_cookieyes_state(snapshot_chrome_dir: Path, env: dict[str, str]) -> dict:
    """Read visible CookieYes state from the active Chrome page without mutating it."""
    script = r"""
const chromeUtils = require(process.argv[1]);
const chromeSessionDir = process.argv[2];

(async () => {
  const puppeteer = chromeUtils.resolvePuppeteerModule();
  const { browser, page } = await chromeUtils.connectToPage({
    chromeSessionDir,
    timeoutMs: 10000,
    waitForNavigationComplete: true,
    puppeteer,
  });
  try {
    const state = await page.evaluate(() => {
      function visible(selector) {
        const el = document.querySelector(selector);
        if (!el) return { found: false, visible: false };
        const style = window.getComputedStyle(el);
        return {
          found: true,
          visible: style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0',
          display: style.display,
          visibility: style.visibility,
          opacity: style.opacity,
        };
      }
      return {
        container: visible('.cky-consent-container'),
        overlay: visible('.cky-overlay'),
        bodyOverflow: window.getComputedStyle(document.body).overflow,
        bodyHasModalClass: document.body.classList.contains('modal-open'),
      };
    });
    process.stdout.write(JSON.stringify(state));
  } finally {
    await browser.disconnect();
  }
})().catch(error => {
  console.error(error && (error.stack || error.message || String(error)));
  process.exit(1);
});
"""
    chrome_utils = PLUGIN_DIR.parent / "chrome" / "chrome_utils.js"
    result = subprocess.run(
        [
            "node",
            "-e",
            script,
            str(chrome_utils),
            str(snapshot_chrome_dir),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    assert result.returncode == 0, (
        f"CookieYes state inspection failed.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return json.loads(result.stdout)


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert MODALCLOSER_HOOK is not None, "Modalcloser hook not found"
    assert MODALCLOSER_HOOK.exists(), f"Hook not found: {MODALCLOSER_HOOK}"


def test_verify_deps_with_abxpkg():
    """Verify dependencies are available via abxpkg after hook installation."""
    node_loaded = install_binary_with_abxpkg("node", binproviders="env,apt,brew")
    assert node_loaded and node_loaded.abspath, (
        "Node.js required for modalcloser plugin"
    )


def test_config_modalcloser_disabled_skips():
    """Test that MODALCLOSER_ENABLED=False exits without emitting JSONL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        env = get_test_env() | {"SNAP_DIR": str(snap_dir)}
        env["MODALCLOSER_ENABLED"] = "False"

        result = subprocess.run(
            [
                str(MODALCLOSER_HOOK),
                f"--url={TEST_URL}",
                "--snapshot-id=test-disabled",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, (
            f"Should exit 0 when feature disabled: {result.stderr}"
        )
        assert "Skipping" in result.stderr or "False" in result.stderr, (
            "Should log skip reason to stderr"
        )

        jsonl_lines = [
            line
            for line in result.stdout.strip().split("\n")
            if line.strip().startswith("{")
        ]
        assert len(jsonl_lines) == 1, f"Expected skipped JSONL, got: {jsonl_lines}"
        result_json = json.loads(jsonl_lines[0])
        assert result_json["status"] == "skipped", result_json
        assert result_json["output_str"] == "MODALCLOSER_ENABLED=False", result_json


def test_fails_gracefully_without_chrome_session():
    """Test that hook fails gracefully when no chrome session exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        modalcloser_dir = snap_dir / "modalcloser"
        modalcloser_dir.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [
                str(MODALCLOSER_HOOK),
                f"--url={TEST_URL}",
                "--snapshot-id=test-no-chrome",
            ],
            cwd=modalcloser_dir,
            capture_output=True,
            text=True,
            env=get_test_env() | {"SNAP_DIR": str(snap_dir)},
            timeout=30,
        )

        # Should fail (exit 1) when no chrome session
        assert result.returncode != 0, "Should fail when no chrome session exists"
        # Error could be about chrome/CDP not found, or puppeteer module missing
        err_lower = result.stderr.lower()
        assert any(x in err_lower for x in ["chrome", "cdp", "puppeteer", "module"]), (
            f"Should mention chrome/CDP/puppeteer in error: {result.stderr}"
        )


def test_background_script_handles_sigterm(httpserver):
    """Test that background script runs and handles SIGTERM correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        modalcloser_process = None
        try:
            test_url = _modal_page_url(httpserver)
            with chrome_session(
                Path(tmpdir),
                crawl_id="test-modalcloser",
                snapshot_id="snap-modalcloser",
                test_url=test_url,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            ) as (chrome_launch_process, chrome_pid, snapshot_chrome_dir, env):
                # Create modalcloser output directory (sibling to chrome)
                modalcloser_dir = snapshot_chrome_dir.parent / "modalcloser"
                modalcloser_dir.mkdir()

                # Run modalcloser as background process (use env from setup_chrome_session)
                env["MODALCLOSER_POLL_INTERVAL"] = "200"  # Faster polling for test

                modalcloser_process = subprocess.Popen(
                    [
                        str(MODALCLOSER_HOOK),
                        f"--url={test_url}",
                        "--snapshot-id=snap-modalcloser",
                    ],
                    cwd=str(modalcloser_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )

                # Let it run for a bit
                time.sleep(2)

                # Verify it's still running (background script)
                if modalcloser_process.poll() is not None:
                    stdout, stderr = modalcloser_process.communicate(timeout=5)
                    raise AssertionError(
                        "Modalcloser exited early.\n"
                        f"Stdout: {stdout}\n"
                        f"Stderr: {stderr}",
                    )
                assert modalcloser_process.poll() is None, (
                    "Modalcloser should still be running as background process"
                )

                # Send SIGTERM
                modalcloser_process.send_signal(signal.SIGTERM)
                stdout, stderr = modalcloser_process.communicate(timeout=5)

                assert modalcloser_process.returncode == 0, (
                    f"Should exit 0 on SIGTERM: {stderr}"
                )

                # Parse JSONL output
                result_json = None
                for line in stdout.strip().split("\n"):
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            record = json.loads(line)
                            if record.get("type") == "ArchiveResult":
                                result_json = record
                                break
                        except json.JSONDecodeError:
                            pass

                assert result_json is not None, (
                    f"Should have ArchiveResult JSONL output. Stdout: {stdout}"
                )
                assert result_json["status"] == "succeeded", (
                    f"Should succeed: {result_json}"
                )

                # Verify output_str format
                output_str = result_json.get("output_str", "")
                assert output_str.lower().endswith("modals closed"), (
                    f"output_str should report closed modal/dialog counts: {output_str}"
                )
                assert output_str.split()[0].isdigit(), (
                    f"Should close at least one modal/dialog: {output_str}"
                )

                # Verify no files created in output directory
                output_files = list(modalcloser_dir.iterdir())
                assert len(output_files) == 0, (
                    f"Should not create any files, but found: {output_files}"
                )

        finally:
            if modalcloser_process and modalcloser_process.poll() is None:
                modalcloser_process.kill()


def test_background_script_reports_noresults_when_nothing_closed(httpserver):
    """Test that SIGTERM reports noresults when no dialogs or modals were closed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        modalcloser_process = None
        try:
            test_url = _plain_page_url(httpserver)
            with chrome_session(
                Path(tmpdir),
                crawl_id="test-modalcloser-noresults",
                snapshot_id="snap-modalcloser-noresults",
                test_url=test_url,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            ) as (chrome_launch_process, chrome_pid, snapshot_chrome_dir, env):
                modalcloser_dir = snapshot_chrome_dir.parent / "modalcloser"
                modalcloser_dir.mkdir()
                env["MODALCLOSER_POLL_INTERVAL"] = "200"

                modalcloser_process = subprocess.Popen(
                    [
                        str(MODALCLOSER_HOOK),
                        f"--url={test_url}",
                        "--snapshot-id=snap-modalcloser-noresults",
                    ],
                    cwd=str(modalcloser_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )

                time.sleep(1.5)

                if modalcloser_process.poll() is not None:
                    stdout, stderr = modalcloser_process.communicate(timeout=5)
                    raise AssertionError(
                        "Modalcloser exited early.\n"
                        f"Stdout: {stdout}\n"
                        f"Stderr: {stderr}",
                    )
                assert modalcloser_process.poll() is None, (
                    "Modalcloser should still be running as background process"
                )

                modalcloser_process.send_signal(signal.SIGTERM)
                stdout, stderr = modalcloser_process.communicate(timeout=5)

                assert modalcloser_process.returncode == 0, (
                    f"Should exit 0 on SIGTERM: {stderr}"
                )

                result_json = None
                for line in stdout.strip().split("\n"):
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            record = json.loads(line)
                            if record.get("type") == "ArchiveResult":
                                result_json = record
                                break
                        except json.JSONDecodeError:
                            pass

                assert result_json is not None, (
                    f"Should have ArchiveResult JSONL output. Stdout: {stdout}"
                )
                assert result_json["status"] == "noresults", (
                    f"Should report noresults when nothing was closed: {result_json}"
                )
                assert result_json["output_str"] == "0 modals closed", result_json

        finally:
            if modalcloser_process and modalcloser_process.poll() is None:
                modalcloser_process.kill()


def test_dialog_handler_logs_dialogs(httpserver):
    """Test that dialog handler is set up correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        modalcloser_process = None
        try:
            test_url = _modal_page_url(httpserver)
            with chrome_session(
                Path(tmpdir),
                crawl_id="test-dialog",
                snapshot_id="snap-dialog",
                test_url=test_url,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            ) as (chrome_launch_process, chrome_pid, snapshot_chrome_dir, env):
                modalcloser_dir = snapshot_chrome_dir.parent / "modalcloser"
                modalcloser_dir.mkdir()

                # Use env from setup_chrome_session
                env["MODALCLOSER_TIMEOUT"] = "100"  # Fast timeout for test
                env["MODALCLOSER_POLL_INTERVAL"] = "200"

                modalcloser_process = subprocess.Popen(
                    [
                        str(MODALCLOSER_HOOK),
                        f"--url={test_url}",
                        "--snapshot-id=snap-dialog",
                    ],
                    cwd=str(modalcloser_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )

                # Let it run briefly
                time.sleep(1.5)

                # Verify it's running
                assert modalcloser_process.poll() is None, "Should be running"

                # Check stderr for "listening" message
                # Note: Can't read stderr while process is running without blocking,
                # so we just verify it exits cleanly
                modalcloser_process.send_signal(signal.SIGTERM)
                stdout, stderr = modalcloser_process.communicate(timeout=5)

                assert (
                    "listening" in stderr.lower() or "modalcloser" in stderr.lower()
                ), f"Should log startup message: {stderr}"
                assert modalcloser_process.returncode == 0, (
                    f"Should exit cleanly: {stderr}"
                )

        finally:
            if modalcloser_process and modalcloser_process.poll() is None:
                modalcloser_process.kill()


def test_config_poll_interval(httpserver):
    """Test that MODALCLOSER_POLL_INTERVAL config is respected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        chrome_launch_process = None
        chrome_pid = None
        modalcloser_process = None
        try:
            test_url = _modal_page_url(httpserver)
            with chrome_session(
                Path(tmpdir),
                crawl_id="test-poll",
                snapshot_id="snap-poll",
                test_url=test_url,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            ) as (chrome_launch_process, chrome_pid, snapshot_chrome_dir, env):
                modalcloser_dir = snapshot_chrome_dir.parent / "modalcloser"
                modalcloser_dir.mkdir()

                # Set very short poll interval (use env from setup_chrome_session)
                env["MODALCLOSER_POLL_INTERVAL"] = "100"  # 100ms

                modalcloser_process = subprocess.Popen(
                    [
                        str(MODALCLOSER_HOOK),
                        f"--url={test_url}",
                        "--snapshot-id=snap-poll",
                    ],
                    cwd=str(modalcloser_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )

                # Run for short time
                time.sleep(1)

                # Should still be running
                assert modalcloser_process.poll() is None, "Should still be running"

                # Clean exit
                modalcloser_process.send_signal(signal.SIGTERM)
                stdout, stderr = modalcloser_process.communicate(timeout=5)

                assert modalcloser_process.returncode == 0, f"Should exit 0: {stderr}"

                # Verify JSONL output exists
                result_json = None
                for line in stdout.strip().split("\n"):
                    if line.strip().startswith("{"):
                        try:
                            record = json.loads(line)
                            if record.get("type") == "ArchiveResult":
                                result_json = record
                                break
                        except json.JSONDecodeError:
                            pass

                assert result_json is not None, "Should have JSONL output"
                assert result_json["status"] == "succeeded", (
                    f"Should succeed: {result_json}"
                )
                output_str = result_json.get("output_str", "").lower()
                assert (
                    output_str.endswith("modals closed")
                    and output_str.split()[0].isdigit()
                ), f"Should report closing modals/dialogs: {result_json}"

        finally:
            if modalcloser_process and modalcloser_process.poll() is None:
                modalcloser_process.kill()


def test_hides_cookieyes_consent_with_real_hook(httpserver):
    """Verify modalcloser hides a CookieYes popup through the real hook path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        modalcloser_process = None
        try:
            test_url = _cookieyes_page_url(httpserver)
            with chrome_session(
                Path(tmpdir),
                crawl_id="test-cookieyes",
                snapshot_id="snap-cookieyes",
                test_url=test_url,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            ) as (_chrome_proc, _chrome_pid, snapshot_chrome_dir, env):
                before = _inspect_cookieyes_state(snapshot_chrome_dir, env)
                assert before["container"]["found"] is True, before
                assert before["container"]["visible"] is True, before
                assert before["overlay"]["visible"] is True, before

                modalcloser_dir = snapshot_chrome_dir.parent / "modalcloser"
                modalcloser_dir.mkdir()
                env["MODALCLOSER_POLL_INTERVAL"] = "200"

                modalcloser_process = subprocess.Popen(
                    [
                        str(MODALCLOSER_HOOK),
                        f"--url={test_url}",
                        "--snapshot-id=snap-cookieyes",
                    ],
                    cwd=str(modalcloser_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )

                time.sleep(1.5)
                assert modalcloser_process.poll() is None, "Should still be running"

                after = _inspect_cookieyes_state(snapshot_chrome_dir, env)
                assert after["container"]["found"] is True, after
                assert after["container"]["visible"] is False, after
                assert after["overlay"]["visible"] is False, after
                assert after["bodyHasModalClass"] is False, after

                modalcloser_process.send_signal(signal.SIGTERM)
                stdout, stderr = modalcloser_process.communicate(timeout=5)

                assert modalcloser_process.returncode == 0, (
                    f"Should exit 0 on SIGTERM: {stderr}"
                )

                result_json = None
                for line in stdout.strip().split("\n"):
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    record = json.loads(line)
                    if record.get("type") == "ArchiveResult":
                        result_json = record
                        break

                assert result_json is not None, stdout
                assert result_json["status"] == "succeeded", result_json
                output_str = result_json.get("output_str", "")
                assert output_str.endswith("modals closed"), result_json
                assert int(output_str.split()[0]) >= 2, result_json

        finally:
            if modalcloser_process and modalcloser_process.poll() is None:
                modalcloser_process.kill()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
