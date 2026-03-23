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
# Emit Chromium Binary dependency for the crawl.
# NOTE: We use Chromium instead of Chrome because Chrome 137+ removed support for
# --load-extension and --disable-extensions-except flags, which are needed for
# loading unpacked extensions in headless mode.
#
# Usage:
#     ./on_Install__70_chrome.finite.bg.py > events.jsonl

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

    # Check if Chrome is enabled
    if not config.CHROME_ENABLED:
        sys.exit(0)

    configured_binary = (config.CHROME_BINARY or "").strip()
    configured_name = Path(configured_binary).name.lower() if configured_binary else ""
    if configured_name in ("chrome", "google-chrome"):
        browser_name = "chrome"
    elif configured_name in ("chromium", "chromium-browser"):
        browser_name = "chromium"
    else:
        browser_name = "chromium"

    emit_binary_request_record(
        name=browser_name,
        binproviders="puppeteer",
        overrides={
            "puppeteer": [f"{browser_name}@latest", "--install-deps"],
        },
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
