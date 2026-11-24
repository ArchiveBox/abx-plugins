"""ArchiveBox collection service; indexing and search remain standalone commands."""

import logging
import os
import shlex
import time
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


def search_word_pages(word, *, environ, timeout):
    """Enumerate positive postings in native order. Each socket call is bounded."""
    from .search import search

    offset = 0
    while page := search(word, environ=environ, timeout=timeout, offset=offset):
        yield page
        # Some servers return fewer rows than the requested limit. Advance by
        # the actual page and request the next offset until the server says EOF.
        offset += len(page)


def search_word_hits(word, *, environ, timeout):
    """Stream a bounded prefix of ranked positives without reading archive files."""
    from .search import PAGE_SIZE, search

    # The host applies permission and Boolean filters. Its first visible
    # match may be on Sonic's second page. Fetch it only if still consumed.
    deadline = time.monotonic() + timeout
    for offset in range(0, 500, PAGE_SIZE):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            page = search(word, environ=environ, timeout=remaining, offset=offset)
        except OSError:
            # Keep earlier positives if the next network request fails. The
            # host treats every omitted posting as unknown and can continue.
            logging.getLogger(__name__).warning(
                "Sonic word lookup stopped at offset %s",
                offset,
                exc_info=True,
            )
            return
        for snapshot_id in page:
            # Socket timeouts bound individual operations. The consumer may
            # also spend time checking each batch against its active scope.
            if time.monotonic() >= deadline:
                return
            yield snapshot_id
        if len(page) < PAGE_SIZE:
            return


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
