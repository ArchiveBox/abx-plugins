#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
#
# Emit yt-dlp (and related) Binary dependencies for the crawl.
#
# Usage:
#     ./on_Crawl__15_ytdlp_install.py > events.jsonl

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parent.parent))
from base.utils import get_env, get_env_bool, output_binary

PLUGIN_DIR = Path(__file__).parent.name
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", ".")).resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    ytdlp_enabled = get_env_bool("YTDLP_ENABLED", True)

    if not ytdlp_enabled:
        sys.exit(0)

    output_binary(
        name="yt-dlp",
        binproviders="env,pip,brew,apt",
        overrides={"pip": {"install_args": ["yt-dlp[default]"]}},
    )

    # Node.js (required by several JS-based extractors)
    output_binary(
        name="node",
        binproviders="env,apt,brew",
        overrides={"apt": {"install_args": ["nodejs"]}},
    )

    # ffmpeg (used by media extraction)
    output_binary(name="ffmpeg", binproviders="env,apt,brew")

    sys.exit(0)


if __name__ == "__main__":
    main()
