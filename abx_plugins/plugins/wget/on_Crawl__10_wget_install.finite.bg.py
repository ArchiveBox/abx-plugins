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
# Emit wget Binary dependency for the crawl.
#
# Usage:
#     ./on_Crawl__10_wget_install.py > events.jsonl

import os
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import emit_binary_record, load_config

PLUGIN_DIR = Path(__file__).parent.name
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", ".")).resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    warnings = []
    errors = []

    # Load config from config.json (auto-resolves x-aliases and x-fallback from env)
    config = load_config()
    wget_enabled = config.WGET_ENABLED
    wget_timeout = config.WGET_TIMEOUT
    # Validate timeout with warning (not error)
    if wget_enabled and wget_timeout < 20:
        warnings.append(
            f"WGET_TIMEOUT={wget_timeout} is very low. "
            "wget may fail to archive sites if set to less than ~20 seconds. "
            "Consider setting WGET_TIMEOUT=60 or higher.",
        )

    if wget_enabled:
        emit_binary_record(name="wget", binproviders="env,apt,brew")

    for warning in warnings:
        print(f"WARNING:{warning}", file=sys.stderr)

    for error in errors:
        print(f"ERROR:{error}", file=sys.stderr)

    # Exit with error if any hard errors
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
