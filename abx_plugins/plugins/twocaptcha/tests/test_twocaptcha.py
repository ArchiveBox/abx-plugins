"""
Integration tests for twocaptcha plugin

Run with: TWOCAPTCHA_API_KEY=your_key pytest archivebox/plugins/twocaptcha/tests/ -vs

NOTE: These tests require Chromium-family builds with CDP Extensions.loadUnpacked support.
"""

import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import (
    install_required_binary_from_config,
    parse_jsonl_records,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    setup_test_env,
    launch_chromium_session,
    kill_chromium_session,
    wait_for_extensions_metadata,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

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
LIVE_API_KEY = os.environ.get("TWOCAPTCHA_API_KEY") or os.environ.get(
    "API_KEY_2CAPTCHA",
)


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
            process, cdp_url = launch_chrome(
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

                # Verify config via options.html and Config.getAll()
                # Get the runtime extension ID from the config marker
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
                r = subprocess.run(
                    [env["NODE_BINARY"], "-e", script],
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
                test_url="https://example.com",
                navigate=False,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            ) as (_chrome_launch_process, _chrome_pid, snapshot_chrome_dir, env):
                env["TWOCAPTCHA_API_KEY"] = str(self.api_key)
                wait_for_extensions_metadata(
                    Path(env["CRAWL_DIR"]) / "chrome",
                    timeout_seconds=10,
                )
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
                assert (
                    Path(env["CRAWL_DIR"]) / "chrome" / ".twocaptcha_configured"
                ).exists()

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
                    start_new_session=True,
                )

                try:
                    assert read_captcha_progress(hook_process) == "0 captchas detected"
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

    def test_solves_recaptcha(self):
        """Solve the public reCAPTCHA demo through the real extension lifecycle."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            install_env = setup_test_env(tmpdir)
            install_env["TWOCAPTCHA_API_KEY"] = self.api_key

            install_twocaptcha_extension(install_env)

            crawl_id = "solve"
            snapshot_id = "solve"
            with chrome_session(
                tmpdir,
                crawl_id=crawl_id,
                snapshot_id=snapshot_id,
                test_url=TEST_URL,
                navigate=False,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
                env_overrides={"TWOCAPTCHA_API_KEY": str(self.api_key)},
            ) as (_chrome_launch_process, _chrome_pid, snapshot_chrome_dir, env):
                chrome_dir = Path(env["CRAWL_DIR"]) / "chrome"
                wait_for_extensions_metadata(chrome_dir, timeout_seconds=10)

                config_result = subprocess.run(
                    [
                        str(CONFIG_SCRIPT),
                        f"--url={TEST_URL}",
                        f"--snapshot-id={snapshot_id}",
                    ],
                    env=env,
                    timeout=30,
                    capture_output=True,
                    text=True,
                )
                assert config_result.returncode == 0, (
                    f"Config hook failed: {config_result.stderr}"
                )

                hook_dir = snapshot_chrome_dir.parent / "twocaptcha"
                hook_dir.mkdir(parents=True, exist_ok=True)
                hook_process = subprocess.Popen(
                    [
                        str(SNAPSHOT_HOOK),
                        f"--url={TEST_URL}",
                        f"--snapshot-id={snapshot_id}",
                    ],
                    cwd=hook_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    start_new_session=True,
                )
                try:
                    assert read_captcha_progress(hook_process) == "0 captchas detected"
                    navigate = subprocess.run(
                        [
                            str(NAVIGATE_HOOK),
                            f"--url={TEST_URL}",
                            f"--snapshot-id={snapshot_id}",
                        ],
                        cwd=snapshot_chrome_dir,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    assert navigate.returncode == 0, navigate.stderr
                    assert read_captcha_progress(hook_process) == "1 captcha detected"
                    assert read_captcha_progress(hook_process) == "1 captcha solved"

                    os.killpg(hook_process.pid, signal.SIGTERM)
                    stdout, stderr = hook_process.communicate(timeout=20)
                    hook_process = None
                    archive_result = next(
                        record
                        for record in parse_jsonl_records(stdout)
                        if record.get("type") == "ArchiveResult"
                    )
                    assert archive_result["status"] == "succeeded", archive_result
                    assert archive_result["output_str"] == "1 captcha solved"
                finally:
                    if hook_process is not None:
                        if hook_process.poll() is None:
                            os.killpg(hook_process.pid, signal.SIGTERM)
                        hook_process.communicate(timeout=20)


if __name__ == "__main__":
    pytest.main([__file__, "-vs"])
