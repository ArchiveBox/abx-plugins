"""Real requests preserve native yt-dlp identity and other clients' shared identity."""

import io
import json
import os
import re
import subprocess
import wave
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import (
    install_required_binary_from_config,
    parse_jsonl_output,
)
from abx_plugins.plugins.favicon.tests.test_favicon import TEST_ICON_BYTES
from abx_plugins.plugins.ytdlp.tests.test_ytdlp import _build_test_wav_bytes

PLUGINS = Path(__file__).resolve().parents[1] / "abx_plugins/plugins"


@pytest.fixture(scope="module", params=["ytdlp", "gallerydl", "forumdl"])
def downloader_runtime(request, tmp_path_factory):
    plugin = request.param
    lib = tmp_path_factory.mktemp(f"{plugin}-identity-runtime")
    env = {**os.environ, "ABXPKG_LIB_DIR": str(lib)}
    names = {
        "ytdlp": (
            ("yt-dlp", "YTDLP_BINARY"),
            ("node", "NODE_BINARY"),
            ("ffmpeg", "FFMPEG_BINARY"),
        ),
        "gallerydl": (("gallery-dl", "GALLERYDL_BINARY"),),
        "forumdl": (("forum-dl", "FORUMDL_BINARY"),),
    }
    for name, key in names[plugin]:
        binary = install_required_binary_from_config(PLUGINS / plugin, name, env=env)
        assert binary and binary.abspath
        env[key] = str(binary.abspath)
    return plugin, env


@pytest.mark.parametrize("override", ["shared", "args", "extra"])
def test_downloader_sends_configured_user_agent(
    downloader_runtime,
    tmp_path,
    httpserver,
    override,
):
    plugin, runtime = downloader_runtime
    if plugin == "ytdlp":
        path, data, content_type = "/sample.wav", _build_test_wav_bytes(), "audio/wav"
    elif plugin == "gallerydl":
        path, data, content_type = (
            "/wikipedia/commons/a/a9/Example.png",
            TEST_ICON_BYTES,
            "image/png",
        )
    else:
        path, data, content_type = (
            "/article",
            b"<!doctype html><html><body>An ordinary article</body></html>",
            "text/html",
        )
    httpserver.expect_request(path).respond_with_data(data, content_type=content_type)
    if plugin == "forumdl":
        httpserver.expect_request(re.compile(".*")).respond_with_data(
            "Not Found",
            status=404,
        )
    env = {
        **runtime,
        "SNAP_DIR": str(tmp_path),
        "USER_AGENT": "ArchiveBox shared HTTP identity",
    }
    expected = env["USER_AGENT"]
    if plugin == "ytdlp" and override == "shared":
        # yt-dlp owns its native identity; a shared HTTP override must not force it.
        native = subprocess.run(
            [
                runtime["YTDLP_BINARY"],
                "--simulate",
                "--dump-single-json",
                httpserver.url_for(path),
            ],
            env=runtime,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert native.returncode == 0, native.stderr
        expected = json.loads(native.stdout)["http_headers"]["User-Agent"]
        assert expected and expected != env["USER_AGENT"]
    if override != "shared":
        expected = f"Explicit {override} identity"
        key = f"{plugin.upper()}_ARGS" + ("_EXTRA" if override == "extra" else "")
        # Use each executable's native header option after the shared default.
        args = (
            ["--add-headers", f"User-Agent:{expected}"]
            if plugin == "ytdlp" and override == "extra"
            else ["--user-agent", expected]
        )
        if override == "args":
            defaults = json.loads((PLUGINS / plugin / "config.json").read_text())[
                "properties"
            ][key]["default"]
            args = [*defaults, *args]
        env[key] = json.dumps(args)
    hook = next((PLUGINS / plugin).glob("on_Snapshot__*.py"))
    request_start = len(httpserver.log)
    result = subprocess.run(
        [str(hook), "--url", httpserver.url_for(path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == (
        "noresults" if plugin == "forumdl" else "succeeded"
    ), (result.stdout, result.stderr)
    if plugin == "ytdlp" and override == "shared":
        # yt-dlp deliberately randomizes Chrome's major on each process start.
        # Compare its native shape, then the exact identity saved by this hook.
        metadata = list((tmp_path / plugin).glob("*.info.json"))
        assert len(metadata) == 1
        actual_identity = json.loads(metadata[0].read_text())["http_headers"][
            "User-Agent"
        ]
        assert re.sub(r"Chrome/\d+", "Chrome/VERSION", actual_identity) == re.sub(
            r"Chrome/\d+",
            "Chrome/VERSION",
            expected,
        )
        assert actual_identity != env["USER_AGENT"]
        expected = actual_identity
    requests = httpserver.log[request_start:]
    assert requests
    assert all(
        request.headers["User-Agent"] == expected for request, _response in requests
    )
    if plugin == "ytdlp":
        # yt-dlp adds metadata; decoded media frames must remain unchanged.
        saved = list((tmp_path / plugin).glob("*.wav"))
        assert len(saved) == 1
        with (
            wave.open(str(saved[0]), "rb") as actual,
            wave.open(io.BytesIO(data), "rb") as original,
        ):
            assert actual.getparams() == original.getparams()
            assert actual.readframes(actual.getnframes()) == original.readframes(
                original.getnframes(),
            )
    elif plugin == "gallerydl":
        assert any(
            file.is_file() and file.read_bytes() == data
            for file in (tmp_path / plugin).rglob("*")
        )
    else:
        assert not (tmp_path / plugin / "forum.jsonl").exists()


def test_forum_declared_install_uses_package_root(tmp_path):
    # forum-dl pins an otherwise incompatible dependency set with --no-deps;
    # keep it in its own managed venv, like the existing papers-dl declaration.
    binary = install_required_binary_from_config(
        PLUGINS / "forumdl",
        "forum-dl",
        env={**os.environ, "ABXPKG_LIB_DIR": str(tmp_path)},
    )
    assert binary and binary.abspath
    assert (
        Path(binary.abspath).resolve().is_relative_to(tmp_path / "uv/packages/forum-dl")
    )
    result = subprocess.run(
        [str(binary.abspath), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "--user-agent" in result.stdout
