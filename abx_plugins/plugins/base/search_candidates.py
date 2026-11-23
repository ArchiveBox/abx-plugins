"""Types and bounds for the optional, read-only search_candidates command."""

from __future__ import annotations

import json
import math
import re
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path


@dataclass(frozen=True)
class SearchTerm:
    key: str
    value: str
    mode: str

    @cached_property
    def case_sensitive(self) -> bool:
        # Only the value of this operand controls its case mode.
        return any(char.isupper() for char in self.value)

    @cached_property
    def pattern(self) -> re.Pattern:
        return re.compile(
            re.escape(self.value),
            0 if self.case_sensitive else re.IGNORECASE,
        )


@dataclass(frozen=True)
class Candidate:
    id: str
    path: Path | None
    archive_root: Path | None = None


def id_spellings(value: str) -> tuple[str, ...]:
    """Old and new collections can store the same UUID in either spelling."""
    try:
        uid = uuid.UUID(value)
    except ValueError:
        return (value,)
    return tuple(dict.fromkeys((value, str(uid), uid.hex)))


@dataclass
class SearchBudget:
    deadline: float
    files: int
    entries: int
    bytes: int
    per_file: int

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


@dataclass
class Evidence:
    id: str
    matches: dict[str, bool] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)


CandidateSearch = Callable[
    [list[Candidate], list[SearchTerm], SearchBudget],
    Iterable[Evidence],
]


def run_candidates(search: CandidateSearch) -> int:
    # Black-box executables share the same JSONL protocol. No host or ORM is
    # imported, and no user-supplied value becomes a command-line option.
    raw = sys.stdin.buffer.readline(512 * 1024 + 1)
    if len(raw) > 512 * 1024:
        raise ValueError("Search request exceeds 512 KiB")
    data = json.loads(raw)
    archive_root = Path(data["archive_root"]) if "archive_root" in data else None
    if archive_root and (not archive_root.is_absolute() or ".." in archive_root.parts):
        raise ValueError("Expected an absolute archive root without parent references")
    candidates = []
    if len(data["candidates"]) > 64 or len(data["terms"]) > 128:
        raise ValueError("At most 64 candidates and 128 operands per request")
    for row in data["candidates"]:
        path = Path(row["path"]) if row.get("path") else None
        if (
            not isinstance(row["id"], str)
            or not row["id"]
            or (path and not path.is_absolute())
        ):
            raise ValueError(
                "Expected a snapshot ID and optional absolute snapshot path",
            )
        candidates.append(Candidate(row["id"], path, archive_root))
    terms = [
        SearchTerm(item["key"], item["value"], item["mode"]) for item in data["terms"]
    ]
    if any(
        not isinstance(term.key, str)
        or not isinstance(term.value, str)
        or not (0 < len(term.value) <= 4096)
        or term.mode not in {"deep", "literal", "word"}
        for term in terms
    ):
        raise ValueError("Invalid search operand")
    if len({term.key for term in terms}) != len(terms):
        raise ValueError("Expected unique operand keys")
    limits = data.get("limits", {})

    def limit(name: str, maximum: int) -> int:
        return max(0, min(maximum, int(limits.get(name, maximum))))

    seconds = float(limits.get("seconds", 8))
    if not math.isfinite(seconds):
        raise ValueError("Invalid search timeout")
    budget = SearchBudget(
        deadline=time.monotonic() + min(8, max(0, seconds)),
        files=limit("files", 96),
        entries=limit("entries", 512),
        bytes=limit("bytes", 4 * 1024**2),
        per_file=limit("per_file", 512 * 1024),
    )
    counters = ("files", "entries", "bytes")
    before = {key: getattr(budget, key) for key in counters}
    for evidence in search(candidates, terms, budget):
        after = {key: getattr(budget, key) for key in counters}
        usage = {key: before[key] - after[key] for key in counters}
        print(
            json.dumps(
                {
                    "id": evidence.id,
                    "matches": evidence.matches,
                    "scores": evidence.scores,
                    "usage": usage,
                },
            ),
            flush=True,
        )
        before = after
    return 0
