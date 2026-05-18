#!/usr/bin/env -S uv run --active --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "abx-plugins",
# ]
# ///
"""
Require ArchiveBox's plugin-managed Sonic worker before indexing starts.
"""

import os
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    PROCESS_EXIT_SKIPPED,
    load_config,
)
from abx_plugins.plugins.search_backend_sonic.daemon import (
    is_port_listening,
    prepare_sonic_daemon,
)


PLUGIN_DIR = Path(__file__).parent.name
CONFIG = load_config()
CRAWL_DIR = Path(CONFIG.CRAWL_DIR or ".").resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def emit_skipped(reason: str) -> None:
    print(reason)


def emit_listening_summary(host: str, port: int) -> None:
    print(f"{host}:{port}")


def main() -> None:
    config = load_config()

    if config.ABX_RUNTIME != "archivebox":
        emit_skipped(f"ABX_RUNTIME={config.ABX_RUNTIME}")
        sys.exit(PROCESS_EXIT_SKIPPED)

    if config.SEARCH_BACKEND_ENGINE != "sonic":
        emit_skipped(f"SEARCH_BACKEND_ENGINE={config.SEARCH_BACKEND_ENGINE}")
        sys.exit(PROCESS_EXIT_SKIPPED)

    if not config.USE_INDEXING_BACKEND:
        emit_skipped("USE_INDEXING_BACKEND=False")
        sys.exit(PROCESS_EXIT_SKIPPED)

    host = config.SEARCH_BACKEND_SONIC_HOST_NAME
    port = int(config.SEARCH_BACKEND_SONIC_PORT)
    if is_port_listening(host, port):
        emit_listening_summary(host, port)
        sys.exit(0)

    daemon_event = prepare_sonic_daemon(config, crawl_dir=CRAWL_DIR)
    sys.stdout.write(daemon_event.to_json() + "\n")
    emit_listening_summary(host, port)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
