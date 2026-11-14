"""Read saved hash inventories for host presentation and storage auditing."""

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath


@lru_cache(maxsize=32)
def _read_inventory(path, mtime_ns, size, ctime_ns):
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Invalid hash inventory")
    entries = data.get("files")
    if not isinstance(entries, list):
        entries = [
            {**metadata, "path": name}
            for name, metadata in data.items()
            if isinstance(metadata, dict) and name != "."
        ]
    files = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("path") or "").strip().rstrip("/")
        relative = PurePosixPath(name)
        if not name or relative.is_absolute() or ".." in relative.parts:
            continue
        files[name] = {
            "size": entry.get("size")
            or entry.get("num_bytes")
            or entry.get("bytes")
            or 0,
            "is_dir": bool(entry.get("is_dir"))
            or entry.get("mime_type") == "inode/directory"
            or str(entry.get("path")).endswith("/"),
            "hash": entry.get("hash") or entry.get("hash_sha256"),
        }
    return files, hashlib.sha256(raw).hexdigest()


def read_snapshot_inventory(snapshot_dir, *, verify=False):
    path = Path(snapshot_dir) / "hashes" / "hashes.json"
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    inventory, digest = _read_inventory(
        path,
        info.st_mtime_ns,
        info.st_size,
        info.st_ctime_ns,
    )
    if verify:
        try:
            expected = path.with_name("hashes.sha256").read_text().strip()
        except OSError as error:
            raise ValueError("Hash inventory is incomplete or changed.") from error
        if not re.fullmatch("[0-9a-f]{64}", expected) or expected != digest:
            raise ValueError("Hash inventory is incomplete or changed.")
    return inventory
