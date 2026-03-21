#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
# ]
# ///
#
# Emit node/npm binary dependencies for the crawl.
# This hook runs early in the Crawl lifecycle so node/npm are installed before any npm-based extractors (e.g., puppeteer) run.
#
# Usage:
#     ./on_Crawl__01_npm_install.py > events.jsonl

import os
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import emit_binary_record

PLUGIN_DIR = Path(__file__).parent.name
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", ".")).resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main() -> None:
    emit_binary_record(
        name="node",
        binproviders="env,apt,brew",
        overrides={"apt": {"install_args": ["nodejs"]}},
    )

    emit_binary_record(
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
