"""
Integration tests for screenshot plugin

Tests verify:
1. Hook script exists
2. Dependencies installed via chrome validation hooks
3. Verify deps with abxpkg
4. Screenshot extraction works on https://example.com
5. JSONL output is correct
6. Filesystem output is valid PNG image
7. Config options work
"""

import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import (
    get_hook_script,
    get_plugin_dir,
    install_binary_with_abxpkg,
    parse_jsonl_output,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_PLUGIN_DIR,
    chrome_session,
    get_test_env,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

PLUGIN_DIR = get_plugin_dir(__file__)
_SCREENSHOT_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_screenshot.*")
if _SCREENSHOT_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
SCREENSHOT_HOOK = _SCREENSHOT_HOOK

# Get Chrome hooks for setting up sessions
_CHROME_LAUNCH_HOOK = get_hook_script(
    CHROME_PLUGIN_DIR,
    "on_CrawlSetup__*_chrome_launch.*",
)
if _CHROME_LAUNCH_HOOK is None:
    raise FileNotFoundError(f"Chrome launch hook not found in {CHROME_PLUGIN_DIR}")
CHROME_LAUNCH_HOOK = _CHROME_LAUNCH_HOOK
_CHROME_TAB_HOOK = get_hook_script(CHROME_PLUGIN_DIR, "on_Snapshot__*_chrome_tab.*")
if _CHROME_TAB_HOOK is None:
    raise FileNotFoundError(f"Chrome tab hook not found in {CHROME_PLUGIN_DIR}")
CHROME_TAB_HOOK = _CHROME_TAB_HOOK
_CHROME_NAVIGATE_HOOK = get_hook_script(
    CHROME_PLUGIN_DIR,
    "on_Snapshot__*_chrome_navigate.*",
)
if _CHROME_NAVIGATE_HOOK is None:
    raise FileNotFoundError(f"Chrome navigate hook not found in {CHROME_PLUGIN_DIR}")
CHROME_NAVIGATE_HOOK = _CHROME_NAVIGATE_HOOK
CHROME_STARTUP_TIMEOUT_SECONDS = 45


@pytest.fixture(scope="module", autouse=True)
def _ensure_chrome_prereqs(ensure_chromium_and_puppeteer_installed):
    return ensure_chromium_and_puppeteer_installed


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert SCREENSHOT_HOOK.exists(), f"Hook not found: {SCREENSHOT_HOOK}"


def test_verify_deps_with_abxpkg():
    """Verify dependencies are available via abxpkg after hook installation."""
    node_loaded = install_binary_with_abxpkg("node", binproviders="env,apt,brew")
    assert node_loaded and node_loaded.abspath, "Node.js required for screenshot plugin"


def test_screenshot_with_chrome_session(chrome_test_url):
    """Test multiple screenshot scenarios with one Chrome session to save time."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_url = chrome_test_url
        snapshot_id = "test-screenshot-snap"

        try:
            with chrome_session(
                Path(tmpdir),
                crawl_id="test-screenshot-crawl",
                snapshot_id=snapshot_id,
                test_url=test_url,
                navigate=True,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            ) as (chrome_process, chrome_pid, snapshot_chrome_dir, env):
                # Scenario 1: Basic screenshot extraction
                screenshot_dir = snapshot_chrome_dir.parent / "screenshot"
                screenshot_dir.mkdir()

                try:
                    result = subprocess.run(
                        [
                            str(SCREENSHOT_HOOK),
                            f"--url={test_url}",
                            f"--snapshot-id={snapshot_id}",
                        ],
                        cwd=str(screenshot_dir),
                        capture_output=True,
                        text=True,
                        timeout=120,
                        env=env,
                    )
                except subprocess.TimeoutExpired:
                    pytest.fail("Screenshot capture timed out")

                if (
                    result.returncode != 0
                    and "Screenshot capture timed out" in result.stderr
                ):
                    pytest.fail(f"Screenshot capture timed out: {result.stderr}")

                assert result.returncode == 0, (
                    f"Screenshot extraction failed:\nStderr: {result.stderr}"
                )

                # Parse JSONL output
                result_json = parse_jsonl_output(result.stdout)

                assert result_json and result_json["status"] == "succeeded"
                assert result_json["output_str"] == "screenshot/screenshot.png"
                screenshot_file = screenshot_dir / "screenshot.png"
                assert (
                    screenshot_file.exists() and screenshot_file.stat().st_size > 1000
                )
                assert screenshot_file.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

                # Scenario 2: Wrong target ID (error case)
                screenshot_dir3 = snapshot_chrome_dir.parent / "screenshot3"
                screenshot_dir3.mkdir()
                (snapshot_chrome_dir / "target_id.txt").write_text(
                    "nonexistent-target-id",
                )

                result = subprocess.run(
                    [
                        str(SCREENSHOT_HOOK),
                        f"--url={test_url}",
                        f"--snapshot-id={snapshot_id}",
                    ],
                    cwd=str(screenshot_dir3),
                    capture_output=True,
                    text=True,
                    timeout=20,
                    env=env,
                )

                assert result.returncode != 0
                assert (
                    "target" in result.stderr.lower()
                    and "not found" in result.stderr.lower()
                )

        except RuntimeError:
            raise


def test_skips_when_staticfile_exists(chrome_test_url):
    """Test that screenshot skips when staticfile extractor already handled the URL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        snap_dir = Path(tmpdir) / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        snapshot_dir = snap_dir / "snap-skip"
        screenshot_dir = snapshot_dir / "screenshot"
        screenshot_dir.mkdir(parents=True)

        # Create staticfile output to simulate staticfile extractor already ran
        staticfile_dir = snapshot_dir / "staticfile"
        staticfile_dir.mkdir()
        (staticfile_dir / "stdout.log").write_text(
            '{"type":"ArchiveResult","status":"succeeded","output_str":"responses/example.com/test.json","content_type":"application/json"}\n',
        )

        env = get_test_env() | {"SNAP_DIR": str(snapshot_dir)}
        result = subprocess.run(
            [
                str(SCREENSHOT_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=snap-skip",
            ],
            cwd=str(screenshot_dir),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, f"Should exit successfully: {result.stderr}"

        # Should emit skipped status
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "noresults", f"Should noresult: {result_json}"
        assert result_json["output_str"] == "staticfile already handled"
        assert not (screenshot_dir / "screenshot.png").exists()


def test_config_save_screenshot_false_skips(chrome_test_url):
    """Test that SCREENSHOT_ENABLED=False exits with skipped JSONL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        env = get_test_env()
        env["SCREENSHOT_ENABLED"] = "False"
        env["SNAP_DIR"] = str(snap_dir)

        result = subprocess.run(
            [
                str(SCREENSHOT_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=test999",
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

        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Should emit JSONL when disabled"
        assert result_json["type"] == "ArchiveResult"
        assert result_json["status"] == "skipped"
        assert result_json["output_str"] == "SCREENSHOT_ENABLED=False"
        assert not (snap_dir / "screenshot" / "screenshot.png").exists()


def test_reports_missing_chrome_session(chrome_test_url):
    """Test that script reports an ArchiveResult failure when no Chrome session exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Set CHROME_BINARY to nonexistent path
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        env = get_test_env() | {"SNAP_DIR": str(snap_dir)}
        env["CHROME_BINARY"] = "/nonexistent/chrome"

        result = subprocess.run(
            [
                str(SCREENSHOT_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=test123",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode != 0, (
            f"Should fail when no chrome session exists.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        result_json = parse_jsonl_output(result.stdout)
        assert result_json is not None, "Should emit failed ArchiveResult"
        assert result_json["status"] == "failed", result_json
        combined = result.stdout + result.stderr
        assert "chrome" in combined.lower(), combined


def test_waits_for_navigation_timeout(chrome_test_url):
    """Test that screenshot waits for navigation.json and times out quickly if missing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)

        # Create chrome directory without navigation.json to trigger timeout
        chrome_dir = snap_dir / "chrome"
        chrome_dir.mkdir(parents=True, exist_ok=True)
        (chrome_dir / "cdp_url.txt").write_text(
            "ws://chrome-cdp.localhost:9222/devtools/browser/test",
        )
        (chrome_dir / "target_id.txt").write_text("test-target-id")
        # Intentionally NOT creating navigation.json to test timeout

        screenshot_dir = snap_dir / "screenshot"
        screenshot_dir.mkdir()

        env = get_test_env() | {"SNAP_DIR": str(snap_dir)}
        env["SCREENSHOT_TIMEOUT"] = "2"  # Set 2 second timeout

        start_time = time.time()
        result = subprocess.run(
            [
                str(SCREENSHOT_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=test-timeout",
            ],
            cwd=str(screenshot_dir),
            capture_output=True,
            text=True,
            timeout=5,  # Test timeout slightly higher than SCREENSHOT_TIMEOUT
            env=env,
        )
        elapsed = time.time() - start_time

        # Should fail when navigation.json doesn't appear
        assert result.returncode != 0, "Should fail when navigation.json missing"
        assert (
            "not loaded" in result.stderr.lower() or "navigate" in result.stderr.lower()
        ), f"Should mention navigation timeout: {result.stderr}"
        # Should complete within 3s (2s wait + 1s overhead)
        assert elapsed < 3, f"Should timeout within 3s, took {elapsed:.1f}s"


def test_config_timeout_honored(chrome_test_url):
    """Test that SCREENSHOT_TIMEOUT config controls the navigation wait budget."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        chrome_dir = snap_dir / "chrome"
        chrome_dir.mkdir()
        (chrome_dir / "cdp_url.txt").write_text("ws://chrome-cdp.localhost:9222/test")
        (chrome_dir / "target_id.txt").write_text("test-target")

        env = get_test_env() | {"SNAP_DIR": str(snap_dir)}
        env["SCREENSHOT_TIMEOUT"] = "1"

        start = time.time()
        result = subprocess.run(
            [
                str(SCREENSHOT_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=testtimeout",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
        elapsed = time.time() - start

        assert result.returncode != 0, "Should fail when navigation never completes"
        assert elapsed < 2.5, f"Should honor 1s timeout, took {elapsed:.1f}s"
        result_json = parse_jsonl_output(result.stdout)
        assert result_json is not None
        assert result_json["status"] == "failed", result_json


def test_missing_url_argument():
    """Test that hook fails gracefully when URL argument is missing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        env = get_test_env() | {"SNAP_DIR": str(snap_dir)}
        result = subprocess.run(
            [str(SCREENSHOT_HOOK), "--snapshot-id=test-missing-url"],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        # Should exit with error
        assert result.returncode != 0, "Should fail when URL is missing"
        assert "Usage:" in result.stderr or "url" in result.stderr.lower()


def test_url_only_without_snapshot_id_argument(chrome_test_url):
    """Test that hook accepts URL without snapshot-id."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        env = get_test_env() | {"SNAP_DIR": str(snap_dir)}
        env["SCREENSHOT_ENABLED"] = "False"
        result = subprocess.run(
            [str(SCREENSHOT_HOOK), f"--url={chrome_test_url}"],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        # Should skip successfully and not require --snapshot-id
        assert result.returncode == 0, "Should not require snapshot-id"
        result_json = parse_jsonl_output(result.stdout)
        assert result_json is not None
        assert result_json["status"] == "skipped", result_json
        assert result_json["output_str"] == "SCREENSHOT_ENABLED=False"


def test_no_cdp_url_fails(chrome_test_url):
    """Test error when chrome dir exists but no cdp_url.txt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        chrome_dir = snap_dir / "chrome"
        chrome_dir.mkdir()
        # Create target_id.txt and navigation.json but NOT cdp_url.txt
        (chrome_dir / "target_id.txt").write_text("test-target")
        (chrome_dir / "navigation.json").write_text("{}")

        screenshot_dir = snap_dir / "screenshot"
        screenshot_dir.mkdir()

        result = subprocess.run(
            [
                str(SCREENSHOT_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=test",
            ],
            cwd=str(screenshot_dir),
            capture_output=True,
            text=True,
            timeout=7,
            env=get_test_env() | {"SNAP_DIR": str(snap_dir)},
        )

        assert result.returncode != 0
        assert "no chrome session" in result.stderr.lower()


def test_no_target_id_fails(chrome_test_url):
    """Test error when cdp_url exists but no target_id.txt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        chrome_dir = snap_dir / "chrome"
        chrome_dir.mkdir()
        # Create cdp_url.txt and navigation.json but NOT target_id.txt
        (chrome_dir / "cdp_url.txt").write_text(
            "ws://chrome-cdp.localhost:9222/devtools/browser/test",
        )
        (chrome_dir / "navigation.json").write_text("{}")

        screenshot_dir = snap_dir / "screenshot"
        screenshot_dir.mkdir()

        result = subprocess.run(
            [
                str(SCREENSHOT_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=test",
            ],
            cwd=str(screenshot_dir),
            capture_output=True,
            text=True,
            timeout=7,
            env=get_test_env() | {"SNAP_DIR": str(snap_dir)},
        )

        assert result.returncode != 0
        assert "target_id.txt" in result.stderr.lower()


def test_invalid_cdp_url_fails(chrome_test_url):
    """Test error with malformed CDP URL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        chrome_dir = snap_dir / "chrome"
        chrome_dir.mkdir()
        (chrome_dir / "cdp_url.txt").write_text("invalid-url")
        (chrome_dir / "target_id.txt").write_text("test-target")
        (chrome_dir / "navigation.json").write_text("{}")

        screenshot_dir = snap_dir / "screenshot"
        screenshot_dir.mkdir()

        result = subprocess.run(
            [
                str(SCREENSHOT_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=test",
            ],
            cwd=str(screenshot_dir),
            capture_output=True,
            text=True,
            timeout=7,
            env=get_test_env() | {"SNAP_DIR": str(snap_dir)},
        )

        assert result.returncode != 0


def test_invalid_timeout_uses_default(chrome_test_url):
    """Test that invalid SCREENSHOT_TIMEOUT fails through the hook's ArchiveResult path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        chrome_dir = snap_dir / "chrome"
        chrome_dir.mkdir()
        # No navigation.json to trigger timeout
        (chrome_dir / "cdp_url.txt").write_text("ws://chrome-cdp.localhost:9222/test")
        (chrome_dir / "target_id.txt").write_text("test")

        screenshot_dir = snap_dir / "screenshot"
        screenshot_dir.mkdir()

        env = get_test_env() | {"SNAP_DIR": str(snap_dir)}
        env["SCREENSHOT_TIMEOUT"] = (
            "invalid"  # Should fallback to default (10s becomes NaN, treated as 0)
        )

        start = time.time()
        result = subprocess.run(
            [
                str(SCREENSHOT_HOOK),
                f"--url={chrome_test_url}",
                "--snapshot-id=test",
            ],
            cwd=str(screenshot_dir),
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        elapsed = time.time() - start

        assert result.returncode != 0
        assert elapsed < 2
        result_json = parse_jsonl_output(result.stdout)
        assert result_json is not None
        assert result_json["status"] == "failed", result_json
        assert "Invalid SCREENSHOT_TIMEOUT=invalid" in result_json["output_str"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
