#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
#
# Emit node/npm binary dependencies for the crawl.
# This hook runs early in the Crawl lifecycle so node/npm are installed before any npm-based extractors (e.g., puppeteer) run.
#
# Usage:
#     ./on_Crawl__00_npm_install.py > events.jsonl

import json
import os
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent.name
CRAWL_DIR = Path(os.environ.get('CRAWL_DIR', '.')).resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def get_env(name: str, default: str = '') -> str:
    return os.environ.get(name, default).strip()


def output_binary(name: str, binproviders: str, overrides: dict | None = None) -> None:
    machine_id = os.environ.get('MACHINE_ID', '')
    record = {
        'type': 'Binary',
        'name': name,
        'binproviders': binproviders,
        'machine_id': machine_id,
    }
    if overrides:
        record['overrides'] = overrides
    print(json.dumps(record))


def main() -> None:
    output_binary(
        name='node',
        binproviders='apt,brew,env',
        overrides={'apt': {'packages': ['nodejs']}},
    )

    output_binary(
        name='npm',
        binproviders='apt,brew,env',
        overrides={
            'apt': {'packages': ['nodejs', 'npm']},
            'brew': {'packages': ['node']},
        },
    )

    sys.exit(0)


if __name__ == '__main__':
    main()
