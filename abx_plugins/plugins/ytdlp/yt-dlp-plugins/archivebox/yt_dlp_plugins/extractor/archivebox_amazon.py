"""Resolve product-video HLS manifests before native fragment downloading.

yt-dlp 2026.10.21 returns AmazonStore master manifests as direct media URLs.
The native downloader then concatenates variant playlists instead of video.
Use normal per-entry Generic extraction to parse HLS; keep direct media intact.
Remove this override when the pinned upstream store extractor parses HLS.
"""

from yt_dlp.extractor.amazon import AmazonStoreIE as UpstreamAmazonStoreIE  # pyright: ignore[reportMissingImports]  # ty: ignore[unresolved-import]
from yt_dlp.utils import determine_ext, smuggle_url  # pyright: ignore[reportMissingImports]  # ty: ignore[unresolved-import]


class AmazonStoreIE(UpstreamAmazonStoreIE, plugin_name="archivebox"):  # pyright: ignore[reportGeneralTypeIssues, reportCallIssue]
    def _real_extract(self, url):
        playlist = super()._real_extract(url)
        for entry in playlist["entries"]:
            if determine_ext(entry["url"]) == "m3u8":
                entry["_type"] = "url_transparent"
                entry["ie_key"] = "Generic"
                entry["url"] = smuggle_url(entry["url"], {"force_videoid": entry["id"]})
        return playlist
