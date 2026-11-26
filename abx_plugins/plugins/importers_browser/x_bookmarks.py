"""Read the authenticated X bookmark timeline one cursor at a time."""

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

from gallery_dl import config, exception, extractor
from gallery_dl.extractor.twitter import TwitterAPI, TwitterBookmarkExtractor

from abx_plugins.plugins.base.importers import NeedsLogin, emit, run


URL = "https://x.com/i/bookmarks"
CHECKPOINT_VERSION = 2
PAGE_SIZE = 50
TWITTER_CONFIG = ("extractor", "twitter")
HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")


class BookmarkAPI(TwitterAPI):
    cursor: str | None = None

    def _pagination_tweets(self, endpoint, variables, path=None, *args, **kwargs):
        # Reuse gallery-dl's X endpoint, session and request signing. Read one
        # response at a time so the importer controls the durable cursor and
        # can tell a stalled response from the end of the bookmark timeline.
        if self.cursor is not None:
            variables["cursor"] = self.cursor
        body = self._call(
            endpoint,
            {
                "variables": self._json_dumps(variables),
                "features": self._json_dumps(self.features_pagination),
            },
        )
        if body.get("errors"):
            raise ValueError("X returned an error instead of a bookmark page.")
        instructions: object = body.get("data")
        for key in path or ():
            if not isinstance(instructions, dict):
                raise ValueError("X returned no recognizable bookmark timeline.")
            instructions = instructions.get(key)
        if not isinstance(instructions, dict) or not isinstance(
            pages := instructions.get("instructions"),
            list,
        ):
            raise ValueError("X returned no recognizable bookmark timeline.")
        return pages


def session():
    for name, value in {
        "cookies": os.environ.get("COOKIES_FILE") or None,
        "ratelimit": "abort",
        "retries": 0,
        "timeout": 20,
        "sleep-request": 0.75,
    }.items():
        config.set(TWITTER_CONFIG, name, value)
    bookmarks = extractor.find(URL)
    if not isinstance(bookmarks, TwitterBookmarkExtractor):
        raise ValueError("The installed gallery-dl package cannot read X Bookmarks.")
    bookmarks.initialize()

    if auth_file := os.environ.get("AUTH_STORAGE_FILE"):
        # Chrome's persona import gives auth.json precedence over cookies.txt.
        # X needs only cookies for its own domain from the storage export.
        for cookie in json.loads(Path(auth_file).read_text()).get("cookies", []):
            domain = cookie.get("domain", "")
            expires = cookie.get("expires") or -1
            if domain.lstrip(".") == "x.com" and (
                expires <= 0 or expires > time.time()
            ):
                bookmarks.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=domain,
                    path=cookie.get("path", "/"),
                    secure=cookie.get("secure", False),
                    expires=int(expires) if expires > 0 else None,
                )

    twid = bookmarks.cookies.get("twid", domain=bookmarks.cookies_domain)
    user_id = unquote(twid or "").removeprefix("u=")
    if not bookmarks.cookies_check(bookmarks.cookies_names) or not user_id.isdigit():
        raise NeedsLogin("Sync a persona signed in to X and check access again.")
    bookmarks.api = BookmarkAPI(bookmarks)
    bookmarks.login()
    return bookmarks, user_id


def item_from_entry(entry):
    item = entry.get("content", {}).get("itemContent", {})
    identifier = entry["entryId"].removeprefix("tweet-")
    if not identifier.isdigit() or "promotedMetadata" in item:
        return None
    tweet = item.get("tweet_results", {}).get("result") or {}
    tweet = tweet.get("tweet") or tweet
    author = tweet.get("core", {}).get("user_results", {}).get("result") or {}
    handle = (author.get("core") or author.get("legacy") or {}).get("screen_name", "")
    note = tweet.get("note_tweet", {}).get("note_tweet_results", {}).get("result", {})
    legacy = tweet.get("legacy", {})
    title = note.get("text") or legacy.get("full_text") or legacy.get("text") or ""
    url = (
        f"https://x.com/{handle}/status/{identifier}"
        if HANDLE.fullmatch(handle)
        else f"https://x.com/i/status/{identifier}"
    )
    metadata = {"relationship": "bookmarked"}
    if (sort_index := str(entry.get("sortIndex", ""))).isdigit():
        # X stores the bookmark timestamp in the high bits of the timeline
        # sort index. The post publication time is independent of this order.
        bookmarked_at = (int(sort_index) >> 20) / 1000
        if 0 < bookmarked_at <= time.time():
            metadata["bookmarked_at"] = datetime.fromtimestamp(
                bookmarked_at,
                UTC,
            ).isoformat()
    return {
        "type": "ImporterItem",
        "id": identifier,
        "url": url,
        "title": title[:1000],
        "metadata": metadata,
    }


