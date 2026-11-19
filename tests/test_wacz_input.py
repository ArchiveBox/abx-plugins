"""Offline real WARC records and real extractor subprocesses."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
import pytest
from warcio.warcwriter import WARCWriter
from warcio.statusandheaders import StatusAndHeaders
from abx_plugins.plugins.base.wacz import extract, Unsupported


def fixture(path, rows):
    warc = io.BytesIO()
    writer = WARCWriter(warc, gzip=True)
    cdx = []
    for number, (url, status, mime, body, headers, kind) in enumerate(rows):
        start = warc.tell()
        record = writer.create_warc_record(
            url,
            kind,
            payload=io.BytesIO(body),
            http_headers=StatusAndHeaders(
                str(status) + " Fixture",
                [("Content-Type", mime), *headers],
                protocol="HTTP/1.1",
            ),
        )
        writer.write_record(record)
        length = warc.tell() - start
        chunk = warc.getvalue()[start : start + length]
        cdx.append(
            "test)/ %014d %s"
            % (
                20260101000000 + number,
                json.dumps(
                    dict(
                        url=url,
                        status=status,
                        mime=mime,
                        filename="data.warc.gz",
                        offset=start,
                        length=length,
                        recordDigest="sha256:" + hashlib.sha256(chunk).hexdigest(),
                    ),
                ),
            ),
        )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("archive/data.warc.gz", warc.getvalue())
        archive.writestr("indexes/index.cdx", "\n".join(cdx) + "\n")
        archive.writestr(
            "pages/pages.jsonl",
            "\n".join(
                json.dumps({"url": row[0], "ts": "2026-01-01T00:00:00Z"})
                for row in rows
            ),
        )
        archive.writestr(
            "datapackage.json",
            json.dumps(
                {
                    "profile": "data-package",
                    "resources": [
                        {"path": "archive/data.warc.gz"},
                        {"path": "indexes/index.cdx"},
                        {"path": "pages/pages.jsonl"},
                    ],
                },
            ),
        )


def test_exact_main_and_redirect(tmp_path):
    path = tmp_path / "capture.wacz"
    original = "https://example.test/start"
    final = "https://example.test/article"
    body = b"<html><body>Correct saved article</body></html>"
    fixture(
        path,
        [
            (
                "https://other.test/iframe",
                200,
                "text/html",
                b"Wrong frame",
                [],
                "response",
            ),
            (original, 302, "text/html", b"", [("Location", "/article")], "response"),
            (final, 200, "text/html", body, [], "response"),
        ],
    )
    assert extract(path, original) == body
    with pytest.raises(Unsupported):
        extract(path, "https://missing.test/")


@pytest.mark.parametrize(
    "status,mime,kind",
    [
        (404, "text/html", "response"),
        (200, "application/pdf", "response"),
        (200, "text/html", "revisit"),
    ],
)
def test_no_unrelated_or_unsuccessful_html(tmp_path, status, mime, kind):
    path = tmp_path / "capture.wacz"
    fixture(
        path,
        [("https://example.test/", status, mime, b"ordinary small fixture", [], kind)],
    )
    with pytest.raises(Unsupported):
        extract(path, "https://example.test/")


def test_canonical_fixture_and_real_htmltotext(tmp_path):
    import shutil

    root = Path(__file__).resolve().parents[1]
    snapshot = tmp_path / "snapshot"
    (snapshot / "archivewebpage").mkdir(parents=True)
    archive = snapshot / "archivewebpage/archivewebpage.wacz"
    shutil.copyfile(root / "tests/fixtures/replay/iframetest.wacz", archive)
    body = extract(archive, "http://localhost:20000/")
    assert b"Outside iframe" in body
    hook = root / "abx_plugins/plugins/htmltotext/on_Snapshot__66_htmltotext.py"
    result = subprocess.run(
        [sys.executable, str(hook), "--url=http://localhost:20000/"],
        cwd=snapshot,
        env={
            **os.environ,
            "SNAP_DIR": str(snapshot),
            "HTMLTOTEXT_ENABLED": "True",
            "PYTHONPATH": str(root),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    records = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    assert any(row.get("status") == "succeeded" for row in records), result.stdout
    assert "Outside iframe" in (snapshot / "htmltotext/htmltotext.txt").read_text()
    assert "Inside iframe" not in (snapshot / "htmltotext/htmltotext.txt").read_text()


def test_latest_capture_and_integrity(tmp_path):
    path = tmp_path / "capture.wacz"
    url = "https://example.test/"
    fixture(
        path,
        [
            (url, 200, "text/html", b"old", [], "response"),
            (url, 200, "text/html", b"latest", [], "response"),
        ],
    )
    assert extract(path, url) == b"latest"
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["archive/data.warc.gz"] = members["archive/data.warc.gz"][:-1] + b"x"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    with pytest.raises(Unsupported, match="digest"):
        extract(path, url)


def test_redirect_cycle(tmp_path):
    path = tmp_path / "cycle.wacz"
    url = "https://example.test/"
    fixture(path, [(url, 302, "text/html", b"", [("Location", url)], "response")])
    with pytest.raises(Unsupported, match="cycle"):
        extract(path, url)


def test_real_trafilatura_saved_main(tmp_path):
    import shutil

    root = Path(__file__).resolve().parents[1]
    snapshot = tmp_path / "snapshot"
    (snapshot / "archivewebpage").mkdir(parents=True)
    archive = snapshot / "archivewebpage/archivewebpage.wacz"
    shutil.copyfile(root / "tests/fixtures/replay/iframetest.wacz", archive)
    hook = root / "abx_plugins/plugins/trafilatura/on_Snapshot__66_trafilatura.py"
    result = subprocess.run(
        [sys.executable, str(hook), "--url=http://localhost:20000/"],
        cwd=snapshot,
        env={
            **os.environ,
            "SNAP_DIR": str(snapshot),
            "TRAFILATURA_ENABLED": "True",
            "PYTHONPATH": str(root),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    records = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    assert any(row.get("status") == "succeeded" for row in records), (
        result.stdout + result.stderr
    )
    assert "Outside iframe" in (snapshot / "trafilatura/content.txt").read_text()


def test_zip_metadata_read_bound(tmp_path):
    from abx_plugins.plugins.base.wacz import BoundedZipReader, MAX_COMPRESSED

    path = tmp_path / "small.bin"
    path.write_bytes(b"small ordinary file")
    with BoundedZipReader(path.open("rb", buffering=0)) as source:
        with pytest.raises(Unsupported, match="read cap"):
            source.read(MAX_COMPRESSED + 1)


def test_bad_index_range_and_post_are_unavailable(tmp_path):
    path = tmp_path / "capture.wacz"
    url = "https://example.test/"
    fixture(path, [(url, 200, "text/html", b"small article", [], "response")])
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    line = members["indexes/index.cdx"].decode()
    key, stamp, raw = line.split(" ", 2)
    row = json.loads(raw)
    for changes in ({"offset": -1}, {"method": "POST"}):
        members["indexes/index.cdx"] = (
            f"{key} {stamp} {json.dumps({**row, **changes})}\n".encode()
        )
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        with pytest.raises(Unsupported):
            extract(path, url)


def test_encoded_response_is_not_mistaken_for_html(tmp_path):
    path = tmp_path / "capture.wacz"
    url = "https://example.test/"
    fixture(
        path,
        [
            (
                url,
                200,
                "text/html",
                b"small encoded placeholder",
                [("Content-Encoding", "br")],
                "response",
            ),
        ],
    )
    with pytest.raises(Unsupported, match="encoded HTTP"):
        extract(path, url)


def test_concatenated_gzip_index(tmp_path):
    import gzip

    path = tmp_path / "capture.wacz"
    url = "https://example.test/"
    fixture(
        path,
        [
            ("https://example.test/iframe", 200, "text/html", b"wrong", [], "response"),
            (url, 200, "text/html", b"correct compressed-index main", [], "response"),
        ],
    )
    with zipfile.ZipFile(path) as archive:
        warc = archive.read("archive/data.warc.gz")
        lines = archive.read("indexes/index.cdx").splitlines(keepends=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("archive/data.warc.gz", warc)
        archive.writestr(
            "indexes/index.cdx.gz",
            b"".join(gzip.compress(line) for line in lines),
        )
    assert extract(path, url) == b"correct compressed-index main"


@pytest.mark.parametrize(
    "plugin,binary_name",
    [
        ("readability", "readability-extractor"),
        ("defuddle", "defuddle"),
        ("mercury", "mercury-parser"),
    ],
)
def test_real_browser_consumers_use_saved_wacz(
    tmp_path,
    plugin,
    binary_name,
    httpserver,
):
    from abx_plugins.plugins.base.testing import install_required_binary_from_config

    root = Path(__file__).resolve().parents[1]
    plugin_dir = root / "abx_plugins/plugins" / plugin
    binary = install_required_binary_from_config(plugin_dir, binary_name)
    assert binary and binary.abspath
    snapshot = tmp_path / "snapshot"
    (snapshot / "archivewebpage").mkdir(parents=True)
    text = "This saved offline article describes a practical system for preserving research documents and making their contents searchable without contacting the original publisher. "
    body = (
        "<html><head><title>Offline article</title></head><body><article><h1>Offline article</h1>"
        + "".join(f"<p>{text}</p>" for _ in range(12))
        + "</article></body></html>"
    ).encode()
    fixture(
        snapshot / "archivewebpage/archivewebpage.wacz",
        [("https://example.test/article", 200, "text/html", body, [], "response")],
    )
    hook = next(plugin_dir.glob("on_Snapshot__66_*.py"))
    result = subprocess.run(
        [sys.executable, str(hook), "--url=https://example.test/article"],
        cwd=snapshot,
        env={
            **os.environ,
            "SNAP_DIR": str(snapshot),
            f"{plugin.upper()}_ENABLED": "True",
            f"{plugin.upper()}_BINARY": str(binary.abspath),
            "PYTHONPATH": str(root),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    records = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    assert any(row.get("status") == "succeeded" for row in records), (
        result.stdout + result.stderr
    )
    assert not (snapshot / "dom").exists()
    assert any(
        "preserving research documents" in p.read_text()
        for p in (snapshot / plugin).glob("*.txt")
    )

    if plugin == "mercury":
        original = httpserver.url_for("/original")
        httpserver.expect_request("/original").respond_with_data(
            body,
            content_type="text/html",
        )
        for status, kind in ((404, "response"), (200, "revisit")):
            fixture(
                snapshot / "archivewebpage/archivewebpage.wacz",
                [
                    (
                        original,
                        status,
                        "text/html",
                        b"Archived unavailable page",
                        [],
                        kind,
                    ),
                ],
            )
            unavailable = subprocess.run(
                [sys.executable, str(hook), f"--url={original}"],
                cwd=snapshot,
                env={
                    **os.environ,
                    "SNAP_DIR": str(snapshot),
                    "MERCURY_ENABLED": "True",
                    "MERCURY_BINARY": str(binary.abspath),
                    "PYTHONPATH": str(root),
                },
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert unavailable.returncode == 0, unavailable.stderr
            assert '"status": "noresults"' in unavailable.stdout
            assert httpserver.log == []
