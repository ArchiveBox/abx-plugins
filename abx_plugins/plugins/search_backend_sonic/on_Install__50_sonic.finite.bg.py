#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "abx-plugins",
# ]
# [tool.uv.sources]
# abx-plugins = { path = "../../..", editable = true }
# ///
"""
Emit Sonic Binary dependency for the crawl.
"""

import os
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import emit_binary_request_record, load_config

PLUGIN_DIR = Path(__file__).parent.name
CONFIG = load_config()
CRAWL_DIR = Path(CONFIG.CRAWL_DIR or ".").resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    config = load_config()

    if config.ABX_RUNTIME != "archivebox":
        sys.exit(0)

    if config.SEARCH_BACKEND_ENGINE != "sonic":
        sys.exit(0)

    sonic_binary = config.SONIC_BINARY

    if sonic_binary and sonic_binary != "sonic":
        emit_binary_request_record(
            name="sonic",
            binproviders="env",
            overrides={"env": {"abspath": sonic_binary}},
        )
    else:
        emit_binary_request_record(
            name="sonic",
            binproviders="env,apt,brew,cargo",
            overrides={
                "apt": {"install_args": ["sonic"]},
                "brew": {"install_args": ["sonic"]},
                "cargo": {"install_args": ["sonic-server"]},
            },
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
