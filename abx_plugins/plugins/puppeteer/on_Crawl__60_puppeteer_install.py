#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
# ]
# ///
"""
Emit Puppeteer Binary dependency for the crawl.
"""

import json
import os
import sys
from pathlib import Path

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

    record = {
        "type": "Binary",
        "name": "puppeteer",
        "binproviders": "npm,env",
        "overrides": {
            "npm": {
                "packages": ["puppeteer"],
            }
        },
    }
    print(json.dumps(record))
    sys.exit(0)


if __name__ == "__main__":
    main()
