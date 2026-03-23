#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "abx-plugins",
# ]
# [tool.uv.sources]
# abx-plugins = { path = "../../..", editable = true }
# ///
"""
SQLite FTS5 search backend - search and flush operations.

This module provides the search interface for the SQLite FTS backend.
"""

from collections.abc import Iterable
from pathlib import Path
import sqlite3

from abx_plugins.plugins.base.utils import load_config


CONFIG_PATH = Path(__file__).with_name("config.json")


def get_db_path() -> Path:
    """Get path to the shared collection search index database."""
    config = load_config(CONFIG_PATH)
    data_dir = Path(config.DATA_DIR or Path.cwd()).resolve()
    return data_dir / config.SEARCH_BACKEND_SQLITE_DB


def search(query: str) -> list[str]:
    """Search for snapshots matching the query."""
    db_path = get_db_path()
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


def flush(snapshot_ids: Iterable[str]) -> None:
    """Remove snapshots from the index."""
    db_path = get_db_path()
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
