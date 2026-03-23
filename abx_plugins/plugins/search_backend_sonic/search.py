#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "sonic-client",
#   "abx-plugins",
# ]
# [tool.uv.sources]
# abx-plugins = { path = "../../..", editable = true }
# ///
#
# Sonic search backend - search and flush operations.
#
# This module provides the search interface for the Sonic backend.

from importlib import import_module
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from abx_plugins.plugins.base.utils import load_config


CONFIG_PATH = Path(__file__).with_name("config.json")


def search(query: str) -> list[str]:
    """Search for snapshots in Sonic."""
    try:
        sonic = import_module("sonic")
    except ModuleNotFoundError:
        raise RuntimeError("sonic-client not installed. Run: pip install sonic-client")
    search_client_cls: Any = sonic.SearchClient

    config = load_config(CONFIG_PATH)

    with search_client_cls(
        config.SEARCH_BACKEND_SONIC_HOST_NAME,
        config.SEARCH_BACKEND_SONIC_PORT,
        config.SEARCH_BACKEND_SONIC_PASSWORD,
    ) as search_client:
        results = search_client.query(
            config.SEARCH_BACKEND_SONIC_COLLECTION,
            config.SEARCH_BACKEND_SONIC_BUCKET,
            query,
            limit=100,
        )
        return results


def flush(snapshot_ids: Iterable[str]) -> None:
    """Remove snapshots from Sonic index."""
    try:
        sonic = import_module("sonic")
    except ModuleNotFoundError:
        raise RuntimeError("sonic-client not installed. Run: pip install sonic-client")
    ingest_client_cls: Any = sonic.IngestClient

    config = load_config(CONFIG_PATH)

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
