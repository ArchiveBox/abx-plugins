#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
#
# Emit wget Binary dependency for the crawl.
#
# Usage:
#     ./on_Crawl__10_wget_install.py > events.jsonl

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from base.utils import get_env, get_env_bool, get_env_int, output_binary, output_machine_config

PLUGIN_DIR = Path(__file__).parent.name
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", ".")).resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    warnings = []
    errors = []

    # Get config values
    wget_enabled = get_env_bool("WGET_ENABLED", True)
    wget_timeout = get_env_int("WGET_TIMEOUT") or get_env_int("TIMEOUT", 60)
    wget_binary = get_env("WGET_BINARY", "wget")

    # Compute derived values (USE_WGET for backward compatibility)
    use_wget = wget_enabled

    # Validate timeout with warning (not error)
    if use_wget and wget_timeout < 20:
        warnings.append(
            f"WGET_TIMEOUT={wget_timeout} is very low. "
            "wget may fail to archive sites if set to less than ~20 seconds. "
            "Consider setting WGET_TIMEOUT=60 or higher."
        )

    if use_wget:
        output_binary(name="wget", binproviders="env,apt,brew,pip")

    # Output computed config patch as JSONL
    output_machine_config(
        {
            "USE_WGET": use_wget,
            "WGET_BINARY": wget_binary,
        }
    )

    for warning in warnings:
        print(f"WARNING:{warning}", file=sys.stderr)

    for error in errors:
        print(f"ERROR:{error}", file=sys.stderr)

    # Exit with error if any hard errors
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
