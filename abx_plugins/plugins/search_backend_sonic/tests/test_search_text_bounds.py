"""Real file and hook coverage for bounded, cleaned search text."""

import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


PLUGINS = Path(__file__).parents[2]
BACKENDS = [("sonic", "91"), ("sqlite", "90")]


def write_source(snapshot: Path, name: str, text: str) -> Path:
    path = snapshot / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def source_hashes(snapshot: Path) -> dict[str, str]:
    return {
        str(path.relative_to(snapshot)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in snapshot.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def discover(snapshot: Path, backend: str, order: str) -> dict[str, str]:
    hook = PLUGINS / f"search_backend_{backend}/on_Snapshot__{order}_index_{backend}.py"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,runpy,sys; h=runpy.run_path(sys.argv[1]); "
            "print(json.dumps({x[0]:x[1] for x in h['find_indexable_content']()}))",
            str(hook),
        ],
        cwd=snapshot,
        env={**os.environ, "SNAP_DIR": str(snapshot)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("backend,order", BACKENDS)
def test_primary_text_keeps_long_ocr_tail_and_cleans_all_document_formats(
    tmp_path: Path,
    backend: str,
    order: str,
):
    image_bytes = (PLUGINS / "liteparse/tests/fixtures/ocr.png").read_bytes()
    image_data = base64.b64encode(image_bytes).decode("ascii")
    blob = "Q29udGVudElzTm90QW5JbWFnZQ" * 400
    text = "Useful extracted words " * 17000 + " uniqueliteparsetail"
    assert len(text.encode()) > 256 * 1024
    write_source(tmp_path, "liteparse/document.pdf.txt", text)
    write_source(tmp_path, "ocr/image.txt", "ocrneedle " + blob + " afterocrneedle")
    write_source(
        tmp_path,
        "notes/document.md",
        "markdownneedle ![diagram](data:image/png;base64,"
        + image_data
        + ") aftermarkdownneedle",
    )
    for plugin in ("readability", "defuddle", "mercury", "trafilatura"):
        write_source(
            tmp_path,
            f"{plugin}/content.html",
            f"<p>{plugin}needle</p><img src='data:image/png;base64,{image_data}'><p>after{plugin}needle {blob}</p>",
        )
    from abx_plugins.plugins.base.unpack_downloads import unpack_downloads

    transport = tmp_path.parent / f"{tmp_path.name}-export.html"
    transport.write_text(
        f"<p>providerexportneedle {blob}</p><p>afterproviderneedle</p>",
    )
    unpack_downloads(
        tmp_path / "notion",
        {
            "title": "Native export",
            "downloads": [
                {"filePath": str(transport), "suggestedFilename": "document.html"},
            ],
        },
    )
    image = tmp_path / "screenshot/image.png"
    image.parent.mkdir()
    image.write_bytes(image_bytes)
    write_source(
        tmp_path,
        "singlefile/singlefile.html",
        "<p>excludedfallbackneedle</p>",
    )
    before = source_hashes(tmp_path)
    content = discover(tmp_path, backend, order)
    all_text = "\n".join(content.values())
    for needle in (
        "uniqueliteparsetail",
        "ocrneedle",
        "afterocrneedle",
        "markdownneedle",
        "aftermarkdownneedle",
        "providerexportneedle",
        "afterproviderneedle",
    ):
        assert needle in all_text, needle
    for plugin in ("readability", "defuddle", "mercury", "trafilatura"):
        assert f"after{plugin}needle" in all_text
    assert "data:" not in all_text
    assert blob[:100] not in all_text
    assert image_data[:100] not in all_text
    assert not any(name.startswith("screenshot/") for name in content)
    assert "excludedfallbackneedle" not in all_text
    assert source_hashes(tmp_path) == before


@pytest.mark.parametrize("backend,order", BACKENDS)
def test_primary_text_uses_five_mib_cleaned_byte_budget(
    tmp_path: Path,
    backend: str,
    order: str,
):
    for index in range(3):
        write_source(
            tmp_path,
            f"liteparse/{index}.txt",
            "article résumé text " * 110000,
        )
    before = source_hashes(tmp_path)
    content = discover(tmp_path, backend, order)
    size = sum(len(text.encode()) for text in content.values())
    assert all("résumé" in text for text in content.values())
    assert 5 * 1024**2 - 100 <= size <= 5 * 1024**2
    assert source_hashes(tmp_path) == before


@pytest.mark.parametrize("backend,order", BACKENDS)
def test_fallback_threshold_counts_cleaned_primary_and_has_separate_budget(
    tmp_path: Path,
    backend: str,
    order: str,
):
    # Transport media dwarfs the threshold, but the useful extracted text does not.
    blob = "QW5FbWJlZGRlZE1lZGlhUGF5bG9hZA" * 1000
    primary = "primary " * 1100
    assert len(primary.encode()) < 10 * 1024
    write_source(tmp_path, "ocr/output.txt", primary + " data:image/png;base64," + blob)
    for index, plugin in enumerate(("dom", "singlefile", "responses", "wget")):
        write_source(
            tmp_path,
            f"{plugin}/{index}.html",
            "<p>fallback " + "useful raw text " * 5000 + "</p>",
        )
    oversized = ("excludedoversizedneedle " + "useful text " * 10000)[
        : 100 * 1024 - 1
    ] + "."
    write_source(tmp_path, "wget/boundary.html", "<p>" + oversized + "</p>")
    before = source_hashes(tmp_path)
    content = discover(tmp_path, backend, order)
    assert "ocr/output.txt" in content
    fallback = [text for name, text in content.items() if name != "ocr/output.txt"]
    assert fallback
    fallback_size = sum(len(text.encode()) for text in fallback)
    assert 256 * 1024 - 100 <= fallback_size <= 256 * 1024
    assert sum(len(text.encode()) for text in content.values()) > 256 * 1024
    assert "excludedoversizedneedle" not in "\n".join(fallback)
    assert "data:" not in "\n".join(content.values())
    assert blob[:100] not in "\n".join(content.values())
    assert source_hashes(tmp_path) == before


@pytest.mark.parametrize("backend,order", BACKENDS)
def test_fallback_file_size_is_checked_after_embedded_media_removal(
    tmp_path: Path,
    backend: str,
    order: str,
):
    image_data = base64.b64encode(
        (PLUGINS / "liteparse/tests/fixtures/ocr.png").read_bytes(),
    ).decode("ascii")
    html = (
        "<p>fallbackimagenearstart</p>"
        + f'<img src="data:image/png;base64,{image_data}">' * 2
        + "<p>fallbackimagenearend</p>"
    )
    assert len(html.encode()) > 100 * 1024
    write_source(tmp_path, "singlefile/embedded.html", html)
    below = ("fallbackbelowlimit " + "useful article text " * 6000)[
        : 100 * 1024 - 9
    ] + "."
    boundary = ("excludedboundaryneedle " + "useful article text " * 6000)[
        : 100 * 1024 - 8
    ] + "."
    assert len(("<p>" + below + "</p>").encode()) == 100 * 1024 - 1
    assert len(("<p>" + boundary + "</p>").encode()) == 100 * 1024
    write_source(tmp_path, "wget/below.html", "<p>" + below + "</p>")
    write_source(tmp_path, "wget/boundary.html", "<p>" + boundary + "</p>")
    before = source_hashes(tmp_path)
    content = discover(tmp_path, backend, order)
    assert (
        content["singlefile/embedded.html"]
        == "fallbackimagenearstart fallbackimagenearend"
    )
    assert "fallbackbelowlimit" in content["wget/below.html"]
    assert "wget/boundary.html" not in content
    assert "data:" not in "\n".join(content.values())
    assert image_data[:100] not in "\n".join(content.values())
    assert source_hashes(tmp_path) == before


@pytest.mark.parametrize("backend,order", BACKENDS)
def test_sufficient_cleaned_primary_excludes_raw_html(
    tmp_path: Path,
    backend: str,
    order: str,
):
    write_source(tmp_path, "zz_text/content.txt", "primary " * 1279 + "primary!")
    write_source(tmp_path, "dom/output.html", "<p>excludedrawneedle</p>")
    content = discover(tmp_path, backend, order)
    assert sum(len(text.encode()) for text in content.values()) >= 10 * 1024
    assert set(content) == {"zz_text/content.txt"}


def test_sqlite_hook_stores_clean_text_and_searches_ocr_tail(tmp_path: Path):
    blob = "QmFzZTY0RW5jb2RlZEJsb2I" * 1000
    text = "useful OCR words " * 20000 + " retainedocrtail"
    write_source(
        tmp_path,
        "liteparse/page.txt",
        text + " data:image/png;base64," + blob,
    )
    title = write_source(tmp_path, "title/title.txt", "retainedtitleneedle " + blob)
    before = source_hashes(tmp_path)
    hook = PLUGINS / "search_backend_sqlite/on_Snapshot__90_index_sqlite.py"
    result = subprocess.run(
        [str(hook), "--url=https://example.com/document", "--snapshot-id=cleaned-text"],
        cwd=tmp_path,
        env={
            **os.environ,
            "SNAP_DIR": str(tmp_path),
            "DATA_DIR": str(tmp_path),
            "SEARCH_BACKEND_SQLITE_ENABLED": "true",
            "SEARCH_BACKEND_SQLITE_DB": "bounded.sqlite3",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["status"] == "succeeded"
    with sqlite3.connect(tmp_path / "bounded.sqlite3") as db:
        rows = db.execute(
            "SELECT snapshot_id FROM search_index WHERE search_index MATCH ?",
            ("retainedocrtail",),
        ).fetchall()
        saved_title, saved_content = db.execute(
            "SELECT title,content FROM search_index WHERE snapshot_id=?",
            ("cleaned-text",),
        ).fetchone()
    assert rows == [("cleaned-text",)]
    assert "retainedtitleneedle" in saved_title
    assert blob[:100] not in saved_title + saved_content
    assert "data:" not in saved_content
    for name, digest in before.items():
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest
    assert title.read_text().endswith(blob)


def test_sonic_manifest_metadata_is_cleaned_without_rewriting_it(tmp_path: Path):
    blob = "QW5FbWJlZGRlZE1lZGlhUGF5bG9hZA" * 500
    manifest = write_source(
        tmp_path,
        "index.jsonl",
        json.dumps(
            {
                "type": "Snapshot",
                "id": "metadata-test",
                "title": f"metadatatitleneedle {blob}",
                "tags": [f"metadatatagneedle data:image/png;base64,{blob}"],
            },
        )
        + "\n",
    )
    before = manifest.read_bytes()
    hook = PLUGINS / "search_backend_sonic/on_Snapshot__91_index_sonic.py"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,runpy,sys; h=runpy.run_path(sys.argv[1]); print(json.dumps(h['build_metadata_content']('https://example.com', 'metadata-test')))",
            str(hook),
        ],
        cwd=tmp_path,
        env={**os.environ, "SNAP_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    content = json.loads(result.stdout)
    assert "metadatatitleneedle" in content
    assert "metadatatagneedle" in content
    assert "data:" not in content
    assert blob[:100] not in content
    assert manifest.read_bytes() == before
