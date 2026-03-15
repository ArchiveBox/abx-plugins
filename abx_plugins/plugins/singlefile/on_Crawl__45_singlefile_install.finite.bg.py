#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
# ]
# ///
#
# Emit single-file Binary dependency for the crawl.
#

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
    singlefile_enabled = get_env_bool("SINGLEFILE_ENABLED", True)

    if not singlefile_enabled:
        sys.exit(0)

    output_binary(
        name="single-file",
        binproviders="env,npm",
        overrides={"npm": {"install_args": ["single-file-cli"]}},
    )

    sys.exit(0)


if __name__ == "__main__":
    main()
