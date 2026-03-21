#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
# ]
# ///
#
# Emit git Binary dependency for the crawl.
#
# Usage:
#     ./on_Crawl__05_git_install.py > events.jsonl

import os
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import emit_binary_record, get_env_bool

PLUGIN_DIR = Path(__file__).parent.name
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", ".")).resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    git_enabled = get_env_bool("GIT_ENABLED", True)

    if not git_enabled:
        sys.exit(0)

    emit_binary_record(name="git", binproviders="env,apt,brew")

    sys.exit(0)


if __name__ == "__main__":
    main()
