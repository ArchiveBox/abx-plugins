#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "abx-plugins",
# ]
# ///
"""
Request that ArchiveBox ensure the Sonic worker is running.
"""

import json
import os
import socket
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    PROCESS_EXIT_SKIPPED,
    load_config,
    write_text_atomic,
)


PLUGIN_DIR = Path(__file__).parent.name
CONFIG = load_config()
CRAWL_DIR = Path(CONFIG.CRAWL_DIR or ".").resolve()
OUTPUT_DIR = CRAWL_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def is_port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def get_sonic_dir(config) -> Path:
    if config.SONIC_DIR:
        return Path(config.SONIC_DIR).expanduser().resolve()
    if config.DATA_DIR:
        return (Path(config.DATA_DIR).expanduser().resolve() / "sonic").resolve()
    return (CRAWL_DIR / "sonic").resolve()


def build_config_text(config, sonic_dir: Path) -> str:
    kv_dir = (sonic_dir / "store" / "kv").resolve()
    fst_dir = (sonic_dir / "store" / "fst").resolve()
    return "\n".join(
        [
            "[server]",
            'log_level = "error"',
            "",
            "[channel]",
            f'inet = "{config.SEARCH_BACKEND_SONIC_HOST_NAME}:{config.SEARCH_BACKEND_SONIC_PORT}"',
            "tcp_timeout = 300",
            f'auth_password = "{config.SEARCH_BACKEND_SONIC_PASSWORD}"',
            "",
            "[channel.search]",
            "query_limit_default = 10",
            "query_limit_maximum = 100",
            "query_alternates_try = 4",
            "suggest_limit_default = 5",
            "suggest_limit_maximum = 20",
            "list_limit_default = 100",
            "list_limit_maximum = 500",
            "",
            "[store]",
            "",
            "[store.kv]",
            f'path = "{kv_dir.as_posix()}/"',
            "retain_word_objects = 1000",
            "",
            "[store.kv.pool]",
            "inactive_after = 1800",
            "",
            "[store.kv.database]",
            "flush_after = 900",
            "compress = true",
            "parallelism = 2",
            "max_files = 100",
            "max_compactions = 1",
            "max_flushes = 1",
            "write_buffer = 16384",
            "write_ahead_log = true",
            "",
            "[store.fst]",
            f'path = "{fst_dir.as_posix()}/"',
            "",
            "[store.fst.pool]",
            "inactive_after = 300",
            "",
            "[store.fst.graph]",
            "consolidate_after = 180",
            "max_size = 2048",
            "max_words = 250000",
            "",
        ],
    )


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

    sonic_dir = get_sonic_dir(config)
    (sonic_dir / "store" / "kv").mkdir(parents=True, exist_ok=True)
    (sonic_dir / "store" / "fst").mkdir(parents=True, exist_ok=True)
    config_path = sonic_dir / "config.cfg"
    write_text_atomic(config_path, build_config_text(config, sonic_dir))

    record = {
        "type": "ProcessEvent",
        "plugin_name": PLUGIN_DIR,
        "hook_name": "worker_sonic",
        "hook_path": config.SONIC_BINARY,
        "hook_args": ["-c", str(config_path)],
        "is_background": True,
        "daemon": True,
        "url": f"tcp://{host}:{port}",
        "output_dir": str(sonic_dir),
        "env": {},
        "timeout": 0,
        "process_type": "worker",
        "worker_type": "sonic",
        "process_id": "worker:sonic",
        "event_timeout": 120.0,
        "event_handler_timeout": 150.0,
    }
    sys.stdout.write(json.dumps(record) + "\n")
    emit_listening_summary(host, port)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
