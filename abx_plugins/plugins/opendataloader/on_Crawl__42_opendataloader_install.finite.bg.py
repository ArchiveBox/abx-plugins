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
# Emits opendataloader-pdf as a Binary dependency for the crawl, configured via environment variables.
#
# Usage:
#     ./on_Crawl__42_opendataloader_install.py > events.jsonl

import os
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import emit_binary_record, get_env_bool, load_config

PLUGIN_DIR = Path(__file__).parent.name
CONFIG = load_config()
CRAWL_DIR = Path(CONFIG.CRAWL_DIR or ".").resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    opendataloader_enabled = get_env_bool("OPENDATALOADER_ENABLED", default=True)

    if not opendataloader_enabled:
        sys.exit(0)

    emit_binary_record(
        name="opendataloader-pdf",
        binproviders="env,pip",
        overrides={"pip": {"install_args": ["opendataloader-pdf"]}},
    )

    emit_binary_record(
        name="java",
        binproviders="env,brew" if sys.platform == "darwin" else "env,apt,brew",
        overrides={
            "brew": {"install_args": ["openjdk"]},
            "apt": {"install_args": ["default-jre"]},
        },
        min_version="11.0.0",
    )

    sys.exit(0)


if __name__ == "__main__":
    main()
