#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
#
# Emit Chromium Binary dependency for the crawl.
# NOTE: We use Chromium instead of Chrome because Chrome 137+ removed support for
# --load-extension and --disable-extensions-except flags, which are needed for
# loading unpacked extensions in headless mode.
#
# Usage:
#     ./on_Crawl__70_chrome_install.py > events.jsonl

import json
import os
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent.name
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", ".")).resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    # Check if Chrome is enabled
    chrome_enabled = os.environ.get("CHROME_ENABLED", "true").lower() not in (
        "false",
        "0",
        "no",
        "off",
    )
    if not chrome_enabled:
        sys.exit(0)

    record = {
        "type": "Binary",
        "name": "chromium",
        "binproviders": "puppeteer",
        "overrides": {
            "puppeteer": ["chromium@latest", "--install-deps"],
        },
    }
    print(json.dumps(record))
    print(json.dumps({
        "type": "ArchiveResult",
        "status": "succeeded",
        "output_str": "chromium requested",
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
