#!/usr/bin/env -S abxpkg run --script python3
# /// script
# requires-python = ">=3.12"
# ///
"""Publish provider downloads as ordinary files; ZIP is temporary transport only."""

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


def unpack_downloads(output: Path, request: dict) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    manifest = {"title": request["title"], "files": []}
    previous = output / "downloads.json"
    try:
        old_manifest = json.loads(previous.read_text()) if previous.is_file() else {}
    except (ValueError, OSError):
        old_manifest = {}
    if not isinstance(old_manifest, dict):
        old_manifest = {}
    with tempfile.TemporaryDirectory(prefix=".unpack-", dir=output) as temporary:
        staging = Path(temporary) / "files"
        staging.mkdir()

        def destination_for(name: str) -> Path:
            # Preserve filenames without ever trusting archive paths or symlinks.
            name = name.replace("\\", "/")
            parts = PurePosixPath(name).parts
            if (
                not parts
                or name.startswith("/")
                or re.match(r"^[A-Za-z]:", name)
                or ".." in parts
                or "\0" in name
            ):
                raise ValueError(f"Unsafe archive filename: {name!r}")
            destination = staging.joinpath(*parts)
            return destination

        def save(source, name: str) -> None:
            destination = destination_for(name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with destination.open("xb") as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            relative = destination.relative_to(staging).as_posix()
            manifest["files"].append(
                {
                    "path": "files/" + relative,
                    "filename": relative,
                    "format": destination.suffix.lower().lstrip(".") or "file",
                    "size": size,
                    "sha256": digest.hexdigest(),
                },
            )

        for download in request["downloads"]:
            source = Path(download["filePath"])
            name = download["suggestedFilename"].replace("\\", "/").split("/")[-1]
            is_zip = name.lower().endswith(".zip")
            if request.get("requireZip") and not is_zip:
                raise ValueError("Expected a folder ZIP download")
            if is_zip:
                with zipfile.ZipFile(source) as archive:
                    for entry in archive.infolist():
                        if entry.is_dir():
                            # Dropbox includes a root marker, not a child path.
                            if entry.filename != "/":
                                destination_for(entry.orig_filename).mkdir(
                                    parents=True,
                                    exist_ok=True,
                                )
                            continue
                        if stat.S_ISLNK(entry.external_attr >> 16):
                            raise ValueError(
                                f"Archive contains a symlink: {entry.filename!r}",
                            )
                        with archive.open(entry) as member:
                            save(member, entry.orig_filename)
            else:
                with source.open("rb") as member:
                    save(member, name)
        if not manifest["files"]:
            raise ValueError("Provider returned no files")
        # Complete and CRC-check every entry before replacing the previous tree.
        final = output / "files"
        backup = Path(temporary) / "previous"
        staged_manifest = Path(temporary) / "downloads.json"
        staged_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        if final.exists():
            final.rename(backup)
        try:
            staging.rename(final)
            os.replace(staged_manifest, previous)
        except Exception:
            if final.exists():
                shutil.rmtree(final)
            if backup.exists():
                backup.rename(final)
            raise
        # Remove legacy transport copies only after publication has succeeded.
        for item in old_manifest.get("downloads", []):
            if isinstance(item, dict) and re.fullmatch(
                r"download-\d+\.[A-Za-z0-9]+",
                item.get("path", ""),
            ):
                (output / item["path"]).unlink(missing_ok=True)
    for download in request["downloads"]:
        Path(download["filePath"]).unlink(missing_ok=True)
    return manifest


if __name__ == "__main__":
    try:
        print(json.dumps(unpack_downloads(Path(sys.argv[1]), json.load(sys.stdin))))
    except Exception as error:
        print(f"Download extraction failed: {error}", file=sys.stderr)
        sys.exit(1)
