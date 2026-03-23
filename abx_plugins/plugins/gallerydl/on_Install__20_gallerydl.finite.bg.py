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
# Emits gallery-dl as a Binary dependency for the crawl, configured via environment variables.
#
# Usage:
#     ./on_Install__20_gallerydl.finite.bg.py > events.jsonl

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
    gallerydl_enabled = get_env_bool("GALLERYDL_ENABLED", default=True)

    if not gallerydl_enabled:
        sys.exit(0)

    emit_binary_request_record(name="gallery-dl", binproviders="env,pip,brew,apt")

    sys.exit(0)


if __name__ == "__main__":
    main()
