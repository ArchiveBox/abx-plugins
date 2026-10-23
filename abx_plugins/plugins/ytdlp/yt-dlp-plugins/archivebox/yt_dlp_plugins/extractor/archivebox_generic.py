"""Preserve generic-page thumbnails and ignore commented HTML5 media.

yt-dlp 2026.08.19 copies relative og:image values directly into metadata, then
aborts playlist downloads when its thumbnail request has no URL scheme.
Keep the thumbnail and resolve it against the response URL, as media URLs are.
Remove this override when the pinned upstream extractor resolves these paths.

yt-dlp 2026.10.21 also scans authoring examples inside HTML comments as media.
Limit comment removal to the native HTML5 extractor's input, preserving scripts.
"""

from html.parser import HTMLParser
import re
from urllib.parse import urljoin

from yt_dlp.extractor.generic import GenericIE as UpstreamGenericIE  # pyright: ignore[reportMissingImports]  # ty: ignore[unresolved-import]
from yt_dlp.extractor.genericembeds import (  # pyright: ignore[reportMissingImports]  # ty: ignore[unresolved-import]
    HTML5MediaEmbedIE as UpstreamHTML5MediaEmbedIE,
)


class _CommentParser(HTMLParser):
    def __init__(self, webpage):
        super().__init__(convert_charrefs=False)
        self.webpage = webpage
        self.line_offsets = [0, *(match.end() for match in re.finditer("\n", webpage))]
        self.comments = []

    def handle_comment(self, data):
        line, column = self.getpos()
        start = self.line_offsets[line - 1] + column
        end = self.webpage.index(">", start + 4 + len(data)) + 1
        self.comments.append((start, end))


class HTML5MediaEmbedIE(UpstreamHTML5MediaEmbedIE, plugin_name="archivebox"):  # pyright: ignore[reportGeneralTypeIssues, reportCallIssue]
    def _extract_from_webpage(self, url, webpage):
        # Native HTML5 tag scanning includes commented authoring examples.
        # Locate real comments so JavaScript strings containing <!-- remain intact.
        parser = _CommentParser(webpage)
        parser.feed(webpage)
        for start, end in reversed(parser.comments):
            webpage = webpage[:start] + webpage[end:]
        return super()._extract_from_webpage(url, webpage)


class GenericIE(UpstreamGenericIE, plugin_name="archivebox"):  # pyright: ignore[reportGeneralTypeIssues, reportCallIssue]
    def _extract_embeds(self, url, webpage, *, urlh=None, info_dict=None):
        if info_dict and info_dict.get("thumbnail"):
            info_dict["thumbnail"] = urljoin(
                urlh.url if urlh else url,
                info_dict["thumbnail"],
            )
        return super()._extract_embeds(
            url,
            webpage,
            urlh=urlh,
            info_dict=info_dict or {},
        )
