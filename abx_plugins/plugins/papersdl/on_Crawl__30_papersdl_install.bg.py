#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
#
# Emit papers-dl Binary dependency for the crawl.
#
# Usage:
#     ./on_Crawl__30_papersdl_install.py > events.jsonl

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from base.utils import get_env, get_env_bool, output_binary

PLUGIN_DIR = Path(__file__).parent.name
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", ".")).resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    papersdl_enabled = get_env_bool("PAPERSDL_ENABLED", True)

    if not papersdl_enabled:
        sys.exit(0)

    output_binary(name="papers-dl", binproviders="env,pip")

    sys.exit(0)


if __name__ == "__main__":
    main()
