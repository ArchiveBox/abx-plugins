"""Route large-image link previews through yt-dlp's existing summary handler.

yt-dlp 2026.08.19 treats summary_large_image as an unknown player card and
returns empty video formats. It is the image-sized variant of a summary link,
so let the existing summary handler follow card_url, including links to media.
No format errors are suppressed; actual player/video cards remain untouched.
"""

from yt_dlp.extractor.twitter import TwitterIE as UpstreamTwitterIE  # pyright: ignore[reportMissingImports]  # ty: ignore[unresolved-import]


class TwitterIE(UpstreamTwitterIE, plugin_name="archivebox"):  # pyright: ignore[reportGeneralTypeIssues, reportCallIssue]
    def _extract_status(self, twid):
        status = super()._extract_status(twid)
        card = status.get("card") or {}
        name = card.get("name") or ""
        if name.split(":")[-1] == "summary_large_image":
            card["name"] = name.removesuffix("summary_large_image") + "summary"
        return status
