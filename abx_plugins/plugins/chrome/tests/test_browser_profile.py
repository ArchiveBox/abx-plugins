"""Run Chrome's declared profile command against real filesystem state."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


PLUGIN = Path(__file__).resolve().parent.parent


def test_recovery_stops_browser_before_hook_hydration(
    tmp_path,
    ensure_chrome_test_prereqs,
):
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
        find_chromium,
        get_test_env,
    )

    profile = tmp_path / "profile"
    binary = find_chromium()
    assert binary is not None, "The real Chrome test binary must be installed"
    # Launch the real browser without any hook/observer or archive sidecars.
    # Its native local profile lock must be sufficient to recover this orphan.
    with (tmp_path / "chrome.log").open("w+") as log:
        process = subprocess.Popen(
            [
                binary,
                "--headless",
                "--no-sandbox",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            cwd=tmp_path,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while (
                not (profile / "SingletonLock").is_symlink()
                and time.monotonic() < deadline
            ):
                assert process.poll() is None
                time.sleep(0.05)
            assert (
                int(os.readlink(profile / "SingletonLock").rsplit("-", 1)[1])
                == process.pid
            )
            env = get_test_env()
            for name in (
                "NODE_MODULES_DIR",
                "NODE_MODULE_DIR",
                "NODE_PATH",
                "PNPM_HOME",
                "PNPM_BIN_DIR",
            ):
                env.pop(name, None)
            result = subprocess.run(
                [str(PLUGIN / "browser.py"), "recover"],
                input=json.dumps({"paths": [str(tmp_path)]}),
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == {"processes_orphaned": 1}
            process.wait(timeout=10)
            assert not (tmp_path / "chrome").exists()
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def test_browser_profile_lock_identifies_live_browser(
    tmp_path,
    ensure_chrome_test_prereqs,
    httpserver,
):
    import psutil
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

    httpserver.expect_request("/").respond_with_data("<h1>Browser ownership</h1>")
    with chrome_session(tmp_path, test_url=httpserver.url_for("/"), navigate=False) as (
        launcher,
        pid,
        _,
        env,
    ):
        process = psutil.Process(pid)
        profile = Path(
            next(
                arg.split("=", 1)[1]
                for arg in process.cmdline()
                if arg.startswith("--user-data-dir=")
            ),
        )
        lock = profile / "SingletonLock"
        assert lock.is_symlink(), list(profile.iterdir())
        assert int(os.readlink(lock).rsplit("-", 1)[1]) == pid
        # The real tab daemon still owns this crawl browser after its launch
        # hook is killed, so collection recovery must leave it running.
        launcher.kill()
        launcher.wait(timeout=10)
        result = subprocess.run(
            [str(PLUGIN / "browser.py"), "recover"],
            input=json.dumps({"paths": [str(tmp_path)]}),
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"processes_orphaned": 0}
        assert process.is_running()


def test_recovery_requires_matching_local_crawl_runtime(
    tmp_path,
    ensure_chrome_test_prereqs,
):
    from contextlib import ExitStack

    import psutil

    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
        get_test_env,
        is_pid_alive,
        kill_chrome,
        launch_chromium_session,
    )

    root = tmp_path.resolve()
    scratch = root / "scratch"
    collection = root / "collection" / "archive" / "users" / "test-user" / "crawls"
    foreign = root / "foreign" / "archive" / "users" / "test-user" / "crawls"
    base_env = get_test_env()
    home = root / "home"
    home.mkdir()
    env = {**base_env, "HOME": str(home), "CHROME_HEADLESS": "true"}
    env.pop("CHROME_USER_DATA_DIR", None)

    def recover(expected):
        result = subprocess.run(
            [str(PLUGIN / "browser.py"), "recover"],
            input=json.dumps(
                {"paths": [str(collection)], "config": {"TMP_DIR": str(scratch)}},
            ),
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"processes_orphaned": expected}, (
            result.stderr
        )

    def launch(stack, archive_root, crawl_id):
        output = archive_root / "20260101" / "example.test" / crawl_id
        output.mkdir(parents=True)
        persona_dir = scratch / "personas" / crawl_id / ".persona"
        persona_dir.mkdir(parents=True)
        link = output / ".persona"
        link.symlink_to(persona_dir, target_is_directory=True)
        chrome_dir = output / "chrome"
        launcher, _ = launch_chromium_session(
            env={**env, "PERSONAS_DIR": str(persona_dir), "ACTIVE_PERSONA": "Default"},
            chrome_dir=chrome_dir,
            crawl_id=crawl_id,
        )
        pid = int((chrome_dir / "chrome.pid").read_text())

        def cleanup():
            if launcher.poll() is None:
                launcher.terminate()
                launcher.wait(timeout=15)
            if is_pid_alive(pid):
                kill_chrome(pid)
            launcher._stdout_handle.close()
            launcher._stderr_handle.close()

        stack.callback(cleanup)
        process = psutil.Process(pid)
        profile = persona_dir / "Default" / "chrome_profile"
        assert Path(process.cwd()) == chrome_dir
        assert f"--user-data-dir={profile}" in process.cmdline()
        assert int(os.readlink(profile / "SingletonLock").rsplit("-", 1)[1]) == pid
        return launcher, pid, link, persona_dir

    with ExitStack() as stack:
        owned, owned_pid, owned_link, owned_persona = launch(
            stack,
            collection,
            "1" * 32,
        )
        other, other_pid, _, other_persona = launch(stack, foreign, "2" * 32)
        # Both browsers have real live owners.
        recover(0)
        assert is_pid_alive(owned_pid) and is_pid_alive(other_pid)

        for launcher in (owned, other):
            launcher.kill()
            launcher.wait(timeout=10)
        assert is_pid_alive(owned_pid) and is_pid_alive(other_pid)

        # Neither a mismatched crawl link nor a foreign collection authorizes termination.
        owned_link.unlink()
        owned_link.symlink_to(other_persona, target_is_directory=True)
        recover(0)
        assert is_pid_alive(owned_pid) and is_pid_alive(other_pid)

        owned_link.unlink()
        owned_link.symlink_to(owned_persona, target_is_directory=True)
        profile = owned_persona / "Default" / "chrome_profile"
        displaced_profile = root / "outside-scratch"
        profile.rename(displaced_profile)
        profile.symlink_to(displaced_profile, target_is_directory=True)
        try:
            recover(0)  # The exact runtime path cannot contain a profile redirect.
            assert is_pid_alive(owned_pid) and is_pid_alive(other_pid)
        finally:
            profile.unlink()
            displaced_profile.rename(profile)

        recover(1)
        assert not is_pid_alive(owned_pid) and is_pid_alive(other_pid)


def test_collection_recovery_does_not_scan_saved_outputs(tmp_path):
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import get_test_env

    # Use an actual exited process identity; these files are archived content,
    # not runtime bookkeeping, even though their names match Chrome sidecars.
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    assert process.wait() == 0
    saved = tmp_path / "crawl/snapshots/snapshot/wget/site/chrome"
    saved.mkdir(parents=True)
    pid_file = saved / "on_Snapshot__00_chrome_launch.daemon.bg.js.pid"
    pid_file.write_text(str(process.pid))
    persona = saved.parent / ".persona"
    persona.mkdir()
    (persona / "auth.json").write_text("{}")
    result = subprocess.run(
        [str(PLUGIN / "browser.py"), "recover"],
        input=json.dumps({"paths": [str(tmp_path)]}),
        cwd=tmp_path,
        env=get_test_env(),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"processes_orphaned": 0}
    assert "Checking for zombie Chrome processes" not in result.stderr
    assert pid_file.read_text() == str(process.pid)
    assert (persona / "auth.json").read_text() == "{}"


def test_recovery_without_browsers_needs_no_node_environment(tmp_path):
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import get_test_env

    env = get_test_env()
    for name in (
        "NODE_MODULES_DIR",
        "NODE_MODULE_DIR",
        "NODE_PATH",
        "PNPM_HOME",
        "PNPM_BIN_DIR",
    ):
        env.pop(name, None)
    env["ABXPKG_LIB_DIR"] = str(tmp_path / "lib")
    env["PATH"] = os.pathsep.join(
        entry
        for entry in env["PATH"].split(os.pathsep)
        if Path(entry).parent.name != "node_modules"
    )
    command = json.loads((PLUGIN / "config.json").read_text())["commands"]["recover"]
    result = subprocess.run(
        [str(PLUGIN / command[0]), *command[1:]],
        # A bad Node provider must not matter when there is no orphan. Put it
        # in the command request so the Python script launcher still works.
        input=json.dumps(
            {
                "paths": [str(tmp_path)],
                "config": {"ABXPKG_BINPROVIDERS": "unsupported-recovery-provider"},
            },
        ),
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"processes_orphaned": 0}
    assert "Checking for zombie Chrome processes" not in result.stderr


def run_profile(**request):
    command = json.loads((PLUGIN / "config.json").read_text())["commands"][
        "browser-profile"
    ]
    return subprocess.run(
        [sys.executable, str(PLUGIN / command[0]), *command[1:]],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_profile_copy_preserves_source_and_session_state(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    profile = source / "Default"
    profile.mkdir(parents=True)
    (profile / "Preferences").write_text('{"extensions":{"settings":{}}}')
    (profile / "Sessions").mkdir()
    (profile / "Sessions" / "Session_1").write_bytes(b"saved session")
    (profile / "Cache").mkdir()
    (profile / "Cache" / "cached").write_text("cache")
    (source / "SingletonLock").symlink_to("missing-lock")
    (source / "chrome.log").write_text("log")
    (source / "session-link").symlink_to("Default/Sessions/Session_1")
    destination.mkdir()
    (destination / "stale").write_text("old")
    result = run_profile(
        action="copy",
        source_dir=str(source),
        destination_dir=str(destination),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"path": str(destination)}
    assert (destination / "Default/Sessions/Session_1").read_bytes() == b"saved session"
    assert (destination / "session-link").is_symlink()
    assert (destination / "session-link").read_bytes() == b"saved session"
    assert not (destination / "SingletonLock").is_symlink()
    assert not (destination / "Default/Cache").exists()
    assert not (destination / "chrome.log").exists()
    assert not (destination / "stale").exists()
    assert (source / "SingletonLock").is_symlink()
    assert (source / "Default/Cache/cached").read_text() == "cache"
    assert (source / "chrome.log").read_text() == "log"


def test_profile_import_normalizes_only_selected_profile(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    for name in ("Default", "Profile 2"):
        (source / name).mkdir(parents=True)
        (source / name / "Preferences").write_text(name)
    state = {
        "profile": {
            "last_used": "Profile 2",
            "info_cache": {
                "Default": {"name": "Other"},
                "Profile 2": {"name": "Selected"},
            },
        },
    }
    (source / "Local State").write_text(json.dumps(state))
    result = run_profile(
        action="import",
        source_dir=str(source),
        destination_dir=str(destination),
        profile_dir="Profile 2",
    )
    assert result.returncode == 0, result.stderr
    assert (destination / "Default/Preferences").read_text() == "Profile 2"
    assert not (destination / "Profile 2").exists()
    assert json.loads((destination / "Local State").read_text())["profile"] == {
        "last_used": "Default",
        "last_active_profiles": ["Default"],
        "info_cache": {"Default": {"name": "Selected"}},
    }
    assert json.loads((source / "Local State").read_text()) == state


def test_profile_prepare_preserves_existing_persona_files(tmp_path):
    persona = tmp_path / "persona"
    persona.mkdir()
    (persona / "auth.json").write_text('{"cookies":[]}')
    result = run_profile(
        action="prepare",
        persona_dir=str(persona),
        name="Personal",
        browser_binary="/browser",
    )
    assert result.returncode == 0, result.stderr
    assert (persona / "chrome_profile").is_dir()
    assert (persona / "chrome_downloads").is_dir()
    assert (persona / "persona_name.txt").read_text() == "Personal"
    assert (persona / "chrome_binary.txt").read_text() == "/browser"
    assert (persona / "auth.json").read_text() == '{"cookies":[]}'


@pytest.mark.parametrize("action", ["copy", "import", "prepare"])
@pytest.mark.parametrize("relationship", ["equal", "parent", "child", "symlink"])
def test_profile_copy_rejects_overlaps_before_mutation(tmp_path, action, relationship):
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep").write_text("source")
    (source / "Default").mkdir()
    destination = {
        "equal": source,
        "parent": tmp_path,
        "child": source / "child",
        "symlink": tmp_path / "link",
    }[relationship]
    if relationship == "symlink":
        destination.symlink_to(source, target_is_directory=True)
    result = run_profile(
        action=action,
        source_dir=str(source),
        destination_dir=str(destination),
        persona_dir=str(destination),
        profile_dir="Default",
    )
    assert result.returncode != 0
    assert "overlap" in result.stderr.lower()
    assert (source / "keep").read_text() == "source"


@pytest.mark.parametrize("profile_dir", ["Missing", "../outside", "/outside"])
def test_profile_import_rejects_invalid_source_before_mutation(tmp_path, profile_dir):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (destination / "keep").write_text("destination")
    result = run_profile(
        action="import",
        source_dir=str(source),
        destination_dir=str(destination),
        profile_dir=profile_dir,
    )
    assert result.returncode != 0
    assert (destination / "keep").read_text() == "destination"


def test_profile_import_rejects_invalid_metadata_before_mutation(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    (source / "Default").mkdir(parents=True)
    (source / "Local State").write_text("invalid JSON")
    destination.mkdir()
    (destination / "keep").write_text("destination")
    result = run_profile(
        action="import",
        source_dir=str(source),
        destination_dir=str(destination),
        profile_dir="Default",
    )
    assert result.returncode != 0
    assert (destination / "keep").read_text() == "destination"
