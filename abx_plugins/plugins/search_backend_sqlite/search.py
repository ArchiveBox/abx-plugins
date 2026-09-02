#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""
SQLite FTS5 search backend - search and flush operations.

This module provides the search interface for the SQLite FTS backend.
"""

from collections.abc import Iterable
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import json
import os
import sqlite3

from abx_plugins.plugins.base.search_command import run_search_command


CONFIG_PATH = Path(__file__).with_name("config.json")


@lru_cache(maxsize=1)
def _sqlite_config_properties() -> dict[str, Any]:
    data = json.loads(CONFIG_PATH.read_text())
    properties = data.get("properties") if isinstance(data, dict) else {}
    return dict(properties) if isinstance(properties, dict) else {}


def _coerce_env_value(value: str, prop: Mapping[str, Any]) -> Any:
    prop_type = prop.get("type")
    if prop_type == "boolean":
        return value.strip().lower() not in {"0", "false", "no", "off"}
    if prop_type == "integer":
        try:
            return int(value)
        except ValueError:
            return prop.get("default", 0)
    return value


def load_sqlite_config(environ: Mapping[str, str] | None = None) -> Any:
    """Load SQLite's per-snapshot hot-path config without typed schema imports."""
    env = os.environ if environ is None else environ
    values: dict[str, Any] = {}
    for key, prop in _sqlite_config_properties().items():
        aliases = prop.get("x-aliases") if isinstance(prop, Mapping) else []
        env_keys = [key, *(aliases if isinstance(aliases, list) else [])]
        raw_value = next((env[name] for name in env_keys if name in env), None)
        if raw_value is None:
            values[key] = prop.get("default") if isinstance(prop, Mapping) else ""
        else:
            values[key] = _coerce_env_value(raw_value, prop)
    values.update(DATA_DIR=env.get("DATA_DIR", ""), SNAP_DIR=env.get("SNAP_DIR", ""))
    return SimpleNamespace(**values)


def get_db_path(environ: Mapping[str, str] | None = None) -> Path:
    """Get path to the shared collection search index database."""
    config = load_sqlite_config(environ)
    data_dir = Path(config.DATA_DIR or Path.cwd()).resolve()
    return data_dir / config.SEARCH_BACKEND_SQLITE_DB


def search(
    query: str,
    search_mode: str = "contents",
    *,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Search for snapshots matching the query."""
    del search_mode
    db_path = get_db_path(environ)
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute(
            "SELECT DISTINCT snapshot_id FROM search_index WHERE search_index MATCH ?",
            (query,),
        )
        return [row[0] for row in cursor.fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def flush(
    snapshot_ids: Iterable[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Remove snapshots from the index."""
    db_path = get_db_path(environ)
    if not db_path.exists():
        return

    conn = sqlite3.connect(str(db_path))
    try:
        for snapshot_id in snapshot_ids:
            conn.execute(
                "DELETE FROM search_index WHERE snapshot_id = ?",
                (snapshot_id,),
            )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(run_search_command(search, flush))
