#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""
Emit @anthropic-ai/claude-code Binary dependency for the crawl.
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
    """Output Binary JSONL record for a dependency."""
    machine_id = os.environ.get("MACHINE_ID", "")

    record = {
        "type": "Binary",
        "name": name,
        "binproviders": binproviders,
        "overrides": {
            "npm": {
                "packages": ["@anthropic-ai/claude-code"],
            },
        },
        "machine_id": machine_id,
    }
    print(json.dumps(record))


def main():
    claudecode_enabled = get_env_bool("CLAUDECODE_ENABLED", False)

    if not claudecode_enabled:
        sys.exit(0)

    # Check for API key
    api_key = get_env("ANTHROPIC_API_KEY")
    if not api_key:
        print("WARNING: ANTHROPIC_API_KEY not set, Claude Code will not be functional", file=sys.stderr)

    # Honor custom binary path - skip npm install if user provides their own
    custom_binary = get_env("CLAUDECODE_BINARY")
    if custom_binary and custom_binary != "claude":
        # Use basename for Binary record name (env provider does PATH lookup, not absolute paths)
        binary_name = Path(custom_binary).name
        output_binary(name=binary_name, binproviders="env")
    else:
        output_binary(name="claude", binproviders="env,npm")

    print(json.dumps({
        "type": "ArchiveResult",
        "status": "succeeded",
        "output_str": "claude requested",
    }))

    sys.exit(0)


if __name__ == "__main__":
    main()
