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

    streaming: bool = False
    _window: float = field(default_factory=time.monotonic)
    _last_progress: float = field(default_factory=time.monotonic)
    _spent: dict[str, int] = field(default_factory=dict)
    consumed: dict[str, int] = field(default_factory=dict)

    def take(self, name: str, requested: int) -> int:
        """Charge one chunk. Connected searches keep their cursor between I/O windows."""
        if self.streaming:
            self.report_progress()
        limit = getattr(self, name)
        if not self.streaming:
            count = min(requested, limit)
            setattr(self, name, limit - count)
            return count
        if not limit or not requested:
            return 0
        if self._spent.get(name, 0) >= limit:
            time.sleep(max(0, self._window + 1 - time.monotonic()))
            self._spent.clear()
            self._window = time.monotonic()
        count = min(requested, limit - self._spent.get(name, 0))
        self._spent[name] = self._spent.get(name, 0) + count
        self.consumed[name] = self.consumed.get(name, 0) + count
        return count

    def match_stream(self, read, length, terms, matches):
        # Keep enough overlap for a literal split across reads, including UTF-8
        # characters. Each file/FTS column starts with independent overlap.
        tail = b""
        overlap = 4 * max(len(term.value) for term in terms)
        while length:
            size = self.take("bytes", min(length, self.per_file))
            if not size or not (data := read(size)):
                return False
            length -= len(data)
            combined = tail + data
            text = combined.decode("utf-8", "surrogateescape")
            for term in terms:
                if term.key not in matches and term.pattern.search(text):
                    matches[term.key] = True
            if all(term.key in matches for term in terms):
                return True
            tail = combined[-overlap:]
        return True

    def report_progress(self):
        now = time.monotonic()
        if now - self._last_progress >= 2:
            print(json.dumps({"progress": True}), flush=True)
            self._last_progress = now

    def remaining(self) -> float:
        if self.streaming:
            self.report_progress()
            return (
                8.0  # individual socket/command timeout, independent of scan duration
            )
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
        streaming=data.get("streaming") is True,
    )
    counters = ("files", "entries", "bytes")
    before = {key: 0 if budget.streaming else getattr(budget, key) for key in counters}
    for evidence in search(candidates, terms, budget):
        if budget.streaming:
            after = {key: budget.consumed.get(key, 0) for key in counters}
            usage = {key: after[key] - before[key] for key in counters}
        else:
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
