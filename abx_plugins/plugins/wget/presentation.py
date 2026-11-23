"""Replay roots for Wget's saved host directory layout."""

from pathlib import Path
import re
from urllib.parse import urlsplit

_REQUEST_LINE = re.compile(r"^--\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}--\s+(https?://\S+)")
_SAVED_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} .* saved \[(\d+)(?:/\d+)?\]$",
)


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


# GNU Wget's verbose C-locale log records the actual size of each HTTP download.
# Keep the hostname from the request marker so redirects and page requisites
# are attributed to the server that sent the payload.
def completed_downloads(stderr: str) -> list[tuple[str, int]]:
    downloads = []
    hostname = None
    for line in stderr.splitlines():
        if request := _REQUEST_LINE.match(line):
            try:
                hostname = urlsplit(request[1]).hostname
            except ValueError:
                hostname = None
        elif hostname and (saved := _SAVED_LINE.match(line)):
            downloads.append((hostname, int(saved[1])))
            hostname = None
    return downloads
