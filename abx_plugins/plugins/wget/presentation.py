"""Replay roots for Wget's saved host directory layout."""

from pathlib import Path
from urllib.parse import urlsplit


def replay_roots(snapshot_root: Path, url: str) -> tuple[str, ...]:
    parsed = urlsplit(url)
    if not parsed.hostname:
        return ()
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if parsed.port is not None and (parsed.scheme, parsed.port) not in {
        ("http", 80),
        ("https", 443),
    }:
        # The default Windows-compatible filenames use + before the port;
        # captures with Unix filename rules keep the literal colon.
        return (f"wget/{host}+{parsed.port}", f"wget/{host}:{parsed.port}")
    return (f"wget/{host}",)
