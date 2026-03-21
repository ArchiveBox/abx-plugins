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
Emit Puppeteer Binary dependency for the crawl.
"""

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
    enabled = os.environ.get("PUPPETEER_ENABLED", "true").lower() not in (
        "false",
        "0",
        "no",
        "off",
    )
    if not enabled:
        sys.exit(0)

    emit_binary_record(
        name="puppeteer",
        binproviders="npm",
        overrides={
            "npm": {
                "install_args": ["puppeteer"],
            },
        },
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
