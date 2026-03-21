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
"""Emit trafilatura Binary dependency for the crawl if enabled."""

import os
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import emit_binary_record, get_env_bool

PLUGIN_DIR = Path(__file__).parent.name
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", ".")).resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main() -> None:
    if not get_env_bool("TRAFILATURA_ENABLED", True):
        sys.exit(0)

    emit_binary_record(
        name="trafilatura",
        binproviders="env,pip",
        overrides={"pip": {"install_args": ["trafilatura"]}},
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
