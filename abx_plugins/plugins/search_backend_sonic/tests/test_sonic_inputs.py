import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import time

from abx_plugins.plugins.search_backend_sonic.daemon import (
    get_sonic_supervisord_worker,
    is_port_listening,
)
from abx_plugins.plugins.search_backend_sonic.search import search


def test_real_sonic_indexes_files_and_cli_id_not_reflection_context(tmp_path: Path):
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
            assert (tmp_path / "index.jsonl").read_text() == manifest
            assert not list(tmp_path.rglob("injected"))

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
