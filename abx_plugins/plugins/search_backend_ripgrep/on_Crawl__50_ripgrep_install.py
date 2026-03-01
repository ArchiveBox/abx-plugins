#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Emit ripgrep Binary dependency for the crawl.
"""

import os
import sys
import json
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent.name
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", ".")).resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    # Only proceed if ripgrep backend is enabled
    search_backend_engine = os.environ.get("SEARCH_BACKEND_ENGINE", "ripgrep").strip()
    if search_backend_engine != "ripgrep":
        # Not using ripgrep, exit successfully without output
        sys.exit(0)

    machine_id = os.environ.get("MACHINE_ID", "")
    print(
        json.dumps(
            {
                "type": "Binary",
                "name": "rg",
                "binproviders": "apt,brew,env",
                "overrides": {
                    "apt": {"packages": ["ripgrep"]},
                },
                "machine_id": machine_id,
            }
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
