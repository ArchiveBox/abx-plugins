"""A timed-out real browser installer must not survive its owning bootstrap."""

import signal
import subprocess
import threading

import psutil
import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    _run_chrome_required_binary_env,
    get_test_env,
)


def test_chrome_bootstrap_timeout_reaps_real_playwright_installer(
    tmp_path,
    ensure_chrome_test_prereqs,
):
    env = get_test_env()
    env.update(
        {
            "CHROME_BINARY": "chromium",
            "ABXPKG_PLAYWRIGHT_ROOT": str(tmp_path / "playwright"),
        },
    )
    finished = threading.Event()
    stopped = threading.Event()
    installers = []
    monitor_errors = []

    def signal_process(process, signum):
        try:
            process.send_signal(signum)
        except psutil.AccessDenied:
            subprocess.run(
                ["sudo", "-n", "--", "kill", f"-{signum}", str(process.pid)],
                check=True,
                capture_output=True,
                timeout=5,
            )

    def stop_installer():
        try:
            parent = psutil.Process()
            while not finished.wait(0.02):
                for process in parent.children(recursive=True):
                    try:
                        command = process.cmdline()
                        if (
                            process.name() == "node"
                            and "install" in command
                            and "chromium" in command
                            and any("playwright" in arg for arg in command)
                        ):
                            installers.extend(
                                [process, *process.children(recursive=True)],
                            )
                            signal_process(process, signal.SIGSTOP)
                            stopped.set()
                            return
                    except (
                        psutil.NoSuchProcess,
                        psutil.ZombieProcess,
                        psutil.AccessDenied,
                    ):
                        continue
        except BaseException as error:
            monitor_errors.append(error)

    monitor = threading.Thread(target=stop_installer)
    monitor.start()
    try:
        with pytest.raises(subprocess.TimeoutExpired) as timeout:
            result = _run_chrome_required_binary_env(env, timeout=20)
            pytest.fail(f"Installer returned {result.returncode}: {result.stderr}")
        assert not monitor_errors, monitor_errors
        assert stopped.is_set(), "The real Playwright installer did not start"
        assert "Installing chromium via playwright" in str(timeout.value.stderr)
        assert installers
        _, alive = psutil.wait_procs(installers, timeout=5)
        assert not alive, (
            f"Browser installers survived bootstrap timeout: {[process.pid for process in alive]}"
        )
    finally:
        finished.set()
        monitor.join(timeout=5)
        for process in reversed(installers):
            try:
                signal_process(process, signal.SIGKILL)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
        psutil.wait_procs(installers, timeout=5)
