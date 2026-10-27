"""A timed-out real browser installer must not survive its owning bootstrap."""

import signal
import subprocess
import tempfile
import threading
from pathlib import Path

import psutil
import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_PLUGIN_DIR,
    _run_chrome_required_binary_env,
    get_test_env,
)
from abx_plugins.plugins.base.utils import load_required_binary_from_config


def test_chrome_bootstrap_timeout_reaps_real_browser_installer(
    request,
    ensure_chrome_test_prereqs,
):
    env = get_test_env()
    puppeteer_root = tempfile.TemporaryDirectory(
        prefix="bootstrap-timeout-test-",
        dir=env["ABXPKG_LIB_DIR"],
    )
    request.addfinalizer(puppeteer_root.cleanup)
    shared_browser = Path(ensure_chrome_test_prereqs).resolve(strict=True)
    shared_stat = shared_browser.stat()
    shared_version = subprocess.run(
        [str(shared_browser), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout
    env.update(
        {
            "CHROME_BINARY": "chromium",
            "ABXPKG_PUPPETEER_ROOT": puppeteer_root.name,
        },
    )
    # The timed run must reach the browser installer. First provision a real
    # isolated browser cache so pnpm and APT setup happen before the 20s oracle.
    setup = _run_chrome_required_binary_env(env)
    assert setup.returncode == 0, setup.stderr or setup.stdout
    browser = load_required_binary_from_config(
        "chromium",
        CHROME_PLUGIN_DIR / "config.json",
        global_config=env,
        environ=env,
        install=True,
    )
    installed_path = Path(browser.loaded_abspath).resolve(strict=True)
    assert installed_path.is_relative_to(Path(puppeteer_root.name).resolve())
    assert installed_path != shared_browser
    assert installed_path.exists()
    # Exercise the provider's uninstall path; leave its CLI and system
    # prerequisites ready for the timed real reinstall below.
    browser.uninstall(binproviders=["puppeteer"])
    assert not installed_path.exists()
    assert shared_browser.exists()
    assert (
        shared_browser.stat().st_dev,
        shared_browser.stat().st_ino,
        shared_browser.stat().st_size,
        shared_browser.stat().st_mtime_ns,
    ) == (
        shared_stat.st_dev,
        shared_stat.st_ino,
        shared_stat.st_size,
        shared_stat.st_mtime_ns,
    )
    assert (
        subprocess.run(
            [str(shared_browser), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
        == shared_version
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
                        # Node 24 names its main thread "MainThread" on Linux.
                        # Match the executable argument, not that mutable name.
                        if (
                            command
                            and Path(command[0]).name == "node"
                            and "install" in command
                            and any(arg.startswith("chrome@") for arg in command)
                            and any("puppeteer" in arg for arg in command)
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
        assert stopped.is_set(), (
            "The real Puppeteer installer did not start\n"
            f"stdout={timeout.value.stdout}\nstderr={timeout.value.stderr}"
        )
        assert "Installing chromium via puppeteer" in str(timeout.value.stderr)
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
