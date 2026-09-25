import json
import os
import socket
import subprocess
import time
from pathlib import Path

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
    for name in ("node", "npm", "git", "opencode"):
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
    frame_url = runtime._project_route(collection)
    try:
        started = time.monotonic()
        context = runtime.agent_context(settings)
        assert time.monotonic() - started < 1
        assert context["proxy_url"] == frame_url
        assert not runtime._owned_process_running()

        path = frame_url.removeprefix(runtime._PROXY_PREFIX + "/")
        status, headers, body = runtime.proxy(settings, "GET", path, (), {}, b"")
        assert status == 302
        assert headers["Location"].startswith(frame_url + "/")
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
