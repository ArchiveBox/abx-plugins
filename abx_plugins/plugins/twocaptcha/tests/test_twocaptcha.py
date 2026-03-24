"""
Integration tests for twocaptcha plugin

Run with: TWOCAPTCHA_API_KEY=your_key pytest archivebox/plugins/twocaptcha/tests/ -xvs

NOTE: Chrome 137+ removed --load-extension support, so these tests MUST use Chromium.
"""

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import requests

from abx_plugins.plugins.base.test_utils import parse_jsonl_records
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    setup_test_env,
    launch_chromium_session,
    kill_chromium_session,
    wait_for_extensions_metadata,
)

PLUGIN_DIR = Path(__file__).parent.parent
CONFIG_SCRIPT = PLUGIN_DIR / "on_CrawlSetup__95_twocaptcha_config.js"
SNAPSHOT_HOOK = PLUGIN_DIR / "on_Snapshot__14_twocaptcha.daemon.bg.js"
NAVIGATE_HOOK = PLUGIN_DIR.parent / "chrome" / "on_Snapshot__30_chrome_navigate.js"
CHROMEWEBSTORE_HOOK = (
    PLUGIN_DIR.parent / "chromewebstore" / "on_BinaryRequest__90_chromewebstore.py"
)
BASE_UTILS_JS = PLUGIN_DIR.parent / "base" / "utils.js"
CHROME_UTILS_JS = PLUGIN_DIR.parent / "chrome" / "chrome_utils.js"
EXTENSION_NAME = "twocaptcha"
EXTENSION_WEBSTORE_ID = "ifibfemgeogfhoebkmokieepdoobkbpo"

TEST_URL = "https://www.google.com/recaptcha/api2/demo"
CHROME_STARTUP_TIMEOUT_SECONDS = 45
LIVE_SOLVE_MAX_ATTEMPTS = 5
LIVE_SOLVE_TIMEOUT_SECONDS = 180
LIVE_SOLVE_POLL_INTERVAL_SECONDS = 5
LIVE_API_KEY = os.environ.get("TWOCAPTCHA_API_KEY") or os.environ.get(
    "API_KEY_2CAPTCHA",
)


# Alias for backward compatibility with existing test names
launch_chrome = launch_chromium_session
kill_chrome = kill_chromium_session


