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
import sys
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).parent.name
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", ".")).resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def get_env_bool(name: str, default: bool = False) -> bool:
    val = get_env(name, "").lower()
    if val in ("true", "1", "yes", "on"):
        return True
    if val in ("false", "0", "no", "off"):
        return False
    return default


def output_binary(
    name: str, binproviders: str, overrides: dict[str, Any] | None = None
) -> None:
    """Output Binary JSONL record for a dependency."""
    machine_id = os.environ.get("MACHINE_ID", "")

    record: dict[str, Any] = {
        "type": "Binary",
        "name": name,
        "binproviders": binproviders,
        "machine_id": machine_id,
    }
    if overrides:
        record["overrides"] = overrides
    print(json.dumps(record))


def main():
    forumdl_enabled = get_env_bool("FORUMDL_ENABLED", True)

    if not forumdl_enabled:
        sys.exit(0)

    output_binary(
        name="forum-dl",
        binproviders="pip,env",
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
