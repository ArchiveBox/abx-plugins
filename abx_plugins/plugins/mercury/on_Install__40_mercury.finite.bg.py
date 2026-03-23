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
#
# Emit postlight-parser Binary dependency for the crawl if mercury is enabled.
#
# Usage:
#     ./on_Install__40_mercury.finite.bg.py > events.jsonl

import os
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    emit_binary_request_record,
    get_env,
    get_env_bool,
    load_config,
)

PLUGIN_DIR = Path(__file__).parent.name
CONFIG = load_config()
CRAWL_DIR = Path(CONFIG.CRAWL_DIR or ".").resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def main():
    mercury_enabled = get_env_bool("MERCURY_ENABLED", True)

    if not mercury_enabled:
        sys.exit(0)

    mercury_binary = get_env("MERCURY_BINARY", "postlight-parser")
    mercury_binary_name = Path(mercury_binary).name if mercury_binary else ""

    # A cached absolute path to the default CLI should still use the normal
    # npm->env resolution path so npm can report package metadata correctly.
    # Only treat MERCURY_BINARY as an explicit custom override when it points
    # to a different executable name entirely.
    if (
        mercury_binary
        and mercury_binary_name
        and mercury_binary_name != "postlight-parser"
    ):
        emit_binary_request_record(
            name="postlight-parser",
            binproviders="env",
            overrides={"env": {"abspath": mercury_binary}},
        )
        sys.exit(0)

    emit_binary_request_record(
        name="postlight-parser",
        binproviders="npm,env",
        overrides={"npm": {"install_args": ["@postlight/parser"]}},
    )

    sys.exit(0)


if __name__ == "__main__":
    main()
