"""Patch forum-dl 0.3.0 bugs from inside its own interpreter.

The forumdl hook puts this directory on forum-dl's PYTHONPATH, so Python
imports this module at startup and forum-dl itself still runs as an ordinary
black-box executable. Each patch below delegates to forum-dl's own code and
only corrects the buggy result, so a forum-dl release that fixes the bug makes
the patch a no-op. Delete a patch once the pinned forum-dl includes its fix.
"""

from urllib.parse import urljoin, urlparse

# This module runs inside forum-dl's interpreter, not abx-plugins'.
from forum_dl.extractors.common import normalize_url  # pyright: ignore[reportMissingImports]  # ty: ignore[unresolved-import]
from forum_dl.extractors.phpbb import PhpbbExtractor  # pyright: ignore[reportMissingImports]  # ty: ignore[unresolved-import]
from forum_dl.writers.jsonl import JsonlWriter  # pyright: ignore[reportMissingImports]  # ty: ignore[unresolved-import]


# forum-dl 0.3.0 was written for Pydantic 1, but config.json installs Pydantic 2,
# where its JSONL writer's entry.json(models_as_dict=False) no longer works.
# serialize_as_any keeps the fields of each entry's concrete subclass.
def _serialize_entry(self, entry):
    return entry.model_dump_json(serialize_as_any=True)


JsonlWriter._serialize_entry = _serialize_entry


# phpBB is forum-dl's last-resort extractor: it fetches <url>/viewforum.php and
# accepts any page whose <html> has a dir attribute, which nearly every site
# has. Facebook redirects that unknown subpath to the post's canonical URL, so
# forum-dl "detected" a phpBB board there and then crashed with a bare
# ValueError mapping the requested post URL to a node of that board.
# A real phpBB answers viewforum.php itself (an error page when no forum id is
# given), and every phpBB page links its user control panel, ucp.php.
_upstream_phpbb_detect = PhpbbExtractor._detect


def _detect_phpbb(session, url, options):
    # Validate the probe before upstream constructs a board and fetches its
    # root URL. Non-forum pages can pass its loose HTML check, then fail during
    # construction before a post-construction check could reject them.
    # Upstream reuses this cached response without making another request.
    with session.try_get(
        urljoin(
            normalize_url(url, remove_suffixes=["viewforum.php", "viewtopic.php"]),
            "viewforum.php",
        ),
        should_cache=True,
        should_retry=False,
        stream=True,
    ) as probe:
        if not urlparse(probe.url).path.endswith("/viewforum.php"):
            return None
        # This is a document-discovery probe, not an attachment download.
        # Reject explicit binary types before requests expands compressed bodies
        # and guesses their text encoding. Missing/text MIME types retain the
        # existing HTML checks, including legacy text/plain forum pages.
        content_type = (
            probe.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if content_type and not (
            content_type.startswith("text/") or content_type == "application/xhtml+xml"
        ):
            return None
        if "ucp.php" not in probe.text:
            return None
        return _upstream_phpbb_detect(session, url, options)


PhpbbExtractor._detect = staticmethod(_detect_phpbb)
