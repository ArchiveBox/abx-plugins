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
from contextlib import closing

from abx_plugins.plugins.base.search_candidates import (
    Candidate,
    Evidence,
    SearchBudget,
    SearchTerm,
    id_spellings,
)
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


def ensure_snapshot_lookup(conn: sqlite3.Connection) -> None:
    """Backfill exact identities once, within the caller's write transaction.

    FTS5 equality predicates scan stored document bodies. A separate B-tree
    avoids that scan without depending on how the configured tokenizer treats
    snapshot IDs (including punctuation-only IDs).
    """
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='search_index_lookup'",
    ).fetchone():
        return
    conn.execute(
        "CREATE TABLE search_index_lookup (document_rowid INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL)",
    )
    conn.execute(
        "CREATE INDEX search_index_lookup_snapshot_id ON search_index_lookup(snapshot_id)",
    )
    conn.execute(
        # This plugin creates a normal FTS5 table with snapshot_id first.
        # Reading its documented content columns avoids FTS5 materializing
        # every full document just to backfill the identity lookup.
        "INSERT INTO search_index_lookup SELECT id,c0 FROM search_index_content",
    )


def delete_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> None:
    conn.execute(
        "DELETE FROM search_index WHERE rowid IN "
        "(SELECT document_rowid FROM search_index_lookup WHERE snapshot_id=?)",
        (snapshot_id,),
    )
    conn.execute(
        "DELETE FROM search_index_lookup WHERE snapshot_id=?",
        (snapshot_id,),
    )


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


def _document_ids(conn, snapshot_id, has_lookup):
    identities = id_spellings(snapshot_id)
    slots = ",".join("?" for _ in identities)
    if has_lookup:
        return conn.execute(
            f"SELECT document_rowid FROM search_index_lookup WHERE snapshot_id IN ({slots}) LIMIT 65",
            identities,
        )
    # Older indexes lack the identity B-tree. Restrict the search to the
    # existing FTS postings and verify stored IDs; never backfill on a read.
    words = " OR ".join(
        'snapshot_id:"' + key.replace('"', '""') + '"' for key in identities
    )
    return conn.execute(
        f"SELECT rowid FROM search_index WHERE search_index MATCH ? AND snapshot_id IN ({slots}) LIMIT 65",
        (words, *identities),
    )


def _match_literals(conn, doc, terms, evidence, budget):
    pending = [term for term in terms if term.key not in evidence.matches]
    if not pending:
        return True
    # The existing FTS5 schema also indexes snapshot_id, URL and title. Read
    # each available column as a bounded blob; NULL fields have no bytes.
    columns = ("c3", "c0", "c1", "c2")
    present = conn.execute(
        "SELECT c3 IS NOT NULL,c0 IS NOT NULL,c1 IS NOT NULL,c2 IS NOT NULL "
        "FROM search_index_content WHERE id=?",
        (doc,),
    ).fetchone()
    if not present:
        return False
    complete = True
    remaining = budget.per_file
    for column, exists in zip(columns, present, strict=True):
        if not exists:
            continue
        if not budget.remaining():
            raise TimeoutError
        if budget.streaming:
            with conn.blobopen(
                "search_index_content",
                column,
                doc,
                readonly=True,
            ) as blob:
                length = len(blob)
            offset = 0

            def read(size):
                # Release the SQLite read transaction before waiting for the
                # next I/O window, so an index writer can commit between chunks.
                nonlocal offset
                with conn.blobopen(
                    "search_index_content",
                    column,
                    doc,
                    readonly=True,
                ) as blob:
                    blob.seek(offset)
                    data = blob.read(size)
                offset += len(data)
                return data

            complete &= budget.match_stream(read, length, pending, evidence.matches)
            pending = [term for term in pending if term.key not in evidence.matches]
            if not pending:
                break
            continue
        with conn.blobopen("search_index_content", column, doc, readonly=True) as blob:
            count = min(len(blob), budget.bytes, remaining)
            text = blob.read(count).decode("utf-8", "surrogateescape")
            complete &= count == len(blob)
        remaining -= count
        budget.bytes -= count
        for term in pending:
            if not budget.remaining():
                raise TimeoutError
            if term.pattern.search(text):
                evidence.matches[term.key] = True
        pending = [term for term in pending if term.key not in evidence.matches]
        if not pending:
            break
    return complete


def search_candidates(
    candidates: list[Candidate],
    terms: list[SearchTerm],
    budget: SearchBudget,
):
    path = get_db_path()
    if not path.is_file():
        return
    indexed = [
        term for term in terms if term.mode == "word" and not term.case_sensitive
    ]
    literals = [term for term in terms if term.mode != "deep" and term not in indexed]
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
    except sqlite3.OperationalError:
        return
    with closing(connection) as conn:
        conn.set_progress_handler(lambda: int(not budget.remaining()), 1000)
        try:
            has_lookup = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='search_index_lookup'",
            ).fetchone()
        except sqlite3.OperationalError:
            # A locked or unavailable schema cannot establish any predicates.
            return
        for candidate in candidates:
            if not budget.remaining():
                return
            evidence = Evidence(candidate.id)
            try:
                docs = [row[0] for row in _document_ids(conn, candidate.id, has_lookup)]
                covered = bool(docs) and len(docs) <= 64
                complete_text = covered
                for doc in docs[:64]:
                    if not budget.remaining():
                        raise TimeoutError
                    for term in indexed:
                        if not budget.remaining():
                            raise TimeoutError
                        # An operand is one quoted FTS phrase, never FTS syntax.
                        expression = '"' + term.value.replace('"', '""') + '"'
                        hit = conn.execute(
                            "SELECT bm25(search_index) FROM search_index WHERE rowid=? AND search_index MATCH ?",
                            (doc, expression),
                        ).fetchone()
                        if hit:
                            evidence.matches[term.key] = True
                            evidence.scores[term.key] = max(
                                evidence.scores.get(term.key, 0),
                                -hit[0],
                            )
                    complete_text &= _match_literals(
                        conn,
                        doc,
                        literals,
                        evidence,
                        budget,
                    )
                if covered:
                    for term in indexed:
                        evidence.matches.setdefault(term.key, False)
                if complete_text:
                    for term in literals:
                        evidence.matches.setdefault(term.key, False)
            except (sqlite3.OperationalError, TimeoutError):
                # Preserve any positives already established. An unavailable
                # document leaves this candidate unknown; others may be readable.
                pass
            yield evidence


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
        conn.execute("BEGIN IMMEDIATE")
        ensure_snapshot_lookup(conn)
        for snapshot_id in snapshot_ids:
            delete_snapshot(conn, snapshot_id)
        conn.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(
        run_search_command(search, flush, search_candidates=search_candidates),
    )