def bookmark_page(bookmarks, cursor, count):
    config.set(TWITTER_CONFIG, "limit", count)
    bookmarks.api.cursor = cursor
    rows = []
    next_cursor = None
    terminal = False
    has_entries = False
    for instruction in bookmarks.tweets():
        kind = instruction["type"]
        if (
            kind == "TimelineTerminateTimeline"
            and instruction.get("direction") == "Bottom"
        ):
            terminal = True
        if kind not in {"TimelineAddEntries", "TimelineReplaceEntry"}:
            continue
        has_entries = True
        entries = (
            instruction["entries"]
            if kind == "TimelineAddEntries"
            else [instruction["entry"]]
        )
        for entry in entries:
            if entry["entryId"].startswith("tweet-"):
                if item := item_from_entry(entry):
                    rows.append(item)
            elif entry["entryId"].startswith("cursor-bottom-"):
                content = entry["content"]
                next_cursor = (content.get("itemContent") or content).get("value")
    if not has_entries and not terminal:
        raise ValueError("X returned no recognizable bookmark timeline.")
    if len(rows) > count:
        raise ValueError("X exceeded the requested bookmark page size.")
    return rows, next_cursor, terminal


def import_bookmarks(request):
    try:
        bookmarks, user_id = session()
        profile = bookmarks.api.user_by_rest_id(user_id)
        handle = (profile.get("core") or profile["legacy"])["screen_name"]
        if str(profile["rest_id"]) != user_id or not HANDLE.fullmatch(handle):
            raise NeedsLogin("Could not verify the signed-in X account.")
        expected = request.get("expected_account", {}).get("id", "")
        configured = request["settings"].get("IMPORTERS_BROWSER_ACCOUNT") or ""
        identifiers = {handle.lower(), user_id}
        if any(
            value and value.strip().lstrip("@").lower() not in identifiers
            for value in (expected, configured)
        ):
            raise NeedsLogin("The signed-in X account does not match this importer.")
        account = {"id": expected or handle.lower(), "label": f"@{handle}"}
        discover(request, bookmarks, account)
    except (exception.AuthRequired, exception.AuthorizationError) as error:
        raise NeedsLogin(
            "X requires sign-in or account verification. Sync the persona and check access again.",
        ) from error
    except exception.GalleryDLException as error:
        raise ValueError(
            f"Could not read X Bookmarks ({type(error).__name__}).",
        ) from error


def discover(request, bookmarks, account):
    previous = request.get("checkpoint", {})
    # Old learned checkpoints stored post IDs instead of X page cursors. Walk
    # once from the head to fill any gaps; ArchiveBox deduplicates captures.
    checkpoint = (
        previous
        if request["action"] == "import"
        and previous.get("version") == CHECKPOINT_VERSION
        else {}
    )
    if checkpoint.get("account_id") not in (None, account["id"]):
        raise NeedsLogin("The bookmark checkpoint belongs to another X account.")
    cursor = checkpoint.get("cursor")
    head = checkpoint.get("head_id") if cursor else None
    known_head = checkpoint.get("head_cursor") if cursor else checkpoint.get("head_id")
    limit = request["limit"]
    items = {}
    visited = {cursor}
    complete = reached_head = False

    for _ in range((500 + PAGE_SIZE - 1) // PAGE_SIZE):
        count = min(PAGE_SIZE, limit - len(items))
        rows, next_cursor, terminal = bookmark_page(bookmarks, cursor, count)
        if rows and head is None:
            head = rows[0]["id"]
        for item in rows:
            if item["id"] == known_head:
                reached_head = True
                break
            items.setdefault(item["id"], item)
        if reached_head or terminal:
            complete = True
            break
        if next_cursor == cursor and not rows:
            # X ends a real walk with an empty page that repeats the bottom
            # cursor. Confirm it once more so a transient empty page cannot
            # cause older bookmarks to be skipped.
            confirmed_rows, confirmed_cursor, terminal = bookmark_page(
                bookmarks,
                cursor,
                count,
            )
            if not confirmed_rows and (terminal or confirmed_cursor == cursor):
                complete = True
                break
            raise ValueError(
                "X returned an inconsistent final bookmark page. The saved cursor was kept.",
            )
        if next_cursor is None or next_cursor in visited:
            raise ValueError(
                "X stopped advancing the bookmark cursor. The saved cursor was kept.",
            )
        cursor = next_cursor
        visited.add(cursor)
        if len(items) == limit:
            break

    if request["action"] != "check":
        for item in items.values():
            emit(item)
    if request["action"] == "import":
        checkpoint = {
            "version": CHECKPOINT_VERSION,
            "feed": "x_bookmarks",
            "account_id": account["id"],
            "head_id": head,
            "head_cursor": head if complete else known_head,
            "cursor": None if complete else cursor,
        }
    else:
        checkpoint = previous

    message = "Imported a bookmark batch. Older bookmarks remain."
    if request["action"] != "import":
        message = "Verified access to X Bookmarks."
    elif reached_head:
        message = "Caught up with the previously verified X bookmarks."
    elif complete:
        message = "Reached the end of X Bookmarks."
    emit(
        {
            "type": "ImporterResult",
            "status": "succeeded",
            "account": account,
            "checkpoint": checkpoint,
            "has_more": not complete,
            "message": message,
        },
    )


if __name__ == "__main__":
    run(import_bookmarks)
