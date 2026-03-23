#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "abx-plugins",
# ]
# [tool.uv.sources]
# abx-plugins = { path = "../../..", editable = true }
# ///
"""
Emit @anthropic-ai/claude-code Binary dependency for the crawl.
"""

import os
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    emit_binary_request_record,
    get_env,
    load_config,
)

PLUGIN_DIR = Path(__file__).parent.name
CONFIG = load_config()
CRAWL_DIR = Path(CONFIG.CRAWL_DIR or ".").resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    config = load_config()
    claudecode_enabled = config.CLAUDECODE_ENABLED

    if not claudecode_enabled:
        print("SKIPPED: CLAUDECODE_ENABLED=False")
        sys.exit(0)

    # Check for API key
    api_key = config.ANTHROPIC_API_KEY
    if not api_key:
        print(
            "WARNING: ANTHROPIC_API_KEY not set, Claude Code will not be functional",
            file=sys.stderr,
        )

    # Honor custom binary path - skip npm install if user provides their own
    custom_binary = get_env("CLAUDECODE_BINARY")
    if custom_binary and custom_binary != "claude":
        emit_binary_request_record(
            name="claude",
            binproviders="env",
            overrides={"env": {"abspath": custom_binary}},
        )
    else:
        emit_binary_request_record(
            name="claude",
            binproviders="env,npm",
            overrides={"npm": {"install_args": ["@anthropic-ai/claude-code"]}},
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
