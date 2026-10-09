import base64
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests
import pytest

from abx_plugins.plugins.base.testing import install_required_binary_from_config


@pytest.fixture(scope="module")
def opencode_env(tmp_path_factory):
    plugin_dir = Path(__file__).parents[1]
    env = {
        **os.environ,
        "ABXPKG_LIB_DIR": str(tmp_path_factory.mktemp("opencode-lib")),
    }
    for name in ("node", "npm", "git", "browser-harness", "stagehand", "opencode"):
        binary = install_required_binary_from_config(plugin_dir, name, env=env)
        assert binary.abspath and binary.version, f"Failed to install {name}"
    return env


@pytest.fixture(scope="module")
def rg_binary(opencode_env):
    binary = install_required_binary_from_config(
        Path(__file__).parents[2] / "search_backend_ripgrep",
        "rg",
        env=opencode_env,
    )
    assert binary.abspath and binary.version, "Failed to install ripgrep"
    return str(binary.abspath)


def test_agent_child_python_uses_host_packages(tmp_path, opencode_env):
    """Browser dependencies must not replace the host CLI's Python libraries."""
    from abx_plugins.plugins.opencode import runtime

    settings = runtime._settings(
        {"ABXPKG_LIB_DIR": opencode_env["ABXPKG_LIB_DIR"]}, tmp_path
    )
    _, binary_env = runtime._resolve_binary("opencode", settings["config"])
    probe = [
        "uv",
        "run",
        "--no-project",
        "python",
        "-c",
        "import sys, pydantic; print(sys.prefix); print(pydantic.__file__)",
    ]
    host = subprocess.run(probe, capture_output=True, text=True, check=True)
    child = subprocess.run(
        probe,
        env=runtime.process_environment(settings, binary_env),
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout == host.stdout


def test_cold_agent_wrapper_defers_startup_until_its_frame_request(
    tmp_path,
    opencode_env,
):
    """The wrapper loads before the real server, while its frame gets a session."""
    from abx_plugins.plugins.opencode import runtime

    collection = tmp_path / "collection"
    collection.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = runtime._settings(
        {
            "DATA_DIR": str(collection),
            "OPENCODE_PORT": port,
            "ABXPKG_LIB_DIR": opencode_env["ABXPKG_LIB_DIR"],
        },
    )
    settings["archivebox_admin_url"] = "http://archivebox.localhost:5797/admin"
    server_key = (
        base64.urlsafe_b64encode(
            b"http://archivebox.localhost:5797/admin/agent/opencode",
        )
        .decode()
        .rstrip("=")
    )
    frame_url = f"{runtime._PROXY_PREFIX}/server/{server_key}/session"
    try:
        started = time.monotonic()
        context = runtime.agent_context(settings)
        assert time.monotonic() - started < 1
        assert context["proxy_url"] == frame_url
        assert not runtime._owned_process_running()

        path = frame_url.removeprefix(runtime._PROXY_PREFIX + "/")
        status, headers, body = runtime.proxy(settings, "GET", path, (), {}, b"")
        assert status == 302
        assert headers["Location"].startswith(frame_url + "/ses_")
        assert body == b""
        session_id = headers["Location"].rsplit("/", 1)[-1]
        sessions = requests.get(
            settings["origin"] + "/session",
            params={"directory": str(collection), "roots": "true", "limit": 55},
            timeout=settings["timeout"],
        )
        sessions.raise_for_status()
        assert any(
            item["id"] == session_id and item["directory"] == str(collection)
            for item in sessions.json()
        )
        again_status, again_headers, _ = runtime.proxy(
            settings,
            "GET",
            path,
            (),
            {},
            b"",
        )
        assert again_status == 302
        assert again_headers["Location"] == headers["Location"]
        status, _, html = runtime.proxy(
            settings,
            "GET",
            headers["Location"].removeprefix(runtime._PROXY_PREFIX + "/"),
            (),
            {},
            b"",
        )
        assert status == 200
        assert isinstance(html, bytes)
        assert b"<html" in html.lower()
        prompt = "- Help me create a custom importer.\n- First ask which site and items to collect; do not edit files yet."
        custom_session = runtime.start_session(
            settings,
            title="Create a custom importer",
            prompt=prompt,
        )
        assert custom_session != session_id
        custom_context = runtime.agent_context(settings, session_id=custom_session)
        assert custom_context["proxy_url"] == frame_url + "/" + custom_session
        assert custom_context["recent_session_id"] == custom_session
        deadline = time.monotonic() + 10
        while True:
            messages = requests.get(
                settings["origin"] + f"/session/{custom_session}/message",
                params={"directory": str(collection)},
                timeout=10,
            )
            messages.raise_for_status()
            if any(
                part.get("text") == prompt
                for message in messages.json()
                for part in message["parts"]
            ):
                break
            assert time.monotonic() < deadline, messages.text
            time.sleep(0.1)
    finally:
        runtime._stop_owned_process()


@pytest.mark.parametrize("collection_is_repo", [False, True])
def test_collection_is_not_a_git_project_or_file_index(
    tmp_path,
    collection_is_repo,
    opencode_env,
    rg_binary,
):
    """Exercise the real pinned OpenCode server, including its background indexer."""
    from abx_plugins.plugins.opencode import runtime

    collection = tmp_path / "collection"
    collection.mkdir()
    repo = collection if collection_is_repo else tmp_path
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "Collection isolation regression",
        ],
        check=True,
        capture_output=True,
    )
    payload = collection / "archive" / "snapshot" / "unique-snapshot-payload.txt"
    payload.parent.mkdir(parents=True)
    payload.write_text("archived content remains readable")
    subprocess.run(
        ["git", "init", str(payload.parent)],
        check=True,
        capture_output=True,
    )
    ignore = collection / ".ignore"
    ignore.write_text("# Administrator rules\n*.tmp\n")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = runtime._settings(
        {
            "DATA_DIR": str(collection),
            "OPENCODE_PORT": port,
            "ABXPKG_LIB_DIR": opencode_env["ABXPKG_LIB_DIR"],
        },
    )
    config = settings["config_home"] / "opencode" / "opencode.jsonc"
    config.parent.mkdir(parents=True)
    config.write_text('{"snapshot":true,"username":"collection-test"}\n')
    state_payload = settings["opencode_dir"] / "unique-state-payload.txt"
    state_payload.write_text("session state")
    try:
        ok, error = runtime._ensure_opencode(settings)
        assert ok, error

        def get(endpoint, directory=collection, **params):
            response = requests.get(
                settings["origin"] + endpoint,
                params={"directory": str(directory), **params},
                timeout=30,
            )
            response.raise_for_status()
            return response.json()

        for directory in (collection, payload.parent, settings["opencode_dir"]):
            project = get("/project/current", directory)
            assert project["id"] == "global", project
            assert not project.get("vcs"), project
        effective = get("/config")
        assert effective["snapshot"] is False
        assert effective["username"] == "collection-test"
        browser_skill = (
            settings["config_home"] / "opencode/skills/archivebox-browser/SKILL.md"
        )
        assert str(browser_skill) in effective["instructions"]
        assert browser_skill.is_file()
        # Verify the actual fallback indexer's traversal independently of its
        # asynchronous cache, which could otherwise be empty before indexing ends.
        indexed = subprocess.run(
            [rg_binary, "--no-config", "--files", "--glob=!**/.git/**", "."],
            cwd=collection,
            capture_output=True,
            text=True,
        )
        assert indexed.returncode == 1, indexed.stderr
        assert indexed.stdout == ""
        assert get("/find/file", query="unique-", limit=100) == []
        assert get("/file/status") == []
        content = get("/file/content", path=str(payload.relative_to(collection)))
        assert content["content"] == payload.read_text()
        assert json.loads(config.read_text())["snapshot"] is True
        assert ignore.read_text().startswith("# Administrator rules\n*.tmp\n")
        assert not (repo / ".git" / "opencode").exists()
        assert not (settings["data_home"] / "opencode" / "snapshot").exists()
    finally:
        runtime._stop_owned_process()


