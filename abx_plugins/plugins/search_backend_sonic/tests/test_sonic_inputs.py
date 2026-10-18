import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import time

import pytest

from abx_plugins.plugins.search_backend_sonic.daemon import (
    get_sonic_supervisord_worker,
    is_port_listening,
)
from abx_plugins.plugins.search_backend_sonic.search import flush, search


def write_discovery_files(snapshot: Path) -> dict[str, str]:
    content = {
        "article/text.txt": "plaintextuniqueneedle",
        "article/nested/readme.md": "markdownuniqueneedle",
        "article/nested/page.html": "<p>htmluniqueneedle</p>",
        "article/page.htm": "<p>htmuniqueneedle</p>",
        "article/.txt": "hiddenfilenameuniqueneedle",
    }
    for name, text in content.items():
        target = snapshot / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for directory in (
        "article/.hidden",
        "article/node_modules",
        "article/nested/.cache",
        "article/nested/tool.dist-info",
        "article/nested/tool.egg-info",
        "article/nested/site-packages",
        "article/nested/.venv",
        "article/nested/__pycache__",
        "search_backend_sqlite",
        "scratch_env",
    ):
        target = snapshot / directory / "deep/content.txt"
        target.parent.mkdir(parents=True)
        target.write_text("excludedruntimeuniqueneedle")
    # File aliases and top-level plugin aliases remain inputs, while nested
    # directory aliases/cycles are not recursively followed by Path.rglob.
    (snapshot / "article/alias.txt").symlink_to("text.txt")
    (snapshot / "article/nested/cycle").symlink_to("..", target_is_directory=True)
    (snapshot / "article/nested/alias").symlink_to("../..", target_is_directory=True)
    (snapshot / "article/broken.txt").symlink_to("missing")
    (snapshot / "article/empty.txt").touch()
    (snapshot / "article/blank.txt").write_text(" \n")
    (snapshot / "article/copied.meta.json").write_text("excludedruntimeuniqueneedle")
    (snapshot / "article_alias").symlink_to("article", target_is_directory=True)
    outside = snapshot.parent / f"{snapshot.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("excludedsecretuniqueneedle")
    (snapshot / "article/escape.txt").symlink_to(outside / "secret.txt")
    (snapshot / "outside_alias").symlink_to(outside, target_is_directory=True)
    private = snapshot / ".persona"
    private.mkdir()
    (private / "secret.txt").write_text("excludedsecretuniqueneedle")
    (snapshot / "article/private.txt").symlink_to(private / "secret.txt")
    (snapshot / "private_alias").symlink_to(private, target_is_directory=True)
    responses = snapshot / "responses"
    responses.mkdir()
    (responses / "body.txt").write_text("sharedresponseuniqueneedle")
    (snapshot / "article/response.txt").symlink_to("../responses/body.txt")
    expected = {
        name: text.replace("<p>", "").replace("</p>", "")
        for name, text in content.items()
    }
    expected["article/alias.txt"] = content["article/text.txt"]
    expected["article/response.txt"] = "sharedresponseuniqueneedle"
    expected.update(
        {
            name.replace("article/", "article_alias/", 1): text
            for name, text in list(expected.items())
        },
    )
    expected["responses/body.txt"] = "sharedresponseuniqueneedle"
    return expected


