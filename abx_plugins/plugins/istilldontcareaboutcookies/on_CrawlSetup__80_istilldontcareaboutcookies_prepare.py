#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///

import os
from pathlib import Path


extensions_dir = Path(os.environ["CHROMEWEBSTORE_EXTENSIONS_DIR"])
if not extensions_dir.is_dir():
    raise RuntimeError(f"Chrome extension directory was not prepared: {extensions_dir}")
