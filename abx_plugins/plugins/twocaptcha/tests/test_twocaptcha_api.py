# ci-environment: provider-api
"""Real 2Captcha account and extension checks that do not request a solve."""

import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_records
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    setup_test_env,
    wait_for_extensions_metadata,
)
from abx_plugins.plugins.twocaptcha.tests.test_twocaptcha import (
    CHROME_STARTUP_TIMEOUT_SECONDS,
    CONFIG_SCRIPT,
    NAVIGATE_HOOK,
    SNAPSHOT_HOOK,
    _TwoCaptchaRuntime,
    configure_twocaptcha,
    install_twocaptcha_extension,
    kill_chrome,
    launch_chrome,
    read_captcha_progress,
    twocaptcha_install_state,
)


class TestTwoCaptcha(_TwoCaptchaRuntime):
    def test_install_and_load(self):
        """Extension installs and loads in Chromium."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            env = setup_test_env(tmpdir)
            env["TWOCAPTCHA_API_KEY"] = self.api_key

            # Install
            loaded = install_twocaptcha_extension(env)
            data = twocaptcha_install_state(loaded)
            assert data["webstore_id"] == "ifibfemgeogfhoebkmokieepdoobkbpo"

            # Launch Chromium in crawls directory
            crawl_id = "test"
            crawl_dir = Path(env["CRAWL_DIR"]) / crawl_id
            chrome_dir = crawl_dir / "chrome"
            env["CRAWL_DIR"] = str(crawl_dir)
            process, cdp_url = launch_chrome(
                env,
                chrome_dir,
                crawl_id,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            )

            try:
                exts = wait_for_extensions_metadata(chrome_dir, timeout_seconds=10)
                assert any(e["name"] == "twocaptcha" for e in exts), (
                    f"twocaptcha not loaded: {exts}"
                )
                print(
                    f"[+] Extension loaded: id={next(e['id'] for e in exts if e['name'] == 'twocaptcha')}",
                )
            finally:
                kill_chrome(process, chrome_dir)

    def test_config_applied(self):
        """Configuration is applied to extension and verified via Config.getAll()."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            env = setup_test_env(tmpdir)
            env["TWOCAPTCHA_API_KEY"] = self.api_key
            env["TWOCAPTCHA_RETRY_COUNT"] = "5"
            env["TWOCAPTCHA_RETRY_DELAY"] = "10"

            install_twocaptcha_extension(env)

            # Launch Chromium in crawls directory
            crawl_id = "cfg"
            crawl_dir = Path(env["CRAWL_DIR"]) / crawl_id
            chrome_dir = crawl_dir / "chrome"
            env["CRAWL_DIR"] = str(crawl_dir)
            process, _ = launch_chrome(
                env,
                chrome_dir,
                crawl_id,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            )

            try:
                wait_for_extensions_metadata(chrome_dir, timeout_seconds=10)
                result = subprocess.run(
                    [
                        str(CONFIG_SCRIPT),
                        "--url=https://example.com",
                        "--snapshot-id=test",
                    ],
                    env=env,
                    timeout=30,
                    capture_output=True,
                    text=True,
                )
                assert result.returncode == 0, f"Config failed: {result.stderr}"
                assert (chrome_dir / ".twocaptcha_configured").exists()

                # The actual options-page Config.getAll() readback is part of
                # the configuration hook and is recorded before its CDP client
                # disconnects. The marker includes only safe observed values.
                config_marker = json.loads(
                    (chrome_dir / ".twocaptcha_configured").read_text(),
                )
                assert config_marker["method"] == "popup_login", config_marker
                assert config_marker["verified"] is True, config_marker
                assert config_marker["extensionId"]
                cfg = config_marker["config"]
                assert cfg.get("apiKeySet") is True, "API key readback mismatch"
                assert cfg.get("isPluginEnabled"), f"Plugin not enabled: {cfg}"
                assert cfg.get("repeatOnErrorTimes") == 5, f"Retry count wrong: {cfg}"
                assert cfg.get("repeatOnErrorDelay") == 10, f"Retry delay wrong: {cfg}"
                assert cfg.get("autoSolveRecaptchaV2"), (
                    f"autoSolveRecaptchaV2 not enabled: {cfg}"
                )
                assert cfg.get("autoSolveRecaptchaV3"), (
                    f"autoSolveRecaptchaV3 not enabled: {cfg}"
                )
                assert cfg.get("autoSolveTurnstile"), (
                    f"autoSolveTurnstile not enabled: {cfg}"
                )
                assert cfg.get("enabledForRecaptchaV2"), (
                    f"enabledForRecaptchaV2 not enabled: {cfg}"
                )
                assert cfg.get("recaptchaV2Type") == "token", (
                    f"reCAPTCHA v2 must use token solving: {cfg}"
                )

                print("[+] Config verified via Config.getAll()!")

            finally:
                kill_chrome(process, chrome_dir)

    def test_snapshot_hook_reports_noresults_without_captcha(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            install_env = setup_test_env(tmpdir)
            install_env["TWOCAPTCHA_API_KEY"] = self.api_key
            install_twocaptcha_extension(install_env)

            with chrome_session(
                tmpdir,
                crawl_id="twocaptcha-noresults",
                snapshot_id="twocaptcha-noresults-snap",
                test_url="https://archivebox.io/tests/",
                navigate=False,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
                env_overrides={"TWOCAPTCHA_API_KEY": str(self.api_key)},
                crawl_setup=lambda env, chrome_dir: configure_twocaptcha(
                    env,
                    chrome_dir,
                    url="https://archivebox.io/tests/",
                    snapshot_id="twocaptcha-noresults-snap",
                ),
            ) as (_chrome_launch_process, _chrome_pid, snapshot_chrome_dir, env):
                hook_dir = snapshot_chrome_dir.parent / "twocaptcha"
                hook_dir.mkdir(parents=True, exist_ok=True)

                hook_process = subprocess.Popen(
                    [
                        str(SNAPSHOT_HOOK),
                        "--url=https://archivebox.io/tests/",
                        "--snapshot-id=twocaptcha-noresults-snap",
                    ],
                    cwd=str(hook_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    start_new_session=True,
                )

                try:
                    assert (
                        read_captcha_progress(hook_process)
                        == "twocaptcha observer attached"
                    )
                    navigate = subprocess.run(
                        [
                            str(NAVIGATE_HOOK),
                            "--url=https://archivebox.io/tests/",
                            "--snapshot-id=twocaptcha-noresults-snap",
                        ],
                        cwd=str(snapshot_chrome_dir),
                        env=env,
                        timeout=60,
                        capture_output=True,
                        text=True,
                    )
                    assert navigate.returncode == 0, navigate.stderr

                    os.killpg(hook_process.pid, signal.SIGTERM)
                    stdout, stderr = hook_process.communicate(timeout=20)
                    returncode = hook_process.returncode
                    hook_process = None

                    assert returncode in (0, -signal.SIGTERM), stderr
                    records = parse_jsonl_records(stdout)
                    archive_result = next(
                        record
                        for record in records
                        if record.get("type") == "ArchiveResult"
                    )
                    assert archive_result["status"] == "noresults", archive_result
                    assert archive_result["output_str"] == "0 captchas solved", (
                        archive_result
                    )
                finally:
                    if hook_process is not None:
                        os.killpg(hook_process.pid, signal.SIGTERM)
                        hook_process.communicate(timeout=20)
