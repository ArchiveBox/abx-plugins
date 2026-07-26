#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///

import os
import json
from pathlib import Path


EXTENSION_NAME = "twocaptcha"

extensions_dir = Path(os.environ["CHROMEWEBSTORE_EXTENSIONS_DIR"])
metadata_path = extensions_dir / f"{EXTENSION_NAME}.extension.json"
if not metadata_path.is_file():
    raise RuntimeError(f"Chrome extension metadata was not prepared: {metadata_path}")
metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
manifest_path = Path(metadata["unpacked_path"]) / "manifest.json"
if not manifest_path.is_file():
    raise RuntimeError(f"Chrome extension manifest was not prepared: {manifest_path}")
