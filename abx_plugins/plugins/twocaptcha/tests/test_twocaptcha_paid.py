# ci-environment: provider-paid-manual
"""Explicitly approved real 2Captcha solve; never run this on a normal CI event."""

import os
import signal
import subprocess
import tempfile
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_records
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    setup_test_env,
)
from abx_plugins.plugins.twocaptcha.tests.test_twocaptcha import (
    CHROME_STARTUP_TIMEOUT_SECONDS,
    NAVIGATE_HOOK,
    SNAPSHOT_HOOK,
    TEST_URL,
    _TwoCaptchaRuntime,
    configure_twocaptcha,
    install_twocaptcha_extension,
    read_captcha_progress,
)


class TestTwoCaptchaPaid(_TwoCaptchaRuntime):
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
                crawl_setup=lambda env, chrome_dir: configure_twocaptcha(
                    env,
                    chrome_dir,
                    url=TEST_URL,
                    snapshot_id=snapshot_id,
                ),
            ) as (_chrome_launch_process, _chrome_pid, snapshot_chrome_dir, env):
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
                    assert (
                        read_captcha_progress(hook_process)
                        == "twocaptcha observer attached"
                    )
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
