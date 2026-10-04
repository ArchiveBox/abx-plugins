"""Real provider ZIPs publish a raw tree without retaining transport copies."""

import io
import json
import stat
import zipfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.unpack_downloads import unpack_downloads


def request_for(source):
    return {
        "title": "Folder",
        "requireZip": True,
        "downloads": [{"filePath": str(source), "suggestedFilename": "folder.zip"}],
    }


def test_unpacked_files_preserve_nested_archives_and_remove_transport(tmp_path):
    output = tmp_path / "googledrive"
    output.mkdir()
    legacy = output / "download-1.zip"
    legacy.write_bytes(b"old transport")
    (output / "downloads.json").write_text(
        json.dumps({"downloads": [{"path": legacy.name}]}),
    )
    source = tmp_path / "browser.zip"
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("inside.txt", "preserved inner archive")
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("empty/", "")
        archive.writestr("nested/actual.zip", inner.getvalue())
        archive.writestr("nested/note.txt", "raw searchable text")
    manifest = unpack_downloads(output, request_for(source))
    assert not source.exists() and not legacy.exists()
    assert (output / "files/empty").is_dir()
    assert (output / "files/nested/note.txt").read_text() == "raw searchable text"
    assert (output / "files/nested/actual.zip").read_bytes() == inner.getvalue()
    assert len(manifest["files"]) == 2
    assert not list(output.glob(".unpack-*"))


@pytest.mark.parametrize(
    "bad_entry",
    ["../escape.txt", "/absolute.txt", "C:/windows.txt", "link", "bad-crc"],
)
def test_invalid_archive_preserves_previous_files(tmp_path: Path, bad_entry):
    output = tmp_path / "dropbox"
    (output / "files").mkdir(parents=True)
    (output / "files/previous.txt").write_text("keep previous output")
    old_manifest = '{"files": []}'
    (output / "downloads.json").write_text(old_manifest)
    source = tmp_path / "browser.zip"
    with zipfile.ZipFile(source, "w") as archive:
        if bad_entry == "link":
            entry = zipfile.ZipInfo("link")
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, "../escape.txt")
        else:
            archive.writestr(bad_entry, b"unique payload for CRC")
    if bad_entry == "bad-crc":
        source.write_bytes(
            source.read_bytes().replace(
                b"unique payload for CRC",
                b"broken payload for CRC",
            ),
        )
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        unpack_downloads(output, request_for(source))
    assert (output / "files/previous.txt").read_text() == "keep previous output"
    assert (output / "downloads.json").read_text() == old_manifest
    assert source.exists()
    assert not (tmp_path / "escape.txt").exists()
    assert not list(output.glob(".unpack-*"))


def test_shared_zip_file_is_preserved(tmp_path):
    source = tmp_path / "browser.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("note.txt", "original archive")
    original = source.read_bytes()
    request = request_for(source)
    request["requireZip"] = False
    output = tmp_path / "dropbox"
    manifest = unpack_downloads(output, request)
    assert len(manifest["files"]) == 1
    assert (output / "files/folder.zip").read_bytes() == original
    assert not source.exists()


def test_duplicate_member_names_preserve_every_file(tmp_path):
    source = tmp_path / "browser.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("note.txt", "first")
        archive.writestr("note (2).txt", "existing suffix")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("note.txt", "second")
    output = tmp_path / "googledrive"
    manifest = unpack_downloads(output, request_for(source))
    assert len(manifest["files"]) == 3
    assert {p.read_text() for p in (output / "files").iterdir()} == {
        "first",
        "second",
        "existing suffix",
    }


@pytest.mark.parametrize(
    "old",
    [
        {"downloads": None},
        {"downloads": [{"path": 3}]},
        {"downloads": [{"path": "download-1.zip"}]},
    ],
)
def test_malformed_old_manifest_cannot_break_publication(tmp_path, old):
    output = tmp_path / "googledrive"
    output.mkdir()
    (output / "downloads.json").write_text(json.dumps(old))
    (output / "download-1.zip").mkdir()
    source = tmp_path / "browser.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("note.txt", "saved")
    manifest = unpack_downloads(output, request_for(source))
    assert json.loads((output / "downloads.json").read_text()) == manifest
    assert (output / "files/note.txt").read_text() == "saved"
    assert not source.exists()
