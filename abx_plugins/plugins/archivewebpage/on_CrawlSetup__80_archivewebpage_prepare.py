#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///

import os
import json
import hashlib
import tempfile
from pathlib import Path


EXTENSION_NAME = "archivewebpage"

extensions_dir = Path(os.environ["CHROMEWEBSTORE_EXTENSIONS_DIR"])
metadata_path = extensions_dir / f"{EXTENSION_NAME}.extension.json"
if not metadata_path.is_file():
    raise RuntimeError(f"Chrome extension metadata was not prepared: {metadata_path}")
metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
manifest_path = Path(metadata["unpacked_path"]) / "manifest.json"
if not manifest_path.is_file():
    raise RuntimeError(f"Chrome extension manifest was not prepared: {manifest_path}")

# AWP 0.17.0 exports request cookies in WARC but omits them from CDX. WACZ
# range replay only reads the indexed response, so authenticated SPAs lose
# their captured session state. Carry the same standard req.http:cookie field
# as Webrecorder's py-wacz indexer. Remove when the pinned upstream includes it.
# Only patch the exact pinned bundle, before Chrome starts loading it.
worker_path = manifest_path.parent / "sw.js"
worker = worker_path.read_bytes()
original = b"recordDigest:e.recordDigest,status:e.status};if(t&&(n.filename=t)"
patched = (
    b"recordDigest:e.recordDigest,status:e.status};"
    b'const cookie=new Headers(e.reqHeaders||{}).get("cookie");'
    b'if(cookie)n["req.http:cookie"]=cookie;'
    b"if(t&&(n.filename=t)"
)
unpatched = worker.replace(patched, original)
if hashlib.sha256(unpatched).hexdigest() != (
    "eb449aec15884416b045f3e21d2f5c88db840968a38ccf1b6b12e1c7e587cec9"
):
    raise RuntimeError(
        "Unexpected ArchiveWeb.page worker; review the upstream cookie index fix",
    )
if worker == unpatched:
    if worker.count(original) != 1:
        raise RuntimeError("ArchiveWeb.page cookie index patch target is not unique")
    with tempfile.NamedTemporaryFile(dir=worker_path.parent, delete=False) as tmp:
        temporary = Path(tmp.name)
        tmp.write(worker.replace(original, patched))
    try:
        temporary.chmod(worker_path.stat().st_mode)
        temporary.replace(worker_path)
    finally:
        temporary.unlink(missing_ok=True)
