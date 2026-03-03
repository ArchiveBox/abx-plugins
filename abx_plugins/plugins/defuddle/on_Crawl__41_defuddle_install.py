#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""
Emit defuddle Binary dependency for the crawl.
"""

import json
import os
import sys
from pathlib import Path

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


def output_binary(name: str, binproviders: str):
    machine_id = os.environ.get("MACHINE_ID", "")

    record = {
        "type": "Binary",
        "name": name,
        "binproviders": binproviders,
        "overrides": {
            "npm": {
                "packages": ["defuddle"],
            },
        },
        "machine_id": machine_id,
    }
    print(json.dumps(record))


def main():
    if not get_env_bool("DEFUDDLE_ENABLED", True):
        sys.exit(0)

    output_binary(name="defuddle", binproviders="npm,env")
    sys.exit(0)


if __name__ == "__main__":
    main()
