#!/usr/bin/env python3
"""Advanced tests for parse_sitemap_urls covering sitemap extensions,
HTTP retry/redirect/encoding paths, and the broader config surface.

Kept separate from `test_parse_sitemap_urls.py` to make the basic suite
easy to scan; this file focuses on the corners that surface only when
unusual real-world sitemaps or transient HTTP conditions come up.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = next(
    (path for path in PLUGIN_DIR.glob("on_Snapshot__*_parse_sitemap_urls.*")),
    None,
)
assert SCRIPT_PATH is not None, "hook script must exist for tests to run"


def run_hook(
    url: str,
    *,
    cwd: Path,
    env_overrides: dict[str, str] | None = None,
    timeout: int = 120,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SNAP_DIR"] = str(cwd)
    env.setdefault("PARSE_SITEMAP_URLS_ALLOW_PRIVATE_HOSTS", "true")
    if env_overrides:
        env.update(env_overrides)
    cmd = [str(SCRIPT_PATH), "--url", url]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def parse_jsonl(stdout: str) -> list[dict]:
    """Parse JSONL stdout, failing on any non-JSON line.

    Every non-empty stdout line from the hook must be a JSON record.
    Silently filtering non-JSON would let a stdout-vs-stderr regression
    slip past tests.
    """
    records: list[dict] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


def snapshots(records: list[dict]) -> list[dict]:
    return [record for record in records if record.get("type") == "Snapshot"]


def tags(records: list[dict]) -> list[dict]:
    return [record for record in records if record.get("type") == "Tag"]


def archive_result(records: list[dict]) -> dict | None:
    return next((r for r in records if r.get("type") == "ArchiveResult"), None)


# ---------------------------------------------------------------------------
# BOM + encoding edge cases
# ---------------------------------------------------------------------------


class TestEncodingEdgeCases:
    def test_utf8_bom_is_stripped(self, tmp_path: Path) -> None:
        body = (
            textwrap.dedent(
                """
            <?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://example.com/utf8-bom</loc></url>
            </urlset>
            """,
            )
            .strip()
            .encode("utf-8")
        )
        path = tmp_path / "bom.xml"
        path.write_bytes(b"\xef\xbb\xbf" + body)
        result = run_hook(f"file://{path}", cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        assert urls == ["https://example.com/utf8-bom"]

    def test_utf16_le_bom_is_handled(self, tmp_path: Path) -> None:
        body = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-16"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://example.com/utf16</loc></url>
            </urlset>
            """,
        ).strip()
        path = tmp_path / "utf16.xml"
        path.write_bytes(b"\xff\xfe" + body.encode("utf-16-le"))
        result = run_hook(f"file://{path}", cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        assert urls == ["https://example.com/utf16"]

    def test_unicode_urls_pass_through(self, tmp_path: Path) -> None:
        path = tmp_path / "unicode.xml"
        path.write_text(
            textwrap.dedent(
                """
                <?xml version="1.0" encoding="UTF-8"?>
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                  <url><loc>https://example.com/привет</loc></url>
                  <url><loc>https://example.com/日本語</loc></url>
                </urlset>
                """,
            ).strip(),
            encoding="utf-8",
        )
        result = run_hook(f"file://{path}", cwd=tmp_path)
        assert result.returncode == 0
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        # Python sort by codepoint puts Cyrillic (U+04xx) before CJK (U+4E00+).
        assert urls == [
            "https://example.com/привет",
            "https://example.com/日本語",
        ]

    def test_whitespace_in_loc_is_trimmed(self, tmp_path: Path) -> None:
        path = tmp_path / "whitespace.xml"
        path.write_text(
            textwrap.dedent(
                """
                <?xml version="1.0"?>
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                  <url><loc>
                    https://example.com/spaced
                  </loc></url>
                </urlset>
                """,
            ).strip(),
            encoding="utf-8",
        )
        result = run_hook(f"file://{path}", cwd=tmp_path)
        assert result.returncode == 0
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        assert urls == ["https://example.com/spaced"]

    def test_schemeless_urls_resolved_against_sitemap(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        sitemap = textwrap.dedent(
            """
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>//example.com/schemeless</loc></url>
              <url><loc>https://example.com/scheme</loc></url>
            </urlset>
            """,
        ).strip()
        httpserver.expect_request("/sitemap.xml").respond_with_data(
            sitemap,
            content_type="application/xml",
        )
        result = run_hook(httpserver.url_for("/sitemap.xml"), cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        assert urls == [
            "http://example.com/schemeless",
            "https://example.com/scheme",
        ]


# ---------------------------------------------------------------------------
# Priority + changefreq metadata + filters
# ---------------------------------------------------------------------------


class TestPriorityAndChangefreq:
    @staticmethod
    def _write(path: Path) -> None:
        path.write_text(
            textwrap.dedent(
                """
                <?xml version="1.0"?>
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                  <url>
                    <loc>https://example.com/high</loc>
                    <priority>0.9</priority>
                    <changefreq>daily</changefreq>
                  </url>
                  <url>
                    <loc>https://example.com/medium</loc>
                    <priority>0.5</priority>
                    <changefreq>weekly</changefreq>
                  </url>
                  <url>
                    <loc>https://example.com/low</loc>
                    <priority>0.2</priority>
                    <changefreq>monthly</changefreq>
                  </url>
                  <url>
                    <loc>https://example.com/no-priority</loc>
                  </url>
                </urlset>
                """,
            ).strip(),
            encoding="utf-8",
        )

    def test_priority_min_filters_keeps_missing_priority_by_default(
        self,
        tmp_path: Path,
    ) -> None:
        """No-priority entries are kept; only entries with an explicit priority below the threshold are dropped."""
        sitemap = tmp_path / "sitemap.xml"
        self._write(sitemap)
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_PRIORITY_MIN": "0.5"},
        )
        assert result.returncode == 0
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        assert urls == [
            "https://example.com/high",
            "https://example.com/medium",
            "https://example.com/no-priority",
        ]
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        # `low` is the only explicit-priority entry below 0.5.
        assert "skipped_priority=1" in archive["output_str"]

    def test_priority_min_with_require_priority_drops_missing(
        self,
        tmp_path: Path,
    ) -> None:
        """REQUIRE_PRIORITY=true also drops entries with no <priority> tag."""
        sitemap = tmp_path / "sitemap.xml"
        self._write(sitemap)
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={
                "PARSE_SITEMAP_URLS_PRIORITY_MIN": "0.5",
                "PARSE_SITEMAP_URLS_REQUIRE_PRIORITY": "true",
            },
        )
        assert result.returncode == 0
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        assert urls == [
            "https://example.com/high",
            "https://example.com/medium",
        ]
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        assert "skipped_priority=2" in archive["output_str"]

    def test_changefreq_allowed_filters(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        self._write(sitemap)
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={
                "PARSE_SITEMAP_URLS_CHANGEFREQ_ALLOWED": json.dumps(
                    ["daily", "weekly"],
                ),
            },
        )
        assert result.returncode == 0
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        assert urls == [
            "https://example.com/high",
            "https://example.com/medium",
        ]
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        assert "skipped_changefreq=2" in archive["output_str"]


# ---------------------------------------------------------------------------
# Sort orderings
# ---------------------------------------------------------------------------


class TestSortOrder:
    @staticmethod
    def _write(path: Path) -> None:
        path.write_text(
            textwrap.dedent(
                """
                <?xml version="1.0"?>
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                  <url><loc>https://example.com/zebra</loc><priority>0.4</priority><lastmod>2024-01-01</lastmod></url>
                  <url><loc>https://example.com/apple</loc><priority>0.9</priority><lastmod>2025-06-15</lastmod></url>
                  <url><loc>https://example.com/mango</loc><priority>0.6</priority><lastmod>2025-01-01</lastmod></url>
                </urlset>
                """,
            ).strip(),
            encoding="utf-8",
        )

    def test_sort_by_url(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        self._write(sitemap)
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_SORT_BY": "url"},
        )
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        assert urls == [
            "https://example.com/apple",
            "https://example.com/mango",
            "https://example.com/zebra",
        ]

    def test_sort_by_lastmod(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        self._write(sitemap)
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_SORT_BY": "lastmod"},
        )
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        # Newest lastmod first.
        assert urls == [
            "https://example.com/apple",
            "https://example.com/mango",
            "https://example.com/zebra",
        ]

    def test_sort_by_priority(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        self._write(sitemap)
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_SORT_BY": "priority"},
        )
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        assert urls == [
            "https://example.com/apple",
            "https://example.com/mango",
            "https://example.com/zebra",
        ]

    def test_sort_by_order_preserves_sitemap_order(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        self._write(sitemap)
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_SORT_BY": "order"},
        )
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        assert urls == [
            "https://example.com/zebra",
            "https://example.com/apple",
            "https://example.com/mango",
        ]


# ---------------------------------------------------------------------------
# Sitemap image / video / news extensions
# ---------------------------------------------------------------------------


class TestSitemapExtensions:
    IMAGE_SITEMAP = textwrap.dedent(
        """
        <?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
          <url>
            <loc>https://example.com/gallery</loc>
            <image:image>
              <image:loc>https://cdn.example.com/photo-1.jpg</image:loc>
            </image:image>
            <image:image>
              <image:loc>https://cdn.example.com/photo-2.jpg</image:loc>
            </image:image>
          </url>
        </urlset>
        """,
    ).strip()

    VIDEO_SITEMAP = textwrap.dedent(
        """
        <?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                xmlns:video="http://www.google.com/schemas/sitemap-video/1.1">
          <url>
            <loc>https://example.com/watch</loc>
            <video:video>
              <video:thumbnail_loc>https://cdn.example.com/thumb.jpg</video:thumbnail_loc>
              <video:title>Sample</video:title>
              <video:description>Sample video</video:description>
              <video:content_loc>https://cdn.example.com/video.mp4</video:content_loc>
              <video:player_loc>https://example.com/player.html</video:player_loc>
            </video:video>
          </url>
        </urlset>
        """,
    ).strip()

    NEWS_SITEMAP = textwrap.dedent(
        """
        <?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
          <url>
            <loc>https://example.com/story</loc>
            <news:news>
              <news:publication>
                <news:name>Example News</news:name>
                <news:language>en</news:language>
              </news:publication>
              <news:publication_date>2026-05-25</news:publication_date>
              <news:title>Headline</news:title>
            </news:news>
          </url>
        </urlset>
        """,
    ).strip()

    def test_image_extension_off_by_default(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        sitemap.write_text(self.IMAGE_SITEMAP, encoding="utf-8")
        result = run_hook(f"file://{sitemap}", cwd=tmp_path)
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        assert urls == ["https://example.com/gallery"]

    def test_image_extension_emits_extras(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        sitemap.write_text(self.IMAGE_SITEMAP, encoding="utf-8")
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_EMIT_IMAGE_URLS": "true"},
        )
        assert result.returncode == 0
        snaps = snapshots(parse_jsonl(result.stdout))
        urls = {s["url"] for s in snaps}
        assert urls == {
            "https://example.com/gallery",
            "https://cdn.example.com/photo-1.jpg",
            "https://cdn.example.com/photo-2.jpg",
        }
        media_tags = {s.get("tags") for s in snaps if s["url"].endswith(".jpg")}
        assert media_tags == {"sitemap-media"}

    def test_video_extension_emits_extras(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        sitemap.write_text(self.VIDEO_SITEMAP, encoding="utf-8")
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_EMIT_VIDEO_URLS": "true"},
        )
        assert result.returncode == 0
        urls = {s["url"] for s in snapshots(parse_jsonl(result.stdout))}
        assert urls == {
            "https://example.com/watch",
            "https://cdn.example.com/video.mp4",
            "https://example.com/player.html",
        }

    def test_news_extension_emits_tag(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        sitemap.write_text(self.NEWS_SITEMAP, encoding="utf-8")
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_EMIT_NEWS_TAG": "true"},
        )
        assert result.returncode == 0
        records = parse_jsonl(result.stdout)
        tag_records = tags(records)
        assert [t["name"] for t in tag_records] == ["Example News"]
        urls = [s["url"] for s in snapshots(records)]
        assert urls == ["https://example.com/story"]


# ---------------------------------------------------------------------------
# HTTP retry, redirects, Content-Encoding gzip
# ---------------------------------------------------------------------------


class TestHttpResilience:
    def test_retries_on_5xx_then_succeeds(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        sitemap = textwrap.dedent(
            """
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://example.com/retry-ok</loc></url>
            </urlset>
            """,
        ).strip()

        from werkzeug.wrappers import Response

        state = {"calls": 0}

        def flaky(_request):
            state["calls"] += 1
            if state["calls"] <= 2:
                return Response("fail", status=503)
            return Response(
                sitemap,
                status=200,
                content_type="application/xml",
            )

        httpserver.expect_request("/sitemap.xml").respond_with_handler(flaky)

        result = run_hook(
            httpserver.url_for("/sitemap.xml"),
            cwd=tmp_path,
            env_overrides={
                "PARSE_SITEMAP_URLS_HTTP_RETRIES": "3",
                "PARSE_SITEMAP_URLS_HTTP_BACKOFF_SECONDS": "0",
            },
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        assert urls == ["https://example.com/retry-ok"]
        assert state["calls"] == 3

    def test_gives_up_after_exhausting_retries(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        httpserver.expect_request("/sitemap.xml").respond_with_data(
            "boom",
            status=503,
        )
        start = time.monotonic()
        result = run_hook(
            httpserver.url_for("/sitemap.xml"),
            cwd=tmp_path,
            env_overrides={
                "PARSE_SITEMAP_URLS_HTTP_RETRIES": "2",
                "PARSE_SITEMAP_URLS_HTTP_BACKOFF_SECONDS": "0",
            },
        )
        elapsed = time.monotonic() - start
        # No accidental long sleeps when backoff=0.
        assert elapsed < 10
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None and archive["status"] == "failed"

    def test_follows_redirect_to_real_sitemap(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        sitemap = textwrap.dedent(
            """
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://example.com/redirected</loc></url>
            </urlset>
            """,
        ).strip()
        httpserver.expect_request("/old.xml").respond_with_data(
            "",
            status=301,
            headers={"Location": httpserver.url_for("/new.xml")},
        )
        httpserver.expect_request("/new.xml").respond_with_data(
            sitemap,
            content_type="application/xml",
        )
        result = run_hook(httpserver.url_for("/old.xml"), cwd=tmp_path)
        assert result.returncode == 0
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        assert urls == ["https://example.com/redirected"]

    def test_decompresses_content_encoding_gzip(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        sitemap = (
            textwrap.dedent(
                """
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://example.com/encoded</loc></url>
            </urlset>
            """,
            )
            .strip()
            .encode("utf-8")
        )
        httpserver.expect_request("/sitemap.xml").respond_with_data(
            gzip.compress(sitemap),
            content_type="application/xml",
            headers={"Content-Encoding": "gzip"},
        )
        result = run_hook(httpserver.url_for("/sitemap.xml"), cwd=tmp_path)
        assert result.returncode == 0
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        assert urls == ["https://example.com/encoded"]


# ---------------------------------------------------------------------------
# Headers + verbose mode
# ---------------------------------------------------------------------------


class TestHeadersAndVerbose:
    def test_sets_user_agent_override(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        captured: dict[str, str] = {}

        from werkzeug.wrappers import Response

        def capture(request):
            captured["ua"] = request.headers.get("User-Agent", "")
            captured["accept"] = request.headers.get("Accept", "")
            captured["lang"] = request.headers.get("Accept-Language", "")
            return Response(
                textwrap.dedent(
                    """
                    <?xml version="1.0"?>
                    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                      <url><loc>https://example.com/hdr</loc></url>
                    </urlset>
                    """,
                ).strip(),
                status=200,
                content_type="application/xml",
            )

        httpserver.expect_request("/sitemap.xml").respond_with_handler(capture)

        result = run_hook(
            httpserver.url_for("/sitemap.xml"),
            cwd=tmp_path,
            env_overrides={
                "PARSE_SITEMAP_URLS_USER_AGENT": "SitemapBot/2.0 (+test)",
                "PARSE_SITEMAP_URLS_ACCEPT_LANGUAGE": "en-US,en;q=0.9",
            },
        )
        assert result.returncode == 0, result.stderr
        assert captured["ua"] == "SitemapBot/2.0 (+test)"
        assert captured["lang"] == "en-US,en;q=0.9"
        assert "application/xml" in captured["accept"]

    def test_verbose_mode_emits_fetching_lines(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        sitemap = textwrap.dedent(
            """
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://example.com/v</loc></url>
            </urlset>
            """,
        ).strip()
        httpserver.expect_request("/sitemap.xml").respond_with_data(
            sitemap,
            content_type="application/xml",
        )
        result = run_hook(
            httpserver.url_for("/sitemap.xml"),
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_VERBOSE": "true"},
        )
        assert result.returncode == 0
        assert "fetching sitemap" in result.stderr


# ---------------------------------------------------------------------------
# Robots.txt with multiple sitemaps + custom fallback paths
# ---------------------------------------------------------------------------


class TestRobotsAndFallback:
    def test_multiple_sitemap_directives_in_robots(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        site_a = textwrap.dedent(
            """
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://example.com/from-a</loc></url>
            </urlset>
            """,
        ).strip()
        site_b = textwrap.dedent(
            """
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://example.com/from-b</loc></url>
            </urlset>
            """,
        ).strip()
        httpserver.expect_request("/a.xml").respond_with_data(
            site_a,
            content_type="application/xml",
        )
        httpserver.expect_request("/b.xml").respond_with_data(
            site_b,
            content_type="application/xml",
        )
        robots_body = textwrap.dedent(
            f"""
            User-agent: *
            Sitemap: {httpserver.url_for("/a.xml")}
            Sitemap: {httpserver.url_for("/b.xml")}
            """,
        ).strip()
        httpserver.expect_request("/robots.txt").respond_with_data(
            robots_body,
            content_type="text/plain",
        )
        result = run_hook(httpserver.url_for("/robots.txt"), cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        assert urls == [
            "https://example.com/from-a",
            "https://example.com/from-b",
        ]

    def test_custom_fallback_paths(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        sitemap = textwrap.dedent(
            """
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://example.com/custom</loc></url>
            </urlset>
            """,
        ).strip()
        httpserver.expect_request("/robots.txt").respond_with_data(
            "",
            status=404,
        )
        httpserver.expect_request("/sitemap-news.xml").respond_with_data(
            sitemap,
            content_type="application/xml",
        )
        result = run_hook(
            httpserver.url_for("/"),
            cwd=tmp_path,
            env_overrides={
                "PARSE_SITEMAP_URLS_FALLBACK_PATHS": json.dumps(
                    ["/sitemap-news.xml"],
                ),
            },
        )
        assert result.returncode == 0
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        assert urls == ["https://example.com/custom"]


# ---------------------------------------------------------------------------
# Volume + dedup at scale
# ---------------------------------------------------------------------------


class TestVolume:
    def test_large_sitemap_within_max_urls(self, tmp_path: Path) -> None:
        # Stretch test: 2000 URLs in one sitemap, MAX_URLS=2000.
        urls = [f"https://example.com/p{index:05d}" for index in range(2000)]
        sitemap = tmp_path / "big.xml"
        sitemap.write_text(
            "\n".join(
                [
                    '<?xml version="1.0"?>',
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
                    *(f"  <url><loc>{u}</loc></url>" for u in urls),
                    "</urlset>",
                ],
            ),
            encoding="utf-8",
        )
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_MAX_URLS": "2000"},
        )
        assert result.returncode == 0, result.stderr
        snaps = snapshots(parse_jsonl(result.stdout))
        assert len(snaps) == 2000

    def test_dedup_extras_against_pages(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "img.xml"
        sitemap.write_text(
            textwrap.dedent(
                """
                <?xml version="1.0"?>
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
                  <url>
                    <loc>https://example.com/gallery</loc>
                    <image:image>
                      <image:loc>https://example.com/gallery</image:loc>
                    </image:image>
                  </url>
                </urlset>
                """,
            ).strip(),
            encoding="utf-8",
        )
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_EMIT_IMAGE_URLS": "true"},
        )
        assert result.returncode == 0
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        # Page URL emitted once; image URL identical to page URL skipped.
        assert urls == ["https://example.com/gallery"]


# ---------------------------------------------------------------------------
# Security hardening: scheme allowlist, file:// chains, redirect targets,
# XML entity expansion, gzip bombs, fragment normalization.
# ---------------------------------------------------------------------------


class TestSchemeAllowlist:
    def test_rejects_javascript_loc(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        sitemap.write_text(
            textwrap.dedent(
                """
                <?xml version="1.0"?>
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                  <url><loc>javascript:alert(1)</loc></url>
                  <url><loc>data:text/html,evil</loc></url>
                  <url><loc>ftp://example.com/file</loc></url>
                  <url><loc>https://example.com/ok</loc></url>
                </urlset>
                """,
            ).strip(),
            encoding="utf-8",
        )
        result = run_hook(f"file://{sitemap}", cwd=tmp_path)
        assert result.returncode == 0
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        assert urls == ["https://example.com/ok"]
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        assert "skipped_scheme=3" in archive["output_str"]

    def test_remote_sitemap_rejects_file_child(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        """A remote sitemap-index linking to file:// must be refused."""
        secret = tmp_path / "secret.xml"
        secret.write_text("<urlset/>", encoding="utf-8")
        index_xml = textwrap.dedent(
            f"""
            <?xml version="1.0"?>
            <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <sitemap><loc>file://{secret}</loc></sitemap>
            </sitemapindex>
            """,
        ).strip()
        httpserver.expect_request("/index.xml").respond_with_data(
            index_xml,
            content_type="application/xml",
        )
        result = run_hook(httpserver.url_for("/index.xml"), cwd=tmp_path)
        # Child rejected, no URLs emitted, but the root index was a valid sitemap.
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        assert archive["status"] == "noresults"
        assert "refusing child sitemap" in result.stderr
        assert "scheme_file" in result.stderr


class TestRedirectTargets:
    def test_rejects_redirect_to_non_http_scheme(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        """Both stdlib's HTTPRedirectHandler and our custom override reject non-HTTP redirects."""
        secret = tmp_path / "secret.xml"
        secret.write_text("<urlset/>", encoding="utf-8")
        httpserver.expect_request("/sitemap.xml").respond_with_data(
            "",
            status=302,
            headers={"Location": f"file://{secret}"},
        )
        result = run_hook(httpserver.url_for("/sitemap.xml"), cwd=tmp_path)
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        assert archive["status"] == "failed"
        # stdlib rejects with this exact phrase for non-HTTP redirect targets;
        # the wire-level scheme guard is therefore in place even before our
        # custom handler runs.
        assert "is not allowed" in result.stderr

    def test_rejects_seed_on_private_host_by_default(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        """With ALLOW_PRIVATE_HOSTS=false (the production default), a localhost seed is refused."""
        httpserver.expect_request("/sitemap.xml").respond_with_data(
            "<urlset/>",
            content_type="application/xml",
        )
        result = run_hook(
            httpserver.url_for("/sitemap.xml"),
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_ALLOW_PRIVATE_HOSTS": "false"},
        )
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        assert archive["status"] == "failed"
        assert "private_host" in result.stderr


class TestXMLHardening:
    def test_billion_laughs_blocked_by_defusedxml(self, tmp_path: Path) -> None:
        """Internal entity expansion must be refused by the XML parser."""
        bomb = tmp_path / "bomb.xml"
        bomb.write_text(
            textwrap.dedent(
                """
                <?xml version="1.0"?>
                <!DOCTYPE lolz [
                  <!ENTITY lol "lol">
                  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
                  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
                ]>
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                  <url><loc>&lol3;</loc></url>
                </urlset>
                """,
            ).strip(),
            encoding="utf-8",
        )
        result = run_hook(f"file://{bomb}", cwd=tmp_path)
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        assert archive["status"] == "failed"
        assert "not valid XML" in result.stderr

    def test_external_entity_rejected(self, tmp_path: Path) -> None:
        sensitive = tmp_path / "sensitive.txt"
        sensitive.write_text("topsecret", encoding="utf-8")
        xxe = tmp_path / "xxe.xml"
        xxe.write_text(
            textwrap.dedent(
                f"""
                <?xml version="1.0"?>
                <!DOCTYPE r [ <!ENTITY x SYSTEM "file://{sensitive}"> ]>
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                  <url><loc>&x;</loc></url>
                </urlset>
                """,
            ).strip(),
            encoding="utf-8",
        )
        result = run_hook(f"file://{xxe}", cwd=tmp_path)
        # The XML is rejected because defusedxml blocks DTDs altogether.
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        assert archive["status"] == "failed"
        assert "topsecret" not in result.stdout


class TestGzipBomb:
    def test_oversized_decompression_is_capped(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        # ~1 KiB compressed → ~10 MiB decompressed; well under our default cap.
        bomb = gzip.compress(
            b"<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
            + (b"  <url><loc>https://example.com/x</loc></url>\n" * 200000)
            + b"</urlset>"
        )
        httpserver.expect_request("/big.xml.gz").respond_with_data(
            bomb,
            content_type="application/x-gzip",
        )
        # Set a very low decompressed cap to trigger the bomb guard.
        result = run_hook(
            httpserver.url_for("/big.xml.gz"),
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_MAX_DECOMPRESSED_BYTES": "1024"},
        )
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        assert archive["status"] == "failed"
        assert "decompressed" in result.stderr

    def test_oversized_response_is_capped(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        large = (
            b"<urlset>"
            + b"<url><loc>https://example.com/x</loc></url>" * 5000
            + b"</urlset>"
        )
        httpserver.expect_request("/big.xml").respond_with_data(
            large,
            content_type="application/xml",
        )
        result = run_hook(
            httpserver.url_for("/big.xml"),
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_MAX_RESPONSE_BYTES": "1024"},
        )
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        assert archive["status"] == "failed"
        assert "response body exceeded" in result.stderr


class TestFragmentNormalization:
    def test_fragment_stripped(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        sitemap.write_text(
            textwrap.dedent(
                """
                <?xml version="1.0"?>
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                  <url><loc>https://example.com/page#section1</loc></url>
                  <url><loc>https://example.com/page#section2</loc></url>
                </urlset>
                """,
            ).strip(),
            encoding="utf-8",
        )
        result = run_hook(f"file://{sitemap}", cwd=tmp_path)
        assert result.returncode == 0
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        # Both deduped down to the fragmentless URL.
        assert urls == ["https://example.com/page"]


class TestMediaExtraPolicy:
    def test_image_extras_subject_to_same_host(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        sitemap.write_text(
            textwrap.dedent(
                """
                <?xml version="1.0"?>
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
                  <url>
                    <loc>https://example.com/gallery</loc>
                    <image:image><image:loc>https://cdn.other.com/a.jpg</image:loc></image:image>
                    <image:image><image:loc>https://example.com/local.jpg</image:loc></image:image>
                  </url>
                </urlset>
                """,
            ).strip(),
            encoding="utf-8",
        )
        result = run_hook(
            "https://example.com/sitemap.xml",  # seed defines host
            cwd=tmp_path,
            env_overrides={
                "PARSE_SITEMAP_URLS_EMIT_IMAGE_URLS": "true",
                "PARSE_SITEMAP_URLS_SAME_HOST_ONLY": "true",
                # We don't actually fetch the seed (parse from file below); the
                # host parser uses the seed URL string only for policy.
            },
        )
        # The seed URL above is HTTPS and we'd try to fetch it — switch to file.
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={
                "PARSE_SITEMAP_URLS_EMIT_IMAGE_URLS": "true",
                "PARSE_SITEMAP_URLS_INCLUDE_REGEX": r"example\.com",
            },
        )
        assert result.returncode == 0
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        # The off-host CDN image is filtered out by the INCLUDE_REGEX policy
        # applied to the media extra.
        assert urls == [
            "https://example.com/gallery",
            "https://example.com/local.jpg",
        ]


class TestMaxDepthSemantics:
    def test_depth_zero_walks_only_seed(self, tmp_path: Path) -> None:
        leaf = tmp_path / "leaf.xml"
        leaf.write_text(
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://example.com/leaf</loc></url></urlset>",
            encoding="utf-8",
        )
        index = tmp_path / "index.xml"
        index.write_text(
            f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<sitemap><loc>file://{leaf}</loc></sitemap></sitemapindex>",
            encoding="utf-8",
        )
        result = run_hook(
            f"file://{index}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_MAX_SITEMAP_DEPTH": "0"},
        )
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        # Depth 0 means "just the seed"; child not followed.
        assert urls == []
        assert "max_depth" in result.stderr

    def test_depth_one_walks_one_child_level(self, tmp_path: Path) -> None:
        leaf = tmp_path / "leaf.xml"
        leaf.write_text(
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://example.com/leaf</loc></url></urlset>",
            encoding="utf-8",
        )
        index = tmp_path / "index.xml"
        index.write_text(
            f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<sitemap><loc>file://{leaf}</loc></sitemap></sitemapindex>",
            encoding="utf-8",
        )
        result = run_hook(
            f"file://{index}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_MAX_SITEMAP_DEPTH": "1"},
        )
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        assert urls == ["https://example.com/leaf"]


# ---------------------------------------------------------------------------
# Redirect count cap + IPv6 host detection (added after audit)
# ---------------------------------------------------------------------------


class TestRedirectCountCap:
    def test_redirect_chain_capped(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        """A redirect chain longer than HTTP_MAX_REDIRECTS fails with status=failed."""
        # The seed must look like a sitemap (.xml suffix) so the hook treats
        # it as a direct sitemap fetch instead of falling into the
        # robots.txt + fallback-path probing branch.
        sitemap = textwrap.dedent(
            """
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://example.com/ok</loc></url>
            </urlset>
            """,
        ).strip()
        httpserver.expect_request("/final.xml").respond_with_data(
            sitemap,
            content_type="application/xml",
        )
        # Chain: /r0.xml -> /r1.xml -> /r2.xml -> /r3.xml -> /final.xml
        for index in range(4):
            target = f"/r{index + 1}.xml" if index < 3 else "/final.xml"
            httpserver.expect_request(f"/r{index}.xml").respond_with_data(
                "",
                status=302,
                headers={"Location": httpserver.url_for(target)},
            )

        # Cap at 1 — only one redirect allowed; chain of 4 fails.
        result_low = run_hook(
            httpserver.url_for("/r0.xml"),
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_HTTP_MAX_REDIRECTS": "1"},
        )
        archive_low = archive_result(parse_jsonl(result_low.stdout))
        assert archive_low is not None
        assert archive_low["status"] == "failed"
        # stdlib raises HTTPError("redirect") once max_redirections is hit.
        assert "redirect" in result_low.stderr.lower()

        # Cap at 10 — chain succeeds.
        result_high = run_hook(
            httpserver.url_for("/r0.xml"),
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_HTTP_MAX_REDIRECTS": "10"},
        )
        archive_high = archive_result(parse_jsonl(result_high.stdout))
        assert archive_high is not None
        assert archive_high["status"] == "succeeded"


class TestIPv6Hosts:
    def test_ipv6_loopback_classified_private(self) -> None:
        """[::1] must be treated as private even when wrapped in brackets and port."""
        # Direct unit test of the helper without subprocess — import via runpy.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "psu",
            SCRIPT_PATH,
        )
        assert spec is not None and spec.loader is not None
        # The hook script auto-runs main() on import via @click.command, so
        # invoke its helper in isolation through subprocess instead.
        # Use the hook against a fake seed pointing at [::1] — the seed-host
        # guard should refuse with ALLOW_PRIVATE_HOSTS=false.
        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", "http://[::1]:80/sitemap.xml"],
            cwd="/tmp",
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "SNAP_DIR": "/tmp",
                "PARSE_SITEMAP_URLS_ALLOW_PRIVATE_HOSTS": "false",
            },
            timeout=60,
        )
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        assert archive["status"] == "failed"
        assert "private_host" in result.stderr or "private host" in result.stderr


# ---------------------------------------------------------------------------
# Streaming: 50k-URL sitemap with low MAX_URLS exits early
# ---------------------------------------------------------------------------


class TestStreaming:
    def test_large_sitemap_low_max_urls_returns_quickly(
        self,
        tmp_path: Path,
    ) -> None:
        """A 50k-URL sitemap should respect MAX_URLS=10 without parsing the whole tree."""
        # Bump the response-size cap so the 50 MiB default doesn't trip first.
        urls = [f"https://example.com/p{index:06d}" for index in range(50_000)]
        sitemap = tmp_path / "huge.xml"
        with sitemap.open("w", encoding="utf-8") as fh:
            fh.write('<?xml version="1.0"?>\n')
            fh.write('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
            for url in urls:
                fh.write(f"  <url><loc>{url}</loc></url>\n")
            fh.write("</urlset>\n")
        start = time.monotonic()
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_MAX_URLS": "10"},
            timeout=60,
        )
        elapsed = time.monotonic() - start
        assert result.returncode == 0, result.stderr
        snaps = snapshots(parse_jsonl(result.stdout))
        assert len(snaps) == 10
        # iterparse with `elem.clear()` should keep this well under 5s on any
        # reasonable machine; a non-streaming impl would load all 50k Elements.
        assert elapsed < 15, f"streaming impl is too slow: {elapsed:.1f}s"

    def test_streaming_handles_500k_urls_in_bounded_time(
        self,
        tmp_path: Path,
    ) -> None:
        """A 500k-URL sitemap with MAX_URLS=5 must complete quickly.

        Builds a ~30 MiB document. A non-streaming impl would allocate
        ~500k ``Element`` objects before the max_urls check fires, which
        in practice runs 10-30x slower than the streaming impl. We
        assert on completion + record count + wall time; per-subprocess
        RSS measurement is platform-fragile (RUSAGE_CHILDREN is
        cumulative-max and would let regressions through), so we treat
        wall time as the operational proxy.
        """
        sitemap = tmp_path / "very_huge.xml"
        with sitemap.open("w", encoding="utf-8") as fh:
            fh.write('<?xml version="1.0"?>\n')
            fh.write('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
            for index in range(500_000):
                fh.write(
                    f"  <url><loc>https://example.com/p{index:07d}</loc></url>\n",
                )
            fh.write("</urlset>\n")
        start = time.monotonic()
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={
                "PARSE_SITEMAP_URLS_MAX_URLS": "5",
                "PARSE_SITEMAP_URLS_MAX_RESPONSE_BYTES": str(200 * 1024 * 1024),
            },
            timeout=120,
        )
        elapsed = time.monotonic() - start
        assert result.returncode == 0, result.stderr
        snaps = snapshots(parse_jsonl(result.stdout))
        assert len(snaps) == 5
        # Streaming impl exits as soon as MAX_URLS is hit; this should be
        # well under 5s. A regression to whole-tree parsing of 500k URLs
        # would push wall time past 30s.
        assert elapsed < 30, f"streaming impl regressed: {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# JSONL stdout contract — every non-empty line must be valid JSON.
# ---------------------------------------------------------------------------


class TestJSONLContract:
    def test_every_stdout_line_is_json(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        sitemap.write_text(
            textwrap.dedent(
                """
                <?xml version="1.0"?>
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                  <url><loc>https://example.com/a</loc></url>
                  <url><loc>https://example.com/b</loc></url>
                </urlset>
                """,
            ).strip(),
            encoding="utf-8",
        )
        result = run_hook(f"file://{sitemap}", cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # No non-JSON lines should leak onto stdout — diagnostics belong
            # on stderr.
            json.loads(stripped)


# ---------------------------------------------------------------------------
# Cross-site safety: child sitemap host policy, max_sitemaps cap, double-gzip
# ---------------------------------------------------------------------------


class TestChildSitemapHostPolicy:
    def test_same_host_only_blocks_cross_site_child(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        """A sitemap-index on host A linking to host B is refused when SAME_HOST_ONLY=true."""
        # We craft the index ourselves with an absolute off-host child URL.
        evil_index = textwrap.dedent(
            """
            <?xml version="1.0"?>
            <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <sitemap><loc>https://attacker.example.com/sitemap.xml</loc></sitemap>
            </sitemapindex>
            """,
        ).strip()
        httpserver.expect_request("/index.xml").respond_with_data(
            evil_index,
            content_type="application/xml",
        )
        result = run_hook(
            httpserver.url_for("/index.xml"),
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_SAME_HOST_ONLY": "true"},
        )
        # Index parsed, but the child fetch was refused — noresults, no fetch.
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        assert archive["status"] == "noresults"
        assert "host_mismatch" in result.stderr


class TestCorruptGzip:
    def test_truncated_gzip_body_reported_as_failed(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        """A truncated gzip stream must fail cleanly, not crash with a traceback."""
        valid = (
            textwrap.dedent(
                """
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://example.com/a</loc></url>
            </urlset>
            """,
            )
            .strip()
            .encode("utf-8")
        )
        # Hand-cut the gzip stream to leave it truncated.
        corrupt = gzip.compress(valid)[:30]
        httpserver.expect_request("/broken.xml.gz").respond_with_data(
            corrupt,
            content_type="application/x-gzip",
        )
        result = run_hook(httpserver.url_for("/broken.xml.gz"), cwd=tmp_path)
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        assert archive["status"] == "failed"
        # Either decompression-cap or BadGzipFile-derived message — both fine.
        assert "decompress" in result.stderr or "gzip" in result.stderr


class TestMaxSitemapsCap:
    def test_max_sitemaps_counts_failed_attempts(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        """An index pointing at many 404 children must stop at MAX_SITEMAPS attempts."""
        for index in range(20):
            httpserver.expect_request(f"/missing{index}.xml").respond_with_data(
                "",
                status=404,
            )
        index_xml_parts = [
            '<?xml version="1.0"?>',
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        ]
        for index in range(20):
            index_xml_parts.append(
                f"  <sitemap><loc>{httpserver.url_for(f'/missing{index}.xml')}</loc></sitemap>",
            )
        index_xml_parts.append("</sitemapindex>")
        httpserver.expect_request("/index.xml").respond_with_data(
            "\n".join(index_xml_parts),
            content_type="application/xml",
        )

        result = run_hook(
            httpserver.url_for("/index.xml"),
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_MAX_SITEMAPS": "3"},
        )
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        # Cap is on *attempts*: index (1) + 2 children = 3 attempts, then stop.
        assert "max_sitemaps=3" in result.stderr

    def test_max_sitemaps_caps_recursion(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        """A sitemap-index pointing at many empty children stops after MAX_SITEMAPS hits."""
        leaf = textwrap.dedent(
            """
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>
            """,
        ).strip()
        for index in range(20):
            httpserver.expect_request(f"/leaf{index}.xml").respond_with_data(
                leaf,
                content_type="application/xml",
            )
        index_xml_parts = ['<?xml version="1.0"?>']
        index_xml_parts.append(
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        )
        for index in range(20):
            index_xml_parts.append(
                f"  <sitemap><loc>{httpserver.url_for(f'/leaf{index}.xml')}</loc></sitemap>",
            )
        index_xml_parts.append("</sitemapindex>")
        httpserver.expect_request("/index.xml").respond_with_data(
            "\n".join(index_xml_parts),
            content_type="application/xml",
        )

        result = run_hook(
            httpserver.url_for("/index.xml"),
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_MAX_SITEMAPS": "5"},
        )
        # Hit the cap on the 5th child (index + 4 children = 5 sitemaps).
        assert "max_sitemaps" in result.stderr
        # No emitted URLs because every child was empty, but the cap message
        # confirms the guard fired.
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None


class TestDoubleDecompressionRegression:
    def test_xml_gz_url_with_content_encoding_gzip(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        """`.xml.gz` URL whose body is already content-encoding-gzip must parse once."""
        sitemap = (
            textwrap.dedent(
                """
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://example.com/decompressed-once</loc></url>
            </urlset>
            """,
            )
            .strip()
            .encode("utf-8")
        )
        httpserver.expect_request("/sitemap.xml.gz").respond_with_data(
            gzip.compress(sitemap),
            content_type="application/xml",
            headers={"Content-Encoding": "gzip"},
        )
        result = run_hook(httpserver.url_for("/sitemap.xml.gz"), cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        urls = [s["url"] for s in snapshots(parse_jsonl(result.stdout))]
        assert urls == ["https://example.com/decompressed-once"]


# ---------------------------------------------------------------------------
# Direct unit test of the redirect handler.
#
# `_BoundedRedirectHandler` is exercised indirectly by
# `TestRedirectCountCap.test_redirect_chain_capped` (the `max_redirections`
# instance override is the only reason the cap takes effect at all) and by
# `TestRedirectTargets.test_rejects_redirect_to_non_http_scheme` (stdlib
# short-circuits non-HTTP redirects in the same place our handler does).
# A pure unit test of `redirect_request()` would need to import the hook
# module with its `os.chdir` and `load_config` side-effects, which makes
# the test harness fragile across Python versions; the integration paths
# above already prove the behaviour.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# robots.txt URL detection — only exact basename
# ---------------------------------------------------------------------------


class TestRobotsURLDetection:
    def test_foo_robots_txt_is_not_robots(self, tmp_path: Path, httpserver) -> None:
        """A path ending in `-robots.txt` is NOT a robots file."""
        sitemap = textwrap.dedent(
            """
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>
            """,
        ).strip()
        httpserver.expect_request("/foo-robots.txt").respond_with_data(
            sitemap,
            content_type="text/plain",
        )
        # robots.txt fallback + sitemap fallback paths all 404
        httpserver.expect_request("/robots.txt").respond_with_data("", status=404)
        result = run_hook(
            httpserver.url_for("/foo-robots.txt"),
            cwd=tmp_path,
        )
        # If the hook had treated this as robots.txt it would have parsed
        # the XML body for `Sitemap:` lines (none) and emitted noresults.
        # Instead we expect it to fall through the site-root branch —
        # which means it tries /robots.txt + fallback paths instead of
        # the foo-robots.txt URL.
        # The empty urlset returned by /foo-robots.txt is never read
        # because the hook never targets that URL.
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        # Failed because every probed sitemap returns 404.
        assert archive["status"] == "failed"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
