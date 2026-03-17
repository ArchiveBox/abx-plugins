#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
#
# Emits opendataloader-pdf as a Binary dependency for the crawl, configured via environment variables.
#
# Usage:
#     ./on_Crawl__42_opendataloader_install.py > events.jsonl

import json
import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from base.utils import get_env_bool, output_binary

PLUGIN_DIR = Path(__file__).parent.name
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", ".")).resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    opendataloader_enabled = get_env_bool("OPENDATALOADER_ENABLED", default=True)

    if not opendataloader_enabled:
        sys.exit(0)

    output_binary(
        name="opendataloader-pdf",
        binproviders="env,pip",
        overrides={"pip": {"install_args": ["opendataloader-pdf"]}},
    )

    sys.exit(0)


if __name__ == "__main__":
    main()
