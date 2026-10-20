"""Resolve generic-page OpenGraph thumbnails before media/playlist extraction.

yt-dlp 2026.08.19 copies relative og:image values directly into metadata, then
aborts playlist downloads when its thumbnail request has no URL scheme.
Keep the thumbnail and resolve it against the response URL, as media URLs are.
Remove this override when the pinned upstream extractor resolves these paths.
"""

from urllib.parse import urljoin

from yt_dlp.extractor.generic import GenericIE as UpstreamGenericIE  # pyright: ignore[reportMissingImports]  # ty: ignore[unresolved-import]


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
