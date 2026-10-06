"""Standalone CLI contract shared by search-capable plugins."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterable, Sequence


def run_search_command(
    search: Callable[[str, str], Iterable[str]],
    flush: Callable[[Iterable[str]], None],
    argv: Sequence[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Run a plugin search command")
    subparsers = parser.add_subparsers(dest="command", required=True)
    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--search-mode", default="contents")
    flush_parser = subparsers.add_parser("flush")
    flush_parser.add_argument("snapshot_ids", nargs="*")
    args = parser.parse_args(argv)

    if args.command == "search":
        for snapshot_id in search(args.query, args.search_mode):
            print(str(snapshot_id), flush=True)
        return 0

    snapshot_ids = [
        *args.snapshot_ids,
        *(line.strip() for line in sys.stdin if line.strip()),
    ]
    flush(snapshot_ids)
    return 0
