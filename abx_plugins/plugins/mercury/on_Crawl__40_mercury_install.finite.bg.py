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

sys.path.append(str(Path(__file__).resolve().parent.parent))
from base.utils import get_env, get_env_bool, output_binary

PLUGIN_DIR = Path(__file__).parent.name
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", ".")).resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    mercury_enabled = get_env_bool("MERCURY_ENABLED", True)

    if not mercury_enabled:
        sys.exit(0)

    mercury_binary = get_env("MERCURY_BINARY", "postlight-parser")
    mercury_binary_path = shutil.which(mercury_binary)
    if mercury_binary_path:
        # Emit pre-resolved binary location
        machine_id = os.environ.get("MACHINE_ID", "")
        print(json.dumps({"type": "Binary", "name": "postlight-parser", "abspath": mercury_binary_path, "binprovider": "env", "machine_id": machine_id}))
        sys.exit(0)

    output_binary(name="postlight-parser", binproviders="env,npm", overrides={"npm": {"install_args": ["@postlight/parser"]}})

    sys.exit(0)


if __name__ == "__main__":
    main()
