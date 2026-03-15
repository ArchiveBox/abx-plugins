#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
#
# Emit forum-dl Binary dependency for the crawl, outputting a JSONL record to stdout.
#
# Usage:
#     ./on_Crawl__25_forumdl_install.py > events.jsonl

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from base.utils import get_env, get_env_bool, output_binary

PLUGIN_DIR = Path(__file__).parent.name
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", ".")).resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def output_resolved_binary(name: str, abspath: str, binprovider: str = "env") -> None:
    machine_id = os.environ.get("MACHINE_ID", "")
    print(
        json.dumps(
            {
                "type": "Binary",
                "name": name,
                "abspath": abspath,
                "binprovider": binprovider,
                "machine_id": machine_id,
            }
        )
    )


def main():
    forumdl_enabled = get_env_bool("FORUMDL_ENABLED", True)

    if not forumdl_enabled:
        sys.exit(0)

    forumdl_binary = get_env("FORUMDL_BINARY", "forum-dl")
    forumdl_binary_path = shutil.which(forumdl_binary)
    if forumdl_binary_path:
        output_resolved_binary(name="forum-dl", abspath=forumdl_binary_path)
        sys.exit(0)

    output_binary(
        name="forum-dl",
        binproviders="env,pip",
        overrides={
            "pip": {
                "packages": [
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
                ]
            }
        },
    )

    sys.exit(0)


if __name__ == "__main__":
    main()
