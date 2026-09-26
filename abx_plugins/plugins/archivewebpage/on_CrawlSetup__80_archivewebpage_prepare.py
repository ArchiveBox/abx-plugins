#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///

import os
import errno
import json
import shutil
import tempfile
from pathlib import Path

from abx_plugins.plugins.base.utils import load_config, write_text_atomic


EXTENSION_NAME = "archivewebpage"

extensions_dir = Path(os.environ["CHROMEWEBSTORE_EXTENSIONS_DIR"])
metadata_path = extensions_dir / f"{EXTENSION_NAME}.extension.json"
if not metadata_path.is_file():
    raise RuntimeError(f"Chrome extension metadata was not prepared: {metadata_path}")
metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
manifest_path = Path(metadata["unpacked_path"]) / "manifest.json"
if not manifest_path.is_file():
    raise RuntimeError(f"Chrome extension manifest was not prepared: {manifest_path}")

config = load_config()
source = manifest_path.parent
version = json.loads(manifest_path.read_text())["version"]
if version != "0.17.0":
    raise RuntimeError("ArchiveWeb.page service-worker patch requires release 0.17.0")
script = (source / "bg.js").read_text()
old = '"Network.setBypassServiceWorker",{bypass:!0}'
if script.count(old) != 1:
    raise RuntimeError(
        "ArchiveWeb.page service-worker patch does not match pinned release",
    )
# AWP bypasses workers in every recording session, including child frames.
# That changes what the page does: ReplayWeb.page's worker-owned replay URLs
# fall through to the website, recursively embed it, and eventually wedge the
# renderer. Preserve normal worker handling so all extractors see the actual
# page; AWP still records its Network response events and bodies.
script = script.replace(old, '"Network.setBypassServiceWorker",{bypass:!1}')
# Worker-generated responses have no bytes transferred over the network, even
# when their body is nonempty. AWP's wire-byte gate otherwise silently drops
# these bodies from the WACZ after the live page has rendered successfully.
old_payload = (
    'e.encodedDataLength&&(r=yield this.fetchPayloads(e,t,i,"Network.getResponseBody"))'
)
if script.count(old_payload) != 1:
    raise RuntimeError(
        "ArchiveWeb.page worker-response patch does not match pinned release",
    )
script = script.replace(
    old_payload,
    '(e.encodedDataLength||t.fromServiceWorker)&&(r=yield this.fetchPayloads(e,t,i,"Network.getResponseBody"))',
)
destination = (
    Path(config.PERSONAS_DIR) / ".archivewebpage" / f"{version}-service-workers2"
)
destination.parent.mkdir(parents=True, exist_ok=True)
if not destination.exists():
    # Publish a complete directory atomically. Concurrent crawls can reuse the
    # identical versioned copy; none can observe a half-copied extension.
    with tempfile.TemporaryDirectory(
        prefix=".prepare-",
        dir=destination.parent,
    ) as temporary:
        staging = Path(temporary) / "extension"
        shutil.copytree(source, staging)
        (staging / "bg.js").write_text(script)
        try:
            staging.rename(destination)
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
if (destination / "bg.js").read_text() != script:
    raise RuntimeError(
        "Prepared ArchiveWeb.page differs from pinned service-worker patch",
    )
prepared_path = (
    Path(config.CRAWL_DIR) / "chrome" / "extensions" / "archivewebpage.extension.json"
)
prepared_path.parent.mkdir(parents=True, exist_ok=True)
write_text_atomic(
    prepared_path,
    json.dumps(
        {
            "name": EXTENSION_NAME,
            "version": version,
            "unpacked_path": str(destination.resolve()),
        },
    ),
)
