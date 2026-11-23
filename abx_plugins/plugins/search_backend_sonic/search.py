#!/usr/bin/env -S abxpkg run --script python3
# /// script
# requires-python = ">=3.12"
# ///
#
# Sonic search backend - search and flush operations.
#
# This module provides the search interface for the Sonic backend.

from importlib import import_module
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from abx_plugins.plugins.base.search_candidates import (
    Candidate,
    Evidence,
    SearchBudget,
    SearchTerm,
    id_spellings,
)
from abx_plugins.plugins.base.search_command import run_search_command
from abx_plugins.plugins.base.utils import load_config


CONFIG_PATH = Path(__file__).with_name("config.json")


def search(
    query: str,
    search_mode: str = "contents",
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float | None = None,
    offset: int = 0,
) -> list[str]:
    """Search for snapshots in Sonic."""
    del search_mode
    try:
        sonic = import_module("sonic")
    except ModuleNotFoundError:
        raise RuntimeError("sonic-client not installed. Run: pip install sonic-client")
    search_client_cls: Any = sonic.SearchClient

    # Network clients need connection settings, not local Sonic server discovery.
    config = load_config(CONFIG_PATH, environ=environ, hydrate_binaries=False)

    client = search_client_cls(
        config.SEARCH_BACKEND_SONIC_HOST_NAME,
        config.SEARCH_BACKEND_SONIC_PORT,
        config.SEARCH_BACKEND_SONIC_PASSWORD,
    )
    protocol = import_module("sonic.client")
    if timeout is not None:
        # sonic-client opens sockets through its pool. Assigning a timeout to
        # SearchClient itself leaves new connections at the 60-second default.
        client.pool = protocol.ConnectionPool(
            host=client.host,
            port=client.port,
            password=config.SEARCH_BACKEND_SONIC_PASSWORD,
            channel=client.channel,
            timeout=timeout,
        )
    try:
        with client:
            return client.query(
                config.SEARCH_BACKEND_SONIC_COLLECTION,
                config.SEARCH_BACKEND_SONIC_BUCKET,
                query,
                limit=100,
                offset=offset,
            )
    except (protocol.SonicServerError, protocol.ChannelError) as err:
        raise ConnectionError("Sonic could not complete the search") from err


def search_candidates(
    candidates: list[Candidate],
    terms: list[SearchTerm],
    budget: SearchBudget,
):
    # A capped Sonic QUERY provides ranked positive evidence. Its omission of
    # an ID says nothing about coverage or absence. Sonic also discards case
    # and token positions, so it cannot verify exact substrings.
    rows = {candidate.id: Evidence(candidate.id) for candidate in candidates}
    aliases = {
        alias: candidate.id
        for candidate in candidates
        for alias in id_spellings(candidate.id)
    }
    for term in terms:
        if not budget.remaining():
            break
        if term.mode != "word" or term.case_sensitive:
            continue
        try:
            offset = 0
            while True:
                results = search(
                    term.value,
                    timeout=max(0.001, budget.remaining()),
                    offset=offset,
                )
                for rank, found in enumerate(results, start=offset):
                    candidate_id = aliases.get(found)
                    if candidate_id is not None:
                        rows[candidate_id].matches[term.key] = True
                        rows[candidate_id].scores[term.key] = 1 / (1 + rank)
                if (
                    not budget.streaming
                    or len(results) < 100
                    or all(term.key in row.matches for row in rows.values())
                ):
                    break
                offset += len(results)
        except (OSError, ValueError):
            break
    yield from rows.values()


def flush(
    snapshot_ids: Iterable[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Remove snapshots from Sonic index."""
    try:
        sonic = import_module("sonic")
    except ModuleNotFoundError:
        raise RuntimeError("sonic-client not installed. Run: pip install sonic-client")
    ingest_client_cls: Any = sonic.IngestClient

    # The server may be remote; resolving its local executable adds work per call.
    config = load_config(CONFIG_PATH, environ=environ, hydrate_binaries=False)

    with ingest_client_cls(
        config.SEARCH_BACKEND_SONIC_HOST_NAME,
        config.SEARCH_BACKEND_SONIC_PORT,
        config.SEARCH_BACKEND_SONIC_PASSWORD,
    ) as ingest:
        for snapshot_id in snapshot_ids:
            try:
                ingest.flush_object(
                    config.SEARCH_BACKEND_SONIC_COLLECTION,
                    config.SEARCH_BACKEND_SONIC_BUCKET,
                    snapshot_id,
                )
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(
        run_search_command(search, flush, search_candidates=search_candidates),
    )
