"""ArchiveBox collection service; indexing and search remain standalone commands."""

import os
import shlex
from functools import partial
from pathlib import Path

from .daemon import (
    config_value,
    get_sonic_dir,
    get_sonic_supervisord_worker,
    is_port_listening,
    is_sonic_backend_enabled,
    sonic_daemon_bind_host,
)


def prepare_worker(config):
    values = dict(config) if isinstance(config, dict) else vars(config).copy()
    explicit_binary = os.environ.get("SONIC_BINARY")
    requested = Path(
        explicit_binary or str(config_value(config, "SONIC_BINARY", "sonic")),
    ).expanduser()
    if explicit_binary:
        values["SONIC_BINARY"] = str(requested)
    worker = get_sonic_supervisord_worker(values)
    if worker is not None and requested.is_absolute():
        command = shlex.split(worker["command"])
        command[0] = str(requested.resolve(strict=True))
        worker["command"] = shlex.join(command)
    return worker


def get_services(host, config):
    if not is_sonic_backend_enabled(config):
        return ()
    host = sonic_daemon_bind_host(config)
    port = int(config_value(config, "SEARCH_BACKEND_SONIC_PORT", 1491))
    return (
        {
            "name": "worker_sonic",
            "label": "sonic",
            "prepare": partial(prepare_worker, config),
            "ready": partial(is_port_listening, host, port),
            "bind": (host, port),
            "process_name": Path(
                os.environ.get("SONIC_BINARY")
                or str(config_value(config, "SONIC_BINARY", "sonic")),
            ).name,
            "identity": (str(get_sonic_dir(config) / "config.cfg"),),
        },
    )