@pytest.mark.parametrize("backend,order", [("sonic", "91"), ("sqlite", "90")])
def test_discovery_walks_content_once_and_prunes_runtime_trees(
    tmp_path: Path,
    backend: str,
    order: str,
):
    expected = write_discovery_files(tmp_path)
    hook = (
        Path(__file__).parents[2]
        / f"search_backend_{backend}/on_Snapshot__{order}_index_{backend}.py"
    )
    # Observe actual filesystem traversal without replacing any filesystem API.
    probe = """
import json, os, runpy, sys
from pathlib import Path
hook = runpy.run_path(sys.argv[1])
scans = []
resolutions = []
file_stats = []
def audit(event, args):
    # Path.iterdir uses listdir on Python 3.12 and scandir on newer Python.
    if event in {'os.scandir', 'os.listdir'}:
        scans.append(os.fspath(args[0]))
sys.addaudithook(audit)
def profile(frame, event, arg):
    path = frame.f_locals.get('self')
    if isinstance(path, Path):
        if event == 'call' and frame.f_code.co_name == 'resolve':
            resolutions.append(str(path))
        if event == 'c_call' and (arg is os.lstat or (arg is os.stat and not frame.f_locals.get('follow_symlinks', True))):
            file_stats.append(str(path))
sys.setprofile(profile)
contents = hook['find_indexable_content']()
sys.setprofile(None)
print(json.dumps({'contents': contents, 'scans': scans, 'resolutions': resolutions, 'file_stats': file_stats}, default=str))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(hook)],
        cwd=tmp_path,
        env={**os.environ, "SNAP_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert {item[0]: item[1] for item in observed["contents"]} == expected
    assert len(observed["contents"]) == len(expected)
    # Ordinary files (including files reached through a validated root alias)
    # need one metadata read per visit, without resolving every parent again.
    regular = str(tmp_path / "article/text.txt")
    assert regular not in observed["resolutions"]
    assert observed["file_stats"].count(regular) == 2
    assert observed["file_stats"].count(str(tmp_path / "responses/body.txt")) == 1
    assert sorted(observed["scans"]) == sorted(
        str(tmp_path / name)
        for name in (
            ".",
            "article",
            "article/nested",
            "article_alias",
            "article_alias/nested",
            "responses",
        )
    )


def test_real_sonic_indexes_files_and_cli_id_not_reflection_context(tmp_path: Path):
    discovery_content = write_discovery_files(tmp_path)
    # The real Analog PDF plaintext contained 80,369 consecutive newlines.
    # A whitespace-only wire chunk is invalid in Sonic, but must not prevent
    # indexing the actual text on either side or change the saved source.
    spaced_text = "beforeblankuniqueneedle" + "\n" * 80369 + "afterblankuniqueneedle"
    spaced_file = tmp_path / "article/pdf-spacing.txt"
    spaced_file.write_text(spaced_text)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = {
        "DATA_DIR": str(tmp_path),
        "SNAP_DIR": str(tmp_path),
        "SEARCH_BACKEND_SONIC_ENABLED": "true",
        "SEARCH_BACKEND_SONIC_HOST_NAME": "127.0.0.1",
        "SEARCH_BACKEND_SONIC_PORT": str(port),
        "SEARCH_BACKEND_SONIC_PASSWORD": "test-input-contract",
        "SEARCH_BACKEND_SONIC_COLLECTION": "archivebox",
        "SEARCH_BACKEND_SONIC_BUCKET": "snapshots",
    }
    worker = get_sonic_supervisord_worker(config)
    assert worker is not None
    title = (
        "archived " * 20000
        + "metatitleuniqueneedle $(touch injected) `touch injected` "
        + chr(92)
        + '" afterescapeuniqueneedle'
    )
    manifest = (
        json.dumps(
            {
                "type": "Snapshot",
                "id": "explicit-sonic-id",
                "url": "https://example.com",
                "title": title,
                "tags": "metataguniqueneedle",
            },
        )
        + "\n"
    )
    (tmp_path / "index.jsonl").write_text(manifest)
    env = {
        **os.environ,
        **config,
        "EXTRA_CONTEXT": json.dumps(
            {
                "snapshot_id": "reflection-only",
                "snapshot_title": "forbiddencontextneedle",
                "snapshot_tags": "forbiddencontexttag",
                "trace_id": ["keep", 3],
            },
        ),
    }
    hook = Path(__file__).parents[1] / "on_Snapshot__91_index_sonic.py"
    with (tmp_path / "sonic-test.log").open("w") as log:
        daemon = subprocess.Popen(
            shlex.split(worker["command"]),
            cwd=worker["directory"],
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 10
            while not is_port_listening("127.0.0.1", port):
                assert daemon.poll() is None, (tmp_path / "sonic-test.log").read_text()
                assert time.monotonic() < deadline, "Sonic did not become ready"
                time.sleep(0.05)
            result = subprocess.run(
                [
                    str(hook),
                    "--url=https://example.com",
                    "--snapshot-id=explicit-sonic-id",
                ],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr
            record = json.loads(result.stdout.splitlines()[-1])
            assert record["status"] == "succeeded"
            assert record["snapshot_id"] == "reflection-only"
            assert record["trace_id"] == ["keep", 3]
            assert search("metatitleuniqueneedle", environ=env) == ["explicit-sonic-id"]
            assert search("afterescapeuniqueneedle", environ=env) == [
                "explicit-sonic-id",
            ]
            assert search("metataguniqueneedle", environ=env) == ["explicit-sonic-id"]
            assert search("forbiddencontextneedle", environ=env) == []
            assert search("forbiddencontexttag", environ=env) == []
            for term in set(discovery_content.values()):
                assert search(term, environ=env) == ["explicit-sonic-id"], term
            assert search("excludedruntimeuniqueneedle", environ=env) == []
            assert search("excludedsecretuniqueneedle", environ=env) == []
            assert (tmp_path / "index.jsonl").read_text() == manifest
            assert not list(tmp_path.rglob("injected"))
            for term in ("beforeblankuniqueneedle", "afterblankuniqueneedle"):
                assert search(term, environ=env) == ["explicit-sonic-id"]
            assert spaced_file.read_text() == spaced_text

            # Actual 2026-09-26 defuddle output from the failed
            # plaintextoffenders.com -> balboaferriswheel.com capture.
            # The old character-count chunks overflow Sonic's negotiated wire
            # buffer and silently lose the first part of this mostly Thai text.
            # A separate unique term checks that its tail remains searchable.
            fixture = (
                Path(__file__).parent
                / "fixtures/plaintextoffenders_defuddle_content.txt.gz"
            )
            captured_text = gzip.decompress(fixture.read_bytes())
            assert (
                hashlib.sha256(captured_text).hexdigest()
                == "f158646041729116873422eb771a3c100fc0083435d2f8a5719c1ef8c38519b8"
            )
            defuddle = tmp_path / "defuddle/content.txt"
            defuddle.parent.mkdir()
            defuddle.write_bytes(captured_text)
            tail_term = "ระยะเวลารับสิทธิ์"
            assert captured_text.decode("utf-8").find(tail_term) > 10000
            assert captured_text.decode("utf-8").count(tail_term) == 1
            metadata = json.loads(manifest)
            metadata["title"] = ""
            metadata["tags"] = ""
            (tmp_path / "index.jsonl").write_text(json.dumps(metadata) + "\n")
            result = subprocess.run(
                [
                    str(hook),
                    "--url=https://example.com",
                    "--snapshot-id=explicit-sonic-id",
                ],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout.splitlines()[-1])["status"] == "succeeded"
            assert search("Lotto889", environ=env) == ["explicit-sonic-id"]
            assert search(tail_term, environ=env) == ["explicit-sonic-id"]
            assert "buffer overflow" not in (tmp_path / "sonic-test.log").read_text()
            flush(["explicit-sonic-id"], environ=env)
            assert search("Lotto889", environ=env) == []
            assert search(tail_term, environ=env) == []
        finally:
            daemon.terminate()
            daemon.wait(timeout=10)


def test_unpacked_exports_index_text_and_ocr_end_to_end(tmp_path: Path):
    import zipfile
    from abx_plugins.plugins.liteparse.tests.test_liteparse import (
        IMAGE_URL_OCR,
        _download_png,
        _run_hook,
        require_tessdata_dir,
    )
    from abx_plugins.plugins.base.unpack_downloads import unpack_downloads

    require_tessdata_dir()
    snap = tmp_path / "snap"
    snap.mkdir()
    transport = tmp_path / "provider.zip"
    with zipfile.ZipFile(transport, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nested/note.txt", "exportplainuniqueneedle")
        archive.writestr("nested/readme.md", "# exportmarkdownuniqueneedle")
        archive.writestr("nested/page.html", "<p>exporthtmluniqueneedle</p>")
        archive.writestr("nested/eurotext.png", _download_png(IMAGE_URL_OCR))
    unpack_downloads(
        snap / "googledrive",
        {
            "title": "Export",
            "requireZip": True,
            "downloads": [
                {"filePath": str(transport), "suggestedFilename": "provider.zip"},
            ],
        },
    )
    assert not transport.exists()
    assert not list(snap.rglob("*.zip"))
    from abx_plugins.plugins.base.testing import install_required_binary_from_config

    ripgrep = install_required_binary_from_config(
        Path(__file__).parents[2] / "search_backend_ripgrep",
        "rg",
    )
    assert ripgrep and ripgrep.abspath
    rg = subprocess.run(
        [
            str(ripgrep.abspath),
            "-l",
            "exportplainuniqueneedle",
            str(snap / "googledrive/files"),
        ],
        capture_output=True,
        text=True,
    )
    assert rg.returncode == 0 and "nested/note.txt" in rg.stdout
    parsed = _run_hook(snap, IMAGE_URL_OCR)
    assert parsed.returncode == 0, parsed.stderr
    assert json.loads(parsed.stdout.splitlines()[-1])["status"] == "succeeded", (
        parsed.stderr
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = {
        "DATA_DIR": str(tmp_path),
        "SNAP_DIR": str(snap),
        "SEARCH_BACKEND_SONIC_ENABLED": "true",
        "SEARCH_BACKEND_SONIC_HOST_NAME": "127.0.0.1",
        "SEARCH_BACKEND_SONIC_PORT": str(port),
        "SEARCH_BACKEND_SONIC_PASSWORD": "test-unpacked-exports",
        "SEARCH_BACKEND_SONIC_COLLECTION": "archivebox",
        "SEARCH_BACKEND_SONIC_BUCKET": "snapshots",
    }
    worker = get_sonic_supervisord_worker(config)
    assert worker
    env = {**os.environ, **config}
    hook = Path(__file__).parents[1] / "on_Snapshot__91_index_sonic.py"
    with (tmp_path / "sonic.log").open("w") as log:
        daemon = subprocess.Popen(
            shlex.split(worker["command"]),
            cwd=worker["directory"],
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 10
            while not is_port_listening("127.0.0.1", port):
                assert daemon.poll() is None, (tmp_path / "sonic.log").read_text()
                assert time.monotonic() < deadline, (tmp_path / "sonic.log").read_text()
                time.sleep(0.05)
            indexed = subprocess.run(
                [
                    str(hook),
                    "--url=https://example.com/export",
                    "--snapshot-id=unpacked-export",
                ],
                cwd=snap,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert indexed.returncode == 0, indexed.stderr
            assert (
                json.loads(indexed.stdout.splitlines()[-1])["status"] == "succeeded"
            ), indexed.stderr
            for term in (
                "exportplainuniqueneedle",
                "exportmarkdownuniqueneedle",
                "exporthtmluniqueneedle",
                "quick",
                "brown",
                "fox",
            ):
                assert search(term, environ=env) == ["unpacked-export"], term
        finally:
            daemon.terminate()
            daemon.wait(timeout=10)
