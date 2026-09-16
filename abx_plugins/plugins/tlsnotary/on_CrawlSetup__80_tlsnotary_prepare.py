#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["abx-plugins"]
# ///
"""Build the pinned plugin runtime once into the shared binary cache."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import load_config

config = load_config()
if config.TLSNOTARY_ENABLED and not config.TLSNOTARY_BINARY:
    plugin = Path(__file__).resolve().parent
    target = Path(config.ABXPKG_LIB_DIR) / "tlsnotary" / "target"
    cargo = shutil.which(config.TLSNOTARY_CARGO_BINARY)
    if not cargo:
        raise RuntimeError(
            "TLSNotary requires Rust 1.95+ or TLSNOTARY_BINARY pointing to a prebuilt runtime",
        )
    subprocess.run(
        [
            cargo,
            "build",
            "--release",
            "--locked",
            "--manifest-path",
            str(plugin / "runtime" / "Cargo.toml"),
        ],
        env={**os.environ, "CARGO_TARGET_DIR": str(target)},
        stdout=sys.stderr,
        check=True,
    )
