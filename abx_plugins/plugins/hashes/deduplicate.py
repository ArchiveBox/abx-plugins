#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""Deduplicate a validated selection of retained files from a JSON request."""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from abx_plugins.plugins.hashes.files import deduplicate_files, sha256_file


def main():
    request = json.load(sys.stdin)
    root = Path(request["root"]).resolve(strict=True)
    if not root.is_dir() or not isinstance(request["files"], list):
        raise ValueError("Expected a directory and a list of files")
    devices = defaultdict(list)
    for entry in request["files"]:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("File paths must be relative to the selected root")
        path = root / relative
        if (
            path.is_symlink()
            or not path.resolve(strict=True).is_relative_to(root)
            or not path.is_file()
        ):
            raise ValueError(
                "Only regular files within the selected root can be deduplicated",
            )
        if (
            type(entry["size"]) is not int
            or entry["size"] < 0
            or not re.fullmatch("[0-9a-f]{64}", entry["hash"])
        ):
            raise ValueError("Each file needs a size and SHA256 digest")
        info = path.stat()
        if info.st_size != entry["size"] or sha256_file(path) != entry["hash"]:
            raise ValueError(
                "A selected file changed. No files changed; run a new audit.",
            )
        devices[info.st_dev].append(entry)
    result = {
        "type": "DeduplicateResult",
        "schema_version": 1,
        "supported_platform": sys.platform == "linux",
        "attempted_files": 0,
        "extent_bytes_verified": 0,
        "failed_files": 0,
    }
    if not result["supported_platform"]:
        result["error"] = (
            "Native copy-on-write deduplication currently requires Linux and a supporting filesystem. No files changed."
        )
    else:
        for files in devices.values():
            applied = deduplicate_files(root, files)
            for key in ("attempted_files", "extent_bytes_verified", "failed_files"):
                result[key] += applied[key]
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, TypeError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
