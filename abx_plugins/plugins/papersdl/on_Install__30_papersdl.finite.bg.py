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
# Emit papers-dl Binary dependency for the crawl.
#
# Usage:
#     ./on_Install__30_papersdl.py > events.jsonl

import os
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    emit_binary_request_record,
    get_env_bool,
    load_config,
)

PLUGIN_DIR = Path(__file__).parent.name
CONFIG = load_config()
CRAWL_DIR = Path(CONFIG.CRAWL_DIR or ".").resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    papersdl_enabled = get_env_bool("PAPERSDL_ENABLED", True)

    if not papersdl_enabled:
        sys.exit(0)

    emit_binary_request_record(name="papers-dl", binproviders="env,pip")

    sys.exit(0)


if __name__ == "__main__":
    main()
