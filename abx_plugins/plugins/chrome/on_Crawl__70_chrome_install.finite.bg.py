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

import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from base.utils import output_binary

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

    configured_binary = os.environ.get("CHROME_BINARY", "").strip()
    configured_name = Path(configured_binary).name.lower() if configured_binary else ""
    if configured_name in ("chrome", "google-chrome"):
        browser_name = "chrome"
    elif configured_name in ("chromium", "chromium-browser"):
        browser_name = "chromium"
    else:
        browser_name = "chromium"

    output_binary(
        name=browser_name,
        binproviders="puppeteer",
        overrides={
            "puppeteer": [f"{browser_name}@latest", "--install-deps"],
        },
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
