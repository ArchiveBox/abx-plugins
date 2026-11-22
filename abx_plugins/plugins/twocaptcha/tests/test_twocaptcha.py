"""Credential-free 2Captcha behavior through the real hook."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import (
    install_required_binary_from_config,
    parse_jsonl_records,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    kill_chromium_session,
    launch_chromium_session,
    wait_for_extensions_metadata,
)

PLUGIN_DIR = Path(__file__).parent.parent
CONFIG_SCRIPT = PLUGIN_DIR / "on_CrawlSetup__95_twocaptcha_config.js"
SNAPSHOT_HOOK = PLUGIN_DIR / "on_Snapshot__14_twocaptcha.daemon.bg.js"
NAVIGATE_HOOK = PLUGIN_DIR.parent / "chrome" / "on_Snapshot__30_chrome_navigate.js"
BASE_UTILS_JS = PLUGIN_DIR.parent / "base" / "utils.js"
CHROME_UTILS_JS = PLUGIN_DIR.parent / "chrome" / "chrome_utils.js"
EXTENSION_NAME = "twocaptcha"
EXTENSION_WEBSTORE_ID = "ifibfemgeogfhoebkmokieepdoobkbpo"

TEST_URL = "https://www.google.com/recaptcha/api2/demo"
CHROME_STARTUP_TIMEOUT_SECONDS = 120


def read_captcha_progress(process: subprocess.Popen[str]) -> str:
    """Read one production progress event from the 2Captcha snapshot hook."""
    assert process.stdout is not None
    line = process.stdout.readline()
    if not line:
        returncode = process.wait(timeout=5)
        assert process.stderr is not None
        stderr = process.stderr.read()
        raise AssertionError(
            "2Captcha hook exited without publishing a progress event "
            f"(exit {returncode}):\n{stderr}",
        )
    return line.strip()


# Alias for backward compatibility with existing test names
launch_chrome = launch_chromium_session
kill_chrome = kill_chromium_session


def install_twocaptcha_extension(
    env: dict[str, str],
):
    loaded = install_required_binary_from_config(PLUGIN_DIR, EXTENSION_NAME, env=env)
    assert loaded.loaded_abspath is not None, f"abxpkg did not resolve {EXTENSION_NAME}"
    assert loaded.loaded_abspath.exists(), loaded.loaded_abspath
    return loaded


def twocaptcha_install_state(loaded) -> dict:
    assert loaded.loaded_abspath is not None
    manifest_path = Path(loaded.loaded_abspath)
    assert manifest_path.exists(), manifest_path
    unpacked_dir = manifest_path.parent

    for cache_file in (
        unpacked_dir.parent / "twocaptcha.extension.json",
        unpacked_dir / "twocaptcha.extension.json",
    ):
        if cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "name": EXTENSION_NAME,
        "webstore_id": EXTENSION_WEBSTORE_ID,
        "version": manifest.get("version"),
        "unpacked_path": str(unpacked_dir),
    }


def configure_twocaptcha(
    env: dict[str, str],
    chrome_dir: Path,
    *,
    url: str,
    snapshot_id: str,
) -> None:
    wait_for_extensions_metadata(chrome_dir, timeout_seconds=10)
    result = subprocess.run(
        [
            str(CONFIG_SCRIPT),
            f"--url={url}",
            f"--snapshot-id={snapshot_id}",
        ],
        env=env,
        timeout=30,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Config hook failed: {result.stderr}"
    assert (chrome_dir / ".twocaptcha_configured").exists()


def test_config_uses_one_vendor_attempt_by_default():
    config = json.loads((PLUGIN_DIR / "config.json").read_text())
    assert config["properties"]["TWOCAPTCHA_RETRY_COUNT"]["default"] == 0


def test_snapshot_hook_reports_skipped_when_disabled():
    env = os.environ.copy()
    env["TWOCAPTCHA_ENABLED"] = "false"

    result = subprocess.run(
        [str(SNAPSHOT_HOOK), "--url=https://example.com"],
        env=env,
        timeout=30,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    records = parse_jsonl_records(result.stdout)
    archive_result = next(
        record for record in records if record.get("type") == "ArchiveResult"
    )
    assert archive_result["status"] == "skipped", archive_result
    assert archive_result["output_str"] == "TWOCAPTCHA_ENABLED=False", archive_result


def test_snapshot_hook_reports_skipped_when_api_key_missing():
    env = os.environ.copy()
    env["TWOCAPTCHA_ENABLED"] = "true"
    env.pop("TWOCAPTCHA_API_KEY", None)
    env.pop("API_KEY_2CAPTCHA", None)

    result = subprocess.run(
        [str(SNAPSHOT_HOOK), "--url=https://example.com"],
        env=env,
        timeout=30,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    records = parse_jsonl_records(result.stdout)
    archive_result = next(
        record for record in records if record.get("type") == "ArchiveResult"
    )
    assert archive_result["status"] == "skipped", archive_result
    assert archive_result["output_str"] == "TWOCAPTCHA_API_KEY=None", archive_result


class _TwoCaptchaRuntime:
    """Integration tests for twocaptcha plugin."""

    pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

    @pytest.fixture(autouse=True)
    def setup(self):
        self.api_key = os.environ.get("TWOCAPTCHA_API_KEY") or os.environ.get(
            "API_KEY_2CAPTCHA",
        )
        assert self.api_key, (
            "TWOCAPTCHA_API_KEY or API_KEY_2CAPTCHA must be set in shell env"
        )
