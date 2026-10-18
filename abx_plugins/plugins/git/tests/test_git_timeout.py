"""Real Git interruption must not poison the next capture or orphan helpers."""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil

from abx_plugins.plugins.base.testing import parse_jsonl_output

HOOK = next(Path(__file__).parents[1].glob("on_Snapshot__*_git.*"))


def run_git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def init_repo(repo):
    repo.mkdir()
    run_git(repo, "init")
    run_git(repo, "config", "user.name", "ArchiveBox regression")
    run_git(repo, "config", "user.email", "regression@archivebox.io")


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_listener(port):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.01)
    raise AssertionError("Actual Git/HTTP server did not listen")


def stop_owned_process(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def test_timed_out_checkout_releases_its_own_index_lock(tmp_path):
    child_repo = tmp_path / "child.git"
    init_repo(child_repo)
    (child_repo / "nested.txt").write_text("Actual nested submodule data\n")
    run_git(child_repo, "add", ".")
    run_git(child_repo, "commit", "-m", "Nested content")
    nested_commit = run_git(child_repo, "rev-parse", "HEAD")
    source = tmp_path / "source.git"
    init_repo(source)
    # Enough real checkout work to observe index.lock without artificial Git
    # binaries or filters. SIGSTOP injects an OS stall into the actual reset.
    for number in range(4000):
        folder = source / str(number // 100)
        folder.mkdir(exist_ok=True)
        (folder / f"{number}.txt").write_text(f"Actual checkout entry {number}\n" * 32)
    run_git(
        source,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(child_repo),
        "nested",
    )
    run_git(source, "add", ".")
    run_git(source, "commit", "-m", "Actual checkout fixture")
    expected_commit = run_git(source, "rev-parse", "HEAD")
    port = unused_port()
    daemon = subprocess.Popen(
        [
            "git",
            "daemon",
            "--export-all",
            "--reuseaddr",
            "--listen=127.0.0.1",
            f"--port={port}",
            f"--base-path={tmp_path}",
            str(tmp_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    hook = None
    try:
        wait_for_listener(port)
        snap = tmp_path / "snapshot"
        snap.mkdir()
        # Git's native URL rewrite keeps the public hook input HTTP-shaped
        # while all repository traffic goes to the real local Git daemon.
        env = {
            **os.environ,
            "SNAP_DIR": str(snap),
            "CRAWL_DIR": str(snap),
            "GIT_TIMEOUT": "10",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.git://127.0.0.1:{port}/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://archivebox.io/tests/",
            "GIT_ALLOW_PROTOCOL": "git:file",
        }
        command = [str(HOOK), "--url", "https://archivebox.io/tests/source.git"]
        hook = subprocess.Popen(
            command,
            cwd=snap,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        owner = psutil.Process(hook.pid)
        lock = snap / "git/.git/index.lock"
        stopped = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and hook.poll() is None:
            if lock.exists():
                for child in owner.children(recursive=True):
                    try:
                        args = child.cmdline()
                        if "reset" in args and "--hard" in args:
                            child.send_signal(signal.SIGSTOP)
                            stopped = child
                            break
                    except psutil.NoSuchProcess:
                        pass
                if stopped is not None:
                    break
            time.sleep(0.001)
        assert stopped is not None, (
            "Did not observe the actual reset holding index.lock"
        )
        interrupted_at = time.monotonic()
        stdout, stderr = hook.communicate(timeout=12)
        assert time.monotonic() - interrupted_at < 11
        assert hook.returncode == 1, stderr
        record = parse_jsonl_output(stdout)
        assert record and record["status"] == "failed"
        assert "Timed out after 10 seconds" in record["output_str"]
        assert not stopped.is_running()
        assert not lock.exists(), "Our timeout left Git's index lock behind"
        # A subsequent ordinary capture must work without deleting any lock.
        result = subprocess.run(
            command,
            cwd=snap,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "succeeded"
        checkout = snap / "git"
        assert run_git(checkout, "rev-parse", "HEAD") == expected_commit
        assert (checkout / "39/3999.txt").read_bytes() == (
            source / "39/3999.txt"
        ).read_bytes()
        assert run_git(checkout / "nested", "rev-parse", "HEAD") == nested_commit
        assert (checkout / "nested/nested.txt").read_bytes() == (
            child_repo / "nested.txt"
        ).read_bytes()
        assert not lock.exists()
    finally:
        if hook is not None:
            stop_owned_process(hook)
        stop_owned_process(daemon)


def test_timeout_hard_kill_fallback_reaps_real_git_http_helper(tmp_path):
    port = unused_port()
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "http.server",
            str(port),
            "--bind",
            "127.0.0.1",
            "--directory",
            str(tmp_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    hook = None
    children = []
    try:
        wait_for_listener(port)
        server.send_signal(signal.SIGSTOP)

        # Block SIGINT across exec to exercise forced cleanup of an actual Git
        # HTTP child that cannot acknowledge the normal interrupt signal.
        def block_interrupt():
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})

        env = {
            **os.environ,
            "SNAP_DIR": str(tmp_path / "snapshot"),
            "CRAWL_DIR": str(tmp_path),
            "GIT_TIMEOUT": "10",
            "NO_PROXY": "127.0.0.1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        hook = subprocess.Popen(
            [str(HOOK), "--url", f"http://127.0.0.1:{port}/source.git"],
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            preexec_fn=block_interrupt,
        )
        owner = psutil.Process(hook.pid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and hook.poll() is None:
            children = owner.children(recursive=True)
            if any("remote-http" in " ".join(child.cmdline()) for child in children):
                break
            time.sleep(0.01)
        assert any("remote-http" in " ".join(child.cmdline()) for child in children)
        started = time.monotonic()
        stdout, stderr = hook.communicate(timeout=12)
        assert time.monotonic() - started < 11
        assert hook.returncode == 1, stderr
        record = parse_jsonl_output(stdout)
        assert record and record["status"] == "failed"
        assert "Timed out after 10 seconds" in record["output_str"]
        assert all(
            not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
            for child in children
        )
    finally:
        if hook is not None:
            stop_owned_process(hook)
        for child in children:
            if child.is_running():
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
        stop_owned_process(server)