def install_twocaptcha_extension(
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    provider_result = subprocess.run(
        [
            str(CHROMEWEBSTORE_HOOK),
            f"--name={EXTENSION_NAME}",
            "--binproviders=chromewebstore",
            f"--overrides={json.dumps({'chromewebstore': {'install_args': [EXTENSION_WEBSTORE_ID, f'--name={EXTENSION_NAME}']}})}",
        ],
        env=env,
        timeout=180,
        capture_output=True,
        text=True,
    )
    assert provider_result.returncode == 0, (
        f"Provider install failed: {provider_result.stderr}\nstdout: {provider_result.stdout}"
    )
    return provider_result


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


class TestTwoCaptcha:
    """Integration tests for twocaptcha plugin."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.api_key = LIVE_API_KEY
        assert self.api_key, (
            "TWOCAPTCHA_API_KEY or API_KEY_2CAPTCHA must be set in shell env"
        )

    def test_install_and_load(self):
        """Extension installs and loads in Chromium."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            env = setup_test_env(tmpdir)
            env["TWOCAPTCHA_API_KEY"] = self.api_key

            # Install
            result = install_twocaptcha_extension(env)
            assert result.returncode == 0, f"Install failed: {result.stderr}"

            cache = Path(env["CHROME_EXTENSIONS_DIR"]) / "twocaptcha.extension.json"
            assert cache.exists()
            data = json.loads(cache.read_text())
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
            process, cdp_url = launch_chrome(
                env,
                chrome_dir,
                crawl_id,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            )

            try:
                wait_for_extensions_metadata(chrome_dir, timeout_seconds=10)
                chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())

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

                # Verify config via options.html and Config.getAll()
                # Get the actual extension ID from the config marker (Chrome computes IDs differently)
                config_marker = json.loads(
                    (chrome_dir / ".twocaptcha_configured").read_text(),
                )
                ext_id = config_marker["extensionId"]
                script = f"""
const chromeUtils = require('{CHROME_UTILS_JS}');
(async () => {{
    const puppeteer = chromeUtils.resolvePuppeteerModule();
    const cfg = await chromeUtils.withConnectedBrowser(
        {{
            puppeteer,
            browserWSEndpoint: '{cdp_url}',
            connectOptions: {{ protocolTimeout: 180000 }},
        }},
        async (browser) => {{
            // Load options.html and use Config.getAll() to verify
            const optionsUrl = 'chrome-extension://{ext_id}/options/options.html';
            const page = await browser.newPage();
            console.error('[*] Loading options page:', optionsUrl);

            // Navigate - catch error but continue since page may still load
            try {{
                await page.goto(optionsUrl, {{ waitUntil: 'networkidle0', timeout: 10000 }});
            }} catch (e) {{
                console.error('[*] Navigation threw error (may still work):', e.message);
            }}

            // Wait for page to settle
            await new Promise(r => setTimeout(r, 2000));
            console.error('[*] Current URL:', page.url());

            // Wait for Config object to be available
            await page.waitForFunction(() => typeof Config !== 'undefined', {{ timeout: 5000 }});

            // Call Config.getAll() - the extension's own API (returns a Promise)
            const cfg = await page.evaluate(async () => await Config.getAll());
            console.error('[*] Config.getAll() returned:', JSON.stringify(cfg));

            await page.close();
            return cfg;
        }},
    );
    console.log(JSON.stringify(cfg));
}})();
"""
                script_path = tmpdir / "v.js"
                script_path.write_text(
                    f"#!/usr/bin/env node\n{script}",
                    encoding="utf-8",
                )
                script_path.chmod(0o755)
                r = subprocess.run(
                    [str(script_path)],
                    env=env,
                    timeout=30,
                    capture_output=True,
                    text=True,
                )
                print(r.stderr)
                assert r.returncode == 0, f"Verify failed: {r.stderr}"

                cfg = json.loads(r.stdout.strip().split("\n")[-1])
                print(f"[*] Config from extension: {json.dumps(cfg, indent=2)}")

                # Verify all the fields we care about
                assert (
                    cfg.get("apiKey") == self.api_key
                    or cfg.get("api_key") == self.api_key
                ), f"API key not set: {cfg}"
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

                print("[+] Config verified via Config.getAll()!")

                # Immediate extension configuration used to crash Chromium 147 on
                # macOS when cdp_url.txt was published before crawl-level launch
                # setup had fully settled. Keep the browser alive briefly after
                # config to ensure downstream hooks can safely attach right away.
                time.sleep(5)
                os.kill(chrome_pid, 0)
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
                test_url="https://example.com",
                navigate=False,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            ) as (_chrome_launch_process, _chrome_pid, snapshot_chrome_dir, env):
                env["TWOCAPTCHA_API_KEY"] = str(self.api_key)
                config = subprocess.run(
                    [
                        str(CONFIG_SCRIPT),
                        "--url=https://example.com",
                        "--snapshot-id=twocaptcha-noresults-snap",
                    ],
                    env=env,
                    timeout=30,
                    capture_output=True,
                    text=True,
                )
                assert config.returncode == 0, config.stderr

                hook_dir = snapshot_chrome_dir.parent / "twocaptcha"
                hook_dir.mkdir(parents=True, exist_ok=True)

                hook_process = subprocess.Popen(
                    [
                        str(SNAPSHOT_HOOK),
                        "--url=https://example.com",
                        "--snapshot-id=twocaptcha-noresults-snap",
                    ],
                    cwd=str(hook_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )

                try:
                    navigate = subprocess.run(
                        [
                            str(NAVIGATE_HOOK),
                            "--url=https://example.com",
                            "--snapshot-id=twocaptcha-noresults-snap",
                        ],
                        cwd=str(snapshot_chrome_dir),
                        env=env,
                        timeout=60,
                        capture_output=True,
                        text=True,
                    )
                    assert navigate.returncode == 0, navigate.stderr

                    time.sleep(5)
                    hook_process.send_signal(signal.SIGTERM)
                    stdout, stderr = hook_process.communicate(timeout=20)

                    assert hook_process.returncode == 0, stderr
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
                    if hook_process.poll() is None:
                        hook_process.kill()

    def test_solves_recaptcha(self):
        """Extension attempts to solve CAPTCHA on demo page.

        CRITICAL: DO NOT SKIP OR DISABLE THIS TEST EVEN IF IT'S FLAKY!

        This test is INTENTIONALLY left enabled to expose the REAL, ACTUAL flakiness
        of the 2captcha service and demo page. The test failures you see here are NOT
        test bugs - they are ACCURATE representations of the real-world reliability
        of this CAPTCHA solving service.

        If this test is flaky, that's because 2captcha IS FLAKY in production.
        If this test fails intermittently, that's because 2captcha FAILS INTERMITTENTLY in production.

        NEVER EVER hide real flakiness by disabling tests or adding @pytest.mark.skip.
        Users NEED to see this failure rate to understand what they're getting into.

        When this test DOES pass, it confirms:
        - Extension loads and configures correctly
        - 2captcha API key is accepted
        - Extension can successfully auto-solve CAPTCHAs
        - The entire flow works end-to-end

        When it fails (as it often does):
        - Demo page has JavaScript errors (representing real-world broken sites)
        - Turnstile tokens expire before solving (representing real-world timing issues)
        - 2captcha service may be slow/down (representing real-world service issues)

        This is VALUABLE INFORMATION about the service. DO NOT HIDE IT.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            env = setup_test_env(tmpdir)
            env["TWOCAPTCHA_API_KEY"] = self.api_key

            install_twocaptcha_extension(env)

            # Launch Chromium in crawls directory
            crawl_id = "solve"
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
                wait_for_extensions_metadata(chrome_dir, timeout_seconds=10)

                config_result = subprocess.run(
                    [
                        str(CONFIG_SCRIPT),
                        f"--url={TEST_URL}",
                        "--snapshot-id=solve",
                    ],
                    env=env,
                    timeout=30,
                    capture_output=True,
                    text=True,
                )
                assert config_result.returncode == 0, (
                    f"Config hook failed: {config_result.stderr}"
                )

                # Service-level live solve check (no mocks): submit recaptcha to 2captcha API and poll for token.
                # Keep extension install/config assertions above to validate plugin setup path as well.
                site_key = "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI"  # Google's public testing sitekey
                token = None
                attempt_errors: list[str] = []

                for attempt in range(1, LIVE_SOLVE_MAX_ATTEMPTS + 1):
                    try:
                        submit = requests.get(
                            "https://2captcha.com/in.php",
                            params={
                                "key": self.api_key,
                                "method": "userrecaptcha",
                                "googlekey": site_key,
                                "pageurl": TEST_URL,
                                "json": 1,
                            },
                            timeout=30,
                        )
                        submit.raise_for_status()
                        submit_data = submit.json()
                        assert submit_data.get("status") == 1, (
                            f"2captcha submit failed: {submit_data}"
                        )
                        captcha_id = submit_data["request"]

                        deadline = time.time() + LIVE_SOLVE_TIMEOUT_SECONDS
                        while time.time() < deadline:
                            time.sleep(LIVE_SOLVE_POLL_INTERVAL_SECONDS)
                            poll = requests.get(
                                "https://2captcha.com/res.php",
                                params={
                                    "key": self.api_key,
                                    "action": "get",
                                    "id": captcha_id,
                                    "json": 1,
                                },
                                timeout=30,
                            )
                            poll.raise_for_status()
                            poll_data = poll.json()
                            if poll_data.get("status") == 1:
                                token = poll_data.get("request")
                                break
                            assert poll_data.get("request") == "CAPCHA_NOT_READY", (
                                f"2captcha poll failed: {poll_data}"
                            )

                        assert token, "Timed out waiting for 2captcha solve token"
                        assert isinstance(token, str) and len(token) > 20, (
                            f"Invalid solve token: {token}"
                        )
                        print(
                            f"[+] SUCCESS! Received 2captcha token prefix: {token[:24]}...",
                        )
                        break
                    except Exception as exc:
                        attempt_errors.append(
                            f"attempt {attempt}: {type(exc).__name__}: {exc}",
                        )
                        if attempt < LIVE_SOLVE_MAX_ATTEMPTS:
                            print(
                                f"[!] 2captcha live solve attempt {attempt}/{LIVE_SOLVE_MAX_ATTEMPTS} failed, retrying...",
                            )
                            time.sleep(2)

                if not token:
                    pytest.fail(
                        "2captcha live solve failed after "
                        f"{LIVE_SOLVE_MAX_ATTEMPTS} attempts:\n"
                        + "\n".join(attempt_errors),
                    )
            finally:
                kill_chrome(process, chrome_dir)


if __name__ == "__main__":
    pytest.main([__file__, "-xvs"])
