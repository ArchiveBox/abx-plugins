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

    # Honor OPENDATALOADER_BINARY: if set and the file exists, skip install
    custom_binary = os.environ.get("OPENDATALOADER_BINARY", "").strip()
    if custom_binary and Path(custom_binary).is_file():
        sys.exit(0)

    output_binary(
        name="opendataloader-pdf",
        binproviders="env,pip",
        overrides={"pip": {"install_args": ["opendataloader-pdf"]}},
    )

    custom_java = (
        os.environ.get("OPENDATALOADER_JAVA_BINARY", "").strip()
        or os.environ.get("JAVA_BINARY", "").strip()
    )
    if not (custom_java and Path(custom_java).is_file()):
        output_binary(
            name="java",
            binproviders="brew" if sys.platform == "darwin" else "env,apt,brew",
            overrides={
                "brew": {"install_args": ["openjdk"]},
                "apt": {"install_args": ["default-jre"]},
            },
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
