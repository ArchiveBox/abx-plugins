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
# Emit forum-dl Binary dependency for the crawl, outputting a JSONL record to stdout.
#
# Usage:
#     ./on_Crawl__25_forumdl_install.py > events.jsonl

import os
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    emit_binary_record,
    get_env,
    get_env_bool,
    load_config,
)

PLUGIN_DIR = Path(__file__).parent.name
CONFIG = load_config()
CRAWL_DIR = Path(CONFIG.CRAWL_DIR or ".").resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    forumdl_enabled = get_env_bool("FORUMDL_ENABLED", True)

    if not forumdl_enabled:
        sys.exit(0)

    forumdl_binary = get_env("FORUMDL_BINARY", "forum-dl")

    emit_binary_record(
        name="forum-dl",
        binproviders="env,pip",
        overrides={
            "env": (
                {"abspath": forumdl_binary} if forumdl_binary != "forum-dl" else {}
            ),
            "pip": {
                "install_args": [
                    "--no-deps",
                    "--prefer-binary",
                    "forum-dl",
                    "chardet==5.2.0",
                    "pydantic==2.12.3",
                    "pydantic-core==2.41.4",
                    "typing-extensions>=4.14.1",
                    "annotated-types>=0.6.0",
                    "typing-inspection>=0.4.2",
                    "beautifulsoup4",
                    "soupsieve",
                    "lxml",
                    "requests",
                    "urllib3",
                    "certifi",
                    "idna",
                    "charset-normalizer",
                    "tenacity",
                    "python-dateutil",
                    "six",
                    "html2text",
                    "warcio",
                ],
            },
        },
    )

    sys.exit(0)


if __name__ == "__main__":
    main()
