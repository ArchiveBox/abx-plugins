#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
# ]
# ///
#
# Emit postlight-parser Binary dependency for the crawl if mercury is enabled.
#
# Usage:
#     ./on_Crawl__40_mercury_install.py > events.jsonl

import json
import os
import shutil
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
    """Output Binary JSONL record for a dependency."""
    machine_id = os.environ.get("MACHINE_ID", "")

    record = {
        "type": "Binary",
        "name": name,
        "binproviders": binproviders,
        "overrides": {
            "npm": {
                "packages": ["@postlight/parser"],
            }
        },
        "machine_id": machine_id,
    }
    print(json.dumps(record))


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
    mercury_enabled = get_env_bool("MERCURY_ENABLED", True)

    if not mercury_enabled:
        sys.exit(0)

    mercury_binary = get_env("MERCURY_BINARY", "postlight-parser")
    mercury_binary_path = shutil.which(mercury_binary)
    if mercury_binary_path:
        output_resolved_binary(name="postlight-parser", abspath=mercury_binary_path)
        sys.exit(0)

    output_binary(name="postlight-parser", binproviders="env,npm")

    sys.exit(0)


if __name__ == "__main__":
    main()
