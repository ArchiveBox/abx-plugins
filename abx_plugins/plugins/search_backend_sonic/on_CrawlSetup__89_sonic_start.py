#!/usr/bin/env -S abxpkg run --script python3
# /// script
# requires-python = ">=3.12"
# ///

import argparse
import os
import sys
import time

from abx_plugins.plugins.search_backend_sonic.daemon import (
    get_sonic_supervisord_worker,
    is_port_listening,
    is_sonic_backend_enabled,
    load_sonic_config,
    prepare_sonic_daemon,
)


def start_archivebox_sonic_worker(config) -> None:
    if os.environ.get("ABX_RUNTIME", "").lower() != "archivebox":
        return

    from archivebox.workers.supervisord_util import (
        get_or_create_supervisord_process,
        start_worker,
    )

    worker = get_sonic_supervisord_worker(config)
    if worker is None:
        return

    supervisor = get_or_create_supervisord_process(daemonize=False)
    start_worker(supervisor, worker, lazy=False)


def wait_until_listening(host: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_port_listening(host, port):
            return
        time.sleep(0.25)
    raise RuntimeError(f"Sonic search backend is not listening at tcp://{host}:{port}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="")
    parser.parse_args()

    config = load_sonic_config()
    if not is_sonic_backend_enabled(config):
        return 0

    daemon_event = prepare_sonic_daemon(config)
    start_archivebox_sonic_worker(config)
    wait_until_listening(daemon_event.host, daemon_event.port)
    print(daemon_event.to_json(), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as err:
        print(f"ERROR: {type(err).__name__}: {err}", file=sys.stderr)
        raise SystemExit(1)
