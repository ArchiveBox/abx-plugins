"""Read Substack's native JSON script without changing its media extraction.

Substack moved window._preloads from a quoted JSON.parse argument into the
preloads-json script. yt-dlp 2026.10.21 still expects the old argument. Keep
its legacy parser, format helpers, metadata and unsupported-page behavior.
Remove this override when the pinned upstream extractor reads the new script.
"""

import urllib.parse

from yt_dlp.extractor.substack import SubstackIE as UpstreamSubstackIE  # pyright: ignore[reportMissingImports]  # ty: ignore[unresolved-import]
from yt_dlp.networking import HEADRequest  # pyright: ignore[reportMissingImports]  # ty: ignore[unresolved-import]
from yt_dlp.utils import (  # pyright: ignore[reportMissingImports]  # ty: ignore[unresolved-import]
    determine_ext,
    get_element_html_by_id,
    js_to_json,
    str_or_none,
    try_get,
)


class SubstackIE(UpstreamSubstackIE, plugin_name="archivebox"):  # pyright: ignore[reportGeneralTypeIssues, reportCallIssue]
    def _real_extract(self, url):
        display_id = self._match_id(url)
        webpage = self._download_webpage(url, display_id)

        preloads = get_element_html_by_id("preloads-json", webpage, tag="script")
        if preloads is not None:
            webpage_info = self._search_json(
                r"<script\b[^>]*>",
                preloads,
                "preloads",
                display_id,
            )
        else:
            webpage_info = self._parse_json(
                self._search_json(
                    r"window\._preloads\s*=\s*JSON\.parse\(",
                    webpage,
                    "json string",
                    display_id,
                    transform_source=js_to_json,
                    contains_pattern=r'"{(?s:.+)}"',
                ),
                display_id,
            )

        canonical_url = url
        domain = try_get(
            webpage_info,
            lambda info: info["domainInfo"]["customDomain"],
            expected_type=str,
        )
        if domain:
            canonical_url = urllib.parse.urlparse(url)._replace(netloc=domain).geturl()

        post_type = webpage_info["post"]["type"]
        formats, subtitles = [], {}
        if post_type == "podcast":
            fmt = {"url": webpage_info["post"]["podcast_url"]}
            if not determine_ext(fmt["url"], default_ext=""):
                # The redirected format URL expires but the original URL doesn't,
                # so we only want to extract the extension from this request
                fmt["ext"] = determine_ext(
                    self._request_webpage(
                        HEADRequest(fmt["url"]),
                        display_id,
                        "Resolving podcast file extension",
                        "Podcast URL is invalid",
                    ).url,
                )
            formats.append(fmt)
        elif post_type == "video":
            formats, subtitles = self._extract_video_formats(
                webpage_info["post"]["videoUpload"]["id"],
                canonical_url,
            )
        else:
            self.raise_no_formats(f'Page type "{post_type}" is not supported')

        return {
            "id": str(webpage_info["post"]["id"]),
            "formats": formats,
            "subtitles": subtitles,
            "title": try_get(webpage_info, lambda info: info["post"]["title"]),
            "description": try_get(
                webpage_info,
                lambda info: info["post"]["description"],
            ),
            "thumbnail": try_get(
                webpage_info,
                lambda info: info["post"]["cover_image"],
            ),
            "uploader": try_get(webpage_info, lambda info: info["pub"]["name"]),
            "uploader_id": str_or_none(
                try_get(webpage_info, lambda info: info["post"]["publication_id"]),
            ),
            "webpage_url": canonical_url,
        }