@pytest.fixture
def live_opencode(tmp_path, opencode_env):
    """A real plugin server without ArchiveBox collection or Django setup."""
    from abx_plugins.plugins.opencode import runtime

    collection = tmp_path / "collection"
    collection.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = runtime._settings(
        {
            "DATA_DIR": str(collection),
            "OPENCODE_PORT": port,
            "ABXPKG_LIB_DIR": opencode_env["ABXPKG_LIB_DIR"],
        },
    )
    settings["archivebox_admin_url"] = "http://archivebox.localhost:5797/admin"
    try:
        ok, error = runtime._ensure_opencode(settings)
        assert ok, error
        yield settings
    finally:
        runtime._stop_owned_process()


def test_opencode_oauth_callback_waits_for_user_and_preserves_cancellation(
    live_opencode,
):
    from abx_plugins.plugins.opencode import runtime

    settings: dict = {**live_opencode, "timeout": 1}
    headers = {"Content-Type": "application/json"}
    status, _, body = runtime.proxy(
        settings,
        "POST",
        "provider/openai/oauth/authorize",
        (),
        headers,
        b'{"method":0}',
    )
    assert status == 200
    assert isinstance(body, bytes)
    authorization = json.loads(body)
    assert urlsplit(authorization["url"]).hostname == "auth.openai.com"
    redirect = parse_qs(urlsplit(authorization["url"]).query)["redirect_uri"][0]
    callback_origin = urlsplit(redirect)
    assert callback_origin.hostname == "localhost"

    with ThreadPoolExecutor(max_workers=1) as executor:
        callback = executor.submit(
            runtime.proxy,
            settings,
            "POST",
            "provider/openai/oauth/callback",
            (),
            headers,
            b'{"method":0}',
        )
        try:
            # A real pending authorization must outlive the ordinary API read
            # timeout. No tokens or substituted provider responses are used.
            with pytest.raises(FutureTimeoutError):
                callback.result(timeout=2)
        finally:
            cancelled = requests.get(
                f"{callback_origin.scheme}://{callback_origin.netloc}/cancel",
                timeout=5,
            )
            assert cancelled.status_code == 200
            assert cancelled.text == "Login cancelled"
        status, response_headers, body = callback.result(timeout=5)
    assert status == 500
    assert response_headers["Content-Type"].startswith("application/json")
    assert isinstance(body, bytes)
    error = json.loads(body)
    assert error["name"] == "UnknownError"
    assert error["data"]["ref"].startswith("err_")
    runtime._stop_owned_process()
    # The pnpm launcher can exit before its server child. Stopping the owned
    # process must release both listeners so another collection can authorize.
    with pytest.raises(requests.ConnectionError):
        requests.get(settings["origin"] + "/global/health", timeout=2)
    with pytest.raises(requests.ConnectionError):
        requests.get(
            f"{callback_origin.scheme}://{callback_origin.netloc}/cancel",
            timeout=2,
        )


def test_concurrent_opencode_startup_waits_until_server_is_ready(live_opencode):
    from abx_plugins.plugins.opencode import runtime

    runtime._stop_owned_process()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(runtime._ensure_opencode, [live_opencode] * 2))

    assert results == [(True, ""), (True, "")]
    assert runtime._health(live_opencode)


def test_opencode_does_not_probe_or_replace_a_ready_owned_process(live_opencode):
    from abx_plugins.plugins.opencode import runtime

    process = runtime._PROCESS
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = {**live_opencode, "port": port}
    settings["origin"] = f"http://{settings['host']}:{settings['port']}"

    ok, error = runtime._ensure_opencode(settings)

    assert ok, error
    assert process is not None
    assert runtime._PROCESS is process
    assert process.poll() is None
