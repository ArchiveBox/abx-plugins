#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""Prepare a crawl-local copy of the pinned extension with local proof export."""

import fcntl
import hashlib
import tempfile
import json
import os
import shutil
from pathlib import Path
from abx_plugins.plugins.base.utils import load_config

config = load_config()
if config.TLSNOTARY_ENABLED:
    metadata = json.loads(
        (
            Path(os.environ["CHROMEWEBSTORE_EXTENSIONS_DIR"])
            / "tlsnotary.extension.json"
        ).read_text(),
    )
    source = Path(metadata["unpacked_path"])
    if json.loads((source / "manifest.json").read_text())["version"] != "0.1.0.1501":
        raise RuntimeError("TLSNotary export patch requires release 0.1.0.1501")
    destination = Path(config.PERSONAS_DIR) / ".tlsnotary" / "0.1.0.1501-export2"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (destination.parent / "prepare.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(
            prefix=".prepare-",
            dir=destination.parent,
        ) as stage:
            staging = Path(stage)
            shutil.copytree(source, staging, dirs_exist_ok=True)
            bundle = staging / "offscreen.bundle.js"
            script = bundle.read_text()
            # ZIP pinned in config.json; SessionManager.ts:354-381. These fields go only
            # to the local caller, never to the remote verifier's sessionData.
            old = (
                'return p("COMPLETE",1,"Complete"),w}finally{await r.cleanupProver(b)}'
            )
            new = 'return p("COMPLETE",1,"Complete"),{...w,localProof:{openings:v,commit:f,recv:Array.from(r.getRecvBytes(b))}}}finally{await r.cleanupProver(b)}'
            if script.count(old) != 1:
                raise RuntimeError(
                    "TLSNotary export patch does not match pinned release",
                )
            script = script.replace(old, new).replace(
                'f&&u.debug("reveal openings",v),',
                "",
            )
            bundle.write_text(script)
            # Let Chrome position the approval popup. Centering on an oversized headless
            # window (ArchiveBox's default) can put it outside the virtual screen.
            background = staging / "background.bundle.js"
            background_script = background.read_text()
            position = "height:this.POPUP_HEIGHT,left:a,top:d,focused:!0"
            if background_script.count(position) != 1:
                raise RuntimeError(
                    "TLSNotary popup patch does not match pinned release",
                )
            background_script = background_script.replace(
                position,
                "height:this.POPUP_HEIGHT,focused:!0",
            )
            managed_position = "width:t,height:s,left:e,top:i}"
            if background_script.count(managed_position) != 1:
                raise RuntimeError(
                    "TLSNotary managed-window patch does not match pinned release",
                )
            background.write_text(
                background_script.replace(managed_position, "width:t,height:s}"),
            )
            if destination.exists():
                if (destination / "offscreen.bundle.js").read_text() != script or (
                    destination / "background.bundle.js"
                ).read_bytes() != background.read_bytes():
                    raise RuntimeError(
                        "Prepared extension differs from the pinned version; use a fresh persona cache",
                    )
            else:
                staging.rename(destination)
    metadata_path = Path(config.CRAWL_DIR) / "tlsnotary" / "extension.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(
            {
                "name": "tlsnotary",
                "unpacked_path": str(destination.resolve()),
                "version": "0.1.0.1501",
                "export_patch": 2,
                "bundle_sha256": hashlib.sha256(script.encode()).hexdigest(),
            },
        ),
    )
