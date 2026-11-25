#!/usr/bin/env -S abxpkg run --script python3
# /// script
# requires-python = ">=3.12"
# ///
"""Plugin-owned semantic index, using the standard scoped search command."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from uuid import UUID

from abx_plugins.plugins.base.search_candidates import (
    Candidate,
    Evidence,
    SearchBudget,
    SearchTerm,
)
from abx_plugins.plugins.base.search_command import run_search_command

PLUGIN_DIR = Path(__file__).resolve().parent
MATCH_THRESHOLD = 0.35


class RuntimeUnavailable(Exception):
    """Local model or runtime unavailable, busy or over request budget."""


def enabled() -> bool:
    return os.environ.get("SEARCH_BACKEND_WORD2VEC_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def model_selection() -> tuple[str, str]:
    preset = os.environ.get("SEARCH_BACKEND_WORD2VEC_MODEL", "MiniLM")
    if preset == "MiniLM":
        return "minilm", ""
    if preset == "BrowserVec built-in":
        return "browservec", ""
    if preset == "Custom Hugging Face":
        model = os.environ.get("SEARCH_BACKEND_WORD2VEC_HUGGINGFACE_MODEL", "").strip()
        if re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", model
        ):
            return "huggingface", model
    raise RuntimeUnavailable(
        "Choose a model and provide a public owner/model ID for Custom Hugging Face."
    )


def index_root() -> Path:
    return Path(os.environ.get("DATA_DIR") or ".").resolve() / "search_backend_word2vec"


def index_dir() -> Path:
    preset, model_id = model_selection()
    key = hashlib.sha256(model_id.encode()).hexdigest() if model_id else preset
    return index_root() / key


def runtime_dir() -> Path:
    return index_dir() / "runtime"


def dimension(runtime: Path) -> int:
    if model_selection()[0] == "browservec":
        return 384
    value = json.loads((runtime / "model/config.json").read_text())["hidden_size"]
    if type(value) is not int or not 1 <= value <= 4096:
        raise RuntimeUnavailable("Unsupported embedding dimension")
    return value


def invoke(payload, budget: float):
    runtime = runtime_dir()
    if budget <= 0:
        raise RuntimeUnavailable
    deadline = time.monotonic() + budget
    try:
        # Bound concurrency per collection rather than keeping a resident model.
        with (index_root() / "runtime.lock").open("a+b") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if deadline <= time.monotonic():
                        raise RuntimeUnavailable from None
                    time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
            timeout = deadline - time.monotonic()
            if timeout <= 0 or not (runtime / "ready").is_file():
                raise RuntimeUnavailable
            payload.update(preset=model_selection()[0], dimension=dimension(runtime))
            result = subprocess.run(
                [
                    os.environ.get("NODE_BINARY", "node"),
                    str(PLUGIN_DIR / "runner.mjs"),
                    str(runtime),
                ],
                input=json.dumps(payload),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=timeout,
            )
            return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        raise RuntimeUnavailable from None


def index_id(value: str) -> str:
    try:
        return UUID(value).hex
    except ValueError:
        return value


def index_text(snapshot_id: str, text: str) -> None:
    from abx_plugins.plugins.search_backend_word2vec.setup_runtime import ensure_runtime

    ensure_runtime()
    directory = index_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    vector = base64.b64decode(
        invoke({"operation": "embed", "texts": [text[:4096]]}, 8.0)
    )
    if len(vector) != dimension(runtime_dir()) * 4:
        raise RuntimeUnavailable
    with closing(sqlite3.connect(directory / "index.sqlite3", timeout=2)) as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS embeddings (id TEXT PRIMARY KEY, vector BLOB NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO embeddings VALUES (?,?)",
            (index_id(snapshot_id), vector),
        )


def search_candidates(
    candidates: list[Candidate], terms: list[SearchTerm], budget: SearchBudget
):
    if not enabled() or not candidates or not terms:
        return
    path = index_dir() / "index.sqlite3"
    if not path.is_file():
        return
    aliases = {index_id(item.id): item.id for item in candidates}
    records = {}
    try:
        vector_bytes = dimension(runtime_dir()) * 4
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
        ) as conn:
            slots = ",".join("?" for _ in aliases)
            for ident, vector in conn.execute(
                f"SELECT id, vector FROM embeddings WHERE id IN ({slots})",
                tuple(aliases),
            ):
                if (
                    len(vector) == vector_bytes
                    and budget.take("bytes", vector_bytes) == vector_bytes
                ):
                    records[aliases[ident]] = base64.b64encode(vector).decode("ascii")
        if not records:
            return
        evidence = {ident: Evidence(ident) for ident in records}
        response = invoke(
            {
                "operation": "rank",
                "texts": [term.value for term in terms],
                "records": list(records.items()),
            },
            budget.remaining(),
        )
        for term, hits in zip(terms, response, strict=True):
            for hit in hits:
                target = evidence.get(hit["id"])
                score = float(hit["score"])
                if target is not None and math.isfinite(score):
                    score = max(0.0, min(1.0, score))
                    target.matches[term.key] = score >= MATCH_THRESHOLD
                    target.scores[term.key] = score
        yield from evidence.values()
    except (sqlite3.Error, RuntimeUnavailable):
        return


def rank(query: str, ids: list[str] | None, limit: int = 256):
    """Rank vetted scope IDs in a single bounded model invocation."""
    if not enabled() or not query.strip() or ids == []:
        return []
    path = index_dir() / "index.sqlite3"
    if not path.is_file():
        return []
    return invoke(
        {
            "operation": "scan",
            "database": str(path),
            "ids": ids,
            "texts": [query[:4096]],
            "limit": max(1, min(1024, limit)),
        },
        8.0,
    )


def search(query: str, _mode: str):
    """Standalone backend selection uses the same bounded ranker."""
    try:
        for hit in rank(query, None):
            score = float(hit["score"])
            if math.isfinite(score) and score >= MATCH_THRESHOLD:
                yield hit["id"]
    except RuntimeUnavailable:
        return


def scoped_rank_command() -> int:
    # Exact IDs from the host's already authorized, filtered queryset, not a browser assertion.
    raw = sys.stdin.buffer.readline(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError("Scoped search request exceeds 5 MiB")
    request = json.loads(raw)
    ids, query = request["ids"], request["query"]
    if (
        not isinstance(ids, list)
        or len(ids) > 65_536
        or any(not isinstance(item, str) or not 0 < len(item) <= 128 for item in ids)
    ):
        raise ValueError("Expected at most 65,536 snapshot IDs")
    if not isinstance(query, str) or not 0 < len(query) <= 4096:
        raise ValueError("Expected a non-empty search operand")
    allowed = set(ids)
    for hit in rank(query, ids, int(request.get("limit", 256))):
        ident, score = hit["id"], float(hit["score"])
        if ident in allowed and math.isfinite(score) and score >= MATCH_THRESHOLD:
            print(json.dumps({"id": ident, "score": min(1.0, score)}), flush=True)
    return 0


def flush(snapshot_ids: Iterable[str]) -> None:
    ids = tuple(dict.fromkeys(map(index_id, snapshot_ids)))
    if not ids:
        return
    for path in index_root().glob("*/index.sqlite3"):
        with closing(sqlite3.connect(path, timeout=2)) as conn, conn:
            conn.executemany(
                "DELETE FROM embeddings WHERE id=?", ((item,) for item in ids)
            )


if __name__ == "__main__":
    if sys.argv[1:] == ["search_scope"]:
        raise SystemExit(scoped_rank_command())
    raise SystemExit(
        run_search_command(search, flush, search_candidates=search_candidates)
    )
