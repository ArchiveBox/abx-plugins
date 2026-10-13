"""Stop yt-dlp's RedditIE from re-extracting a gallery post forever.

WHY: yt-dlp's RedditIE treats a post as a text post only when its ``url`` field
contains ``/<id>/``. Gallery posts have ``url = https://www.reddit.com/gallery/<id>``
(no trailing slash), so RedditIE hands that URL to the generic extractor, Reddit
redirects it back to ``/comments/<id>/...``, RedditIE runs again, and so on
until the hook timeout. Upstream report: https://github.com/yt-dlp/yt-dlp/issues/15209

This override delegates to the installed RedditIE and only replaces the one
self-recursive result with the error upstream already uses for media-less text
posts. A yt-dlp release that fixes the guard never returns that result, so this
override then has no effect. Delete this plugin (and ``--plugin-dirs`` in the
hook) once the pinned yt-dlp includes the upstream fix.
"""

import re
import urllib.parse

# This module runs inside yt-dlp's interpreter (--plugin-dirs), not abx-plugins'.
from yt_dlp.extractor.reddit import RedditIE as UpstreamRedditIE  # pyright: ignore[reportMissingImports]  # ty: ignore[unresolved-import]


class RedditIE(UpstreamRedditIE, plugin_name="archivebox"):  # pyright: ignore[reportGeneralTypeIssues, reportCallIssue]
    def _real_extract(self, url):
        info = super()._real_extract(url)
        if not isinstance(info, dict) or info.get("_type") != "url_transparent":
            return info
        match = self._match_valid_url(url)
        post_id = match and match.groupdict().get("id")
        target = urllib.parse.urlparse(info.get("url") or "")
        if not (
            post_id
            and "reddit.com" in target.netloc
            and re.search(rf"/{re.escape(post_id)}(?:/|$)", target.path)
        ):
            return info
        # Same post (e.g. /gallery/<id>): following it would recurse into this extractor.
        self.raise_no_formats("No media found", expected=True, video_id=post_id)
        info = {k: v for k, v in info.items() if k not in ("_type", "url")}
        return {**info, "id": post_id}
