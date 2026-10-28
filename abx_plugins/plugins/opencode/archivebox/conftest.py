"""Fixtures for the host integration suite, imported by ArchiveBox conftest."""

import os
import re
from types import SimpleNamespace

import pytest
import requests


@pytest.fixture
def opencode_archive_config(initialized_archive):
    from archivebox.tests.conftest import get_free_port, _set_archivebox_config

    port = get_free_port()
    state_dir = initialized_archive / "opencode"
    env = os.environ.copy()
    env.update(
        {
            "ARCHIVEBOX_ALLOW_NO_UNIX_SOCKETS": "true",
            "OPENCODE_ENABLED": "True",
            "OPENCODE_HOST": "127.0.0.1",
            "OPENCODE_PORT": str(port),
            "OPENCODE_WORKDIR": str(initialized_archive),
            "OPENCODE_STATE_DIR": str(state_dir),
            "OPENCODE_TIMEOUT": "60",
        },
    )
    _set_archivebox_config(
        initialized_archive,
        "OPENCODE_ENABLED=True",
        "OPENCODE_HOST=127.0.0.1",
        f"OPENCODE_PORT={port}",
        f"OPENCODE_WORKDIR={initialized_archive}",
        f"OPENCODE_STATE_DIR={state_dir}",
        "OPENCODE_TIMEOUT=60",
        env=env,
    )
    return SimpleNamespace(
        data_dir=initialized_archive,
        port=port,
        state_dir=state_dir,
        env=env,
    )


@pytest.fixture
def installed_opencode(opencode_archive_config):
    from archivebox.tests.conftest import run_archivebox_cmd, _reset_runtime_config

    from abx_plugins.plugins.opencode import runtime
    from archivebox.config.common import get_config

    install = run_archivebox_cmd(
        ["install", "opencode"],
        cwd=opencode_archive_config.data_dir,
        env=opencode_archive_config.env,
        timeout=1200,
    )
    assert install.returncode == 0, install.stderr or install.stdout
    _reset_runtime_config()

    config = get_config().model_dump(mode="json")
    settings = runtime._settings(config, opencode_archive_config.data_dir)
    settings["archivebox_base_url"] = "http://admin.archivebox.localhost:5797"
    settings["archivebox_admin_url"] = "http://admin.archivebox.localhost:5797/admin"
    settings["archivebox_api_url"] = "http://admin.archivebox.localhost:5797/api/"
    binary, binary_env = runtime._resolve_binary(settings["binary"], settings["config"])
    version = binary.exec(
        cmd=("--version",),
        env={**os.environ, **binary_env},
        timeout=120,
    )
    assert version.returncode == 0, version.stderr or version.stdout
    return SimpleNamespace(config=opencode_archive_config, settings=settings)


@pytest.fixture
def live_opencode(installed_opencode):
    from abx_plugins.plugins.opencode import runtime

    settings = installed_opencode.settings
    ok, error = runtime._ensure_opencode(settings)
    assert ok, error

    process = runtime._PROCESS
    assert process is not None
    try:
        yield SimpleNamespace(
            config=installed_opencode.config,
            settings=settings,
            process=process,
        )
    finally:
        runtime._stop_owned_process()


@pytest.fixture
def agent_server(installed_opencode, browser_runtime, request):
    from archivebox.tests.conftest import (
        get_free_port,
        _set_archivebox_config,
        run_archivebox_cmd,
        start_archivebox_server,
        stop_archivebox_process,
    )

    port = get_free_port()
    mode, subdomains = getattr(request, "param", ("safe-onedomain-nojsreplay", False))
    base_url = (
        f"http://archivebox.localhost:{port}"
        if subdomains
        else f"http://localhost:{port}"
    )
    url = f"http://admin.archivebox.localhost:{port}" if subdomains else base_url
    config = installed_opencode.config
    _set_archivebox_config(
        config.data_dir,
        f"BASE_URL={base_url}",
        f"SERVER_SECURITY_MODE={mode}",
        # Each parallel fixture starts a complete server, including Sonic. A
        # shared default port caused its supervisor to respawn a failing worker
        # throughout browser startup. Isolate it like the HTTP/OpenCode ports.
        f"SEARCH_BACKEND_SONIC_PORT={get_free_port()}",
    )
    user = run_archivebox_cmd(
        [
            "shell",
            "-c",
            "from django.contrib.auth import get_user_model; User = get_user_model(); User.objects.create_superuser(username='agent-browser-test', password='test-password'); User.objects.create_user(username='agent-regular-test', password='test-password', is_staff=True)",
        ],
        cwd=config.data_dir,
        env=config.env,
    )
    assert user.returncode == 0, user.stderr or user.stdout
    process = start_archivebox_server(
        config.data_dir,
        port=port,
        env=config.env,
        log_name="agent-browser-server.log",
    )
    try:
        yield url, config.data_dir, process
    finally:
        if process.poll() is None:
            stop_archivebox_process(process)


def _login_cookie(server_url, username="agent-browser-test"):
    session = requests.Session()
    login_url = server_url + "/admin/login/"
    page = session.get(login_url, timeout=10)
    assert page.status_code == 200
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
    assert token is not None
    response = session.post(
        login_url,
        data={
            "username": username,
            "password": "test-password",
            "csrfmiddlewaretoken": token[1],
        },
        headers={"Referer": login_url},
        allow_redirects=False,
        timeout=10,
    )
    assert response.status_code == 302
    return "; ".join(f"{key}={value}" for key, value in session.cookies.items())
