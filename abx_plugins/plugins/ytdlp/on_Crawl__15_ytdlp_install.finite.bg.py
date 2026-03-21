#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
# ]
# ///
#
# Emit yt-dlp (and related) Binary dependencies for the crawl.
#
# Usage:
#     ./on_Crawl__15_ytdlp_install.py > events.jsonl

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
    ytdlp_enabled = get_env_bool("YTDLP_ENABLED", True)

    if not ytdlp_enabled:
        sys.exit(0)

    emit_binary_record(
        name="yt-dlp",
        binproviders="env,pip,brew,apt",
        overrides={"pip": {"install_args": ["yt-dlp[default]"]}},
    )

    # Node.js (required by several JS-based extractors)
    emit_binary_record(
        name="node",
        binproviders="env,apt,brew",
        overrides={"apt": {"install_args": ["nodejs"]}},
    )

    # ffmpeg (used by media extraction)
    emit_binary_record(name="ffmpeg", binproviders="env,apt,brew")

    sys.exit(0)


if __name__ == "__main__":
    main()
