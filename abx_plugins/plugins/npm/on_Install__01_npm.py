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
#
# Emit node/npm binary dependencies for the crawl.
# This hook runs early in the Crawl lifecycle so node/npm are installed before any npm-based extractors (e.g., puppeteer) run.
#
# Usage:
#     ./on_Install__01_npm.py > events.jsonl

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


def main() -> None:
    emit_binary_request_record(
        name="node",
        binproviders="env,apt,brew",
        overrides={"apt": {"install_args": ["nodejs"]}},
    )

    emit_binary_request_record(
        name="npm",
        binproviders="env,apt,brew",
        overrides={
            "apt": {"install_args": ["nodejs", "npm"]},
            "brew": {"install_args": ["node"]},
        },
    )

    sys.exit(0)


if __name__ == "__main__":
    main()
