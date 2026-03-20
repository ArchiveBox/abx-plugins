#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
# ]
# ///
"""
Emit defuddle Binary dependency for the crawl.
"""

import json
import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from base.utils import emit_binary_record, get_env_bool

PLUGIN_DIR = Path(__file__).parent.name
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", ".")).resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    if not get_env_bool("DEFUDDLE_ENABLED", True):
        sys.exit(0)

    emit_binary_record(name="defuddle", binproviders="env,npm", overrides={"npm": {"install_args": ["defuddle"]}})
    sys.exit(0)


if __name__ == "__main__":
    main()
