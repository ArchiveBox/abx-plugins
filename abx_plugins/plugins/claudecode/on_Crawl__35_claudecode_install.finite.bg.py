#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
# ]
# ///
"""
Emit @anthropic-ai/claude-code Binary dependency for the crawl.
"""

import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from base.utils import emit_binary_record, get_env, get_env_bool

PLUGIN_DIR = Path(__file__).parent.name
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", ".")).resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    claudecode_enabled = get_env_bool("CLAUDECODE_ENABLED", False)

    if not claudecode_enabled:
        print("SKIPPED: CLAUDECODE_ENABLED=False")
        sys.exit(0)

    # Check for API key
    api_key = get_env("ANTHROPIC_API_KEY")
    if not api_key:
        print("WARNING: ANTHROPIC_API_KEY not set, Claude Code will not be functional", file=sys.stderr)

    # Honor custom binary path - skip npm install if user provides their own
    custom_binary = get_env("CLAUDECODE_BINARY")
    if custom_binary and custom_binary != "claude":
        emit_binary_record(name=Path(custom_binary).name, binproviders="env")
    else:
        emit_binary_record(
            name="claude",
            binproviders="env,npm",
            overrides={"npm": {"install_args": ["@anthropic-ai/claude-code"]}},
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
