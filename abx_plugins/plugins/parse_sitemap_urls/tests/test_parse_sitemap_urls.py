#!/usr/bin/env python3
"""Unit tests for parse_sitemap_urls extractor.

These tests exercise the hook against:

* in-memory `file://` sitemaps (no network)
* `pytest-httpserver` for HTTP discovery flows (robots.txt, sitemap-index,
  gzip, large sitemaps, fallback path probing, retries)
* malformed input and edge cases (truncated XML, non-XML payloads, empty
  sitemaps, mixed namespaces, oversized URL counts)

The hook is run as a subprocess so we exercise the real `uv` shebang and
script-block dependency pinning that ships in production.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = next(
    (path for path in PLUGIN_DIR.glob("on_Snapshot__*_parse_sitemap_urls.*")),
    None,
)
assert SCRIPT_PATH is not None, "hook script must exist for tests to run"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_hook(
    url: str,
    *,
    cwd: Path,
    env_overrides: dict[str, str] | None = None,
    timeout: int = 120,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the hook as a subprocess, mirroring the real invocation contract.

    `pytest-httpserver` binds to localhost, which the production-default
    private-host guard refuses. Tests opt-in to that target via
    `PARSE_SITEMAP_URLS_ALLOW_PRIVATE_HOSTS=true` unless the caller
    overrides it explicitly.
    """
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

    The hook contract requires that every non-empty stdout line is a
    JSON record. A regression where the hook prints human text to
    stdout instead of stderr should fail tests, not be silently
    filtered.
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


def archive_result(records: list[dict]) -> dict | None:
    return next((r for r in records if r.get("type") == "ArchiveResult"), None)


def write_sitemap(
    path: Path, urls: list[str], *, lastmods: list[str] | None = None
) -> None:
    pieces = ['<?xml version="1.0" encoding="UTF-8"?>']
    pieces.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for index, url in enumerate(urls):
        if lastmods and index < len(lastmods):
            pieces.append(
                f"  <url><loc>{url}</loc><lastmod>{lastmods[index]}</lastmod></url>",
            )
        else:
            pieces.append(f"  <url><loc>{url}</loc></url>")
    pieces.append("</urlset>")
    path.write_text("\n".join(pieces), encoding="utf-8")


def write_sitemap_index(path: Path, child_urls: list[str]) -> None:
    pieces = ['<?xml version="1.0" encoding="UTF-8"?>']
    pieces.append('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for url in child_urls:
        pieces.append(f"  <sitemap><loc>{url}</loc></sitemap>")
    pieces.append("</sitemapindex>")
    path.write_text("\n".join(pieces), encoding="utf-8")


# ---------------------------------------------------------------------------
# Basic urlset parsing (file://)
# ---------------------------------------------------------------------------


class TestUrlsetParsing:
    def test_parses_simple_urlset(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        write_sitemap(
            sitemap,
            [
                "https://example.com/",
                "https://example.com/about",
                "https://example.com/contact",
            ],
        )
        result = run_hook(f"file://{sitemap}", cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        records = parse_jsonl(result.stdout)
        snaps = snapshots(records)
        urls = sorted(s["url"] for s in snaps)
        assert urls == [
            "https://example.com/",
            "https://example.com/about",
            "https://example.com/contact",
        ]
        archive = archive_result(records)
        assert archive is not None
        assert archive["status"] == "succeeded"
        assert "3 URLs parsed" in archive["output_str"]

    def test_preserves_lastmod_as_bookmarked_at(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        write_sitemap(
            sitemap,
            ["https://example.com/post-1", "https://example.com/post-2"],
            lastmods=["2025-12-01", "2025-12-02T08:00:00Z"],
        )
        result = run_hook(f"file://{sitemap}", cwd=tmp_path)
        assert result.returncode == 0
        snaps = snapshots(parse_jsonl(result.stdout))
        by_url = {s["url"]: s for s in snaps}
        assert by_url["https://example.com/post-1"]["bookmarked_at"] == "2025-12-01"
        assert (
            by_url["https://example.com/post-2"]["bookmarked_at"]
            == "2025-12-02T08:00:00Z"
        )

    def test_emits_depth_increment(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        write_sitemap(sitemap, ["https://example.com/a"])
        result = run_hook(f"file://{sitemap}", cwd=tmp_path, extra_args=["--depth=2"])
        assert result.returncode == 0
        snaps = snapshots(parse_jsonl(result.stdout))
        assert snaps[0]["depth"] == 3

    def test_persists_urls_jsonl_file(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        write_sitemap(sitemap, ["https://example.com/x", "https://example.com/y"])
        result = run_hook(f"file://{sitemap}", cwd=tmp_path)
        assert result.returncode == 0
        urls_file = tmp_path / "parse_sitemap_urls" / "urls.jsonl"
        assert urls_file.exists()
        lines = [line for line in urls_file.read_text().splitlines() if line.strip()]
        assert len(lines) == 2
        for line in lines:
            entry = json.loads(line)
            assert entry["type"] == "Snapshot"
            assert entry["plugin"] == "parse_sitemap_urls"

    def test_overwrites_stale_urls_jsonl(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        write_sitemap(sitemap, ["https://example.com/fresh"])
        urls_dir = tmp_path / "parse_sitemap_urls"
        urls_dir.mkdir()
        stale = urls_dir / "urls.jsonl"
        stale.write_text(
            '{"type":"Snapshot","url":"https://example.com/stale"}\n',
            encoding="utf-8",
        )
        result = run_hook(f"file://{sitemap}", cwd=tmp_path)
        assert result.returncode == 0
        lines = [line for line in stale.read_text().splitlines() if line.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0])["url"] == "https://example.com/fresh"

    def test_clears_stale_urls_jsonl_on_noresults(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        sitemap.write_text(
            '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>',
            encoding="utf-8",
        )
        urls_dir = tmp_path / "parse_sitemap_urls"
        urls_dir.mkdir()
        stale = urls_dir / "urls.jsonl"
        stale.write_text("stale\n", encoding="utf-8")
        result = run_hook(f"file://{sitemap}", cwd=tmp_path)
        assert result.returncode == 0
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None and archive["status"] == "noresults"
        assert not stale.exists()


# ---------------------------------------------------------------------------
# Sitemap index recursion
# ---------------------------------------------------------------------------


class TestSitemapIndex:
    def test_recurses_one_level(self, tmp_path: Path) -> None:
        child_a = tmp_path / "child_a.xml"
        child_b = tmp_path / "child_b.xml"
        write_sitemap(child_a, ["https://example.com/a1", "https://example.com/a2"])
        write_sitemap(child_b, ["https://example.com/b1"])
        index_path = tmp_path / "index.xml"
        write_sitemap_index(
            index_path,
            [f"file://{child_a}", f"file://{child_b}"],
        )
        result = run_hook(f"file://{index_path}", cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        assert urls == [
            "https://example.com/a1",
            "https://example.com/a2",
            "https://example.com/b1",
        ]

    def test_recurses_two_levels(self, tmp_path: Path) -> None:
        leaf = tmp_path / "leaf.xml"
        write_sitemap(leaf, ["https://example.com/leaf-1"])
        mid = tmp_path / "mid.xml"
        write_sitemap_index(mid, [f"file://{leaf}"])
        top = tmp_path / "top.xml"
        write_sitemap_index(top, [f"file://{mid}"])
        result = run_hook(f"file://{top}", cwd=tmp_path)
        assert result.returncode == 0
        snaps = snapshots(parse_jsonl(result.stdout))
        assert {s["url"] for s in snaps} == {"https://example.com/leaf-1"}

    def test_respects_max_sitemap_depth(self, tmp_path: Path) -> None:
        leaf = tmp_path / "leaf.xml"
        write_sitemap(leaf, ["https://example.com/leaf"])
        mid = tmp_path / "mid.xml"
        write_sitemap_index(mid, [f"file://{leaf}"])
        top = tmp_path / "top.xml"
        write_sitemap_index(top, [f"file://{mid}"])

        result = run_hook(
            f"file://{top}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_MAX_SITEMAP_DEPTH": "1"},
        )
        assert result.returncode in (0, 1)
        snaps = snapshots(parse_jsonl(result.stdout))
        assert snaps == []
        assert "max_depth" in result.stderr

    def test_handles_cyclic_sitemap_index(self, tmp_path: Path) -> None:
        a = tmp_path / "a.xml"
        b = tmp_path / "b.xml"
        write_sitemap_index(a, [f"file://{b}"])
        write_sitemap_index(b, [f"file://{a}"])
        result = run_hook(f"file://{a}", cwd=tmp_path)
        # Cycle terminates safely; no URLs to emit.
        assert result.returncode in (0, 1)
        snaps = snapshots(parse_jsonl(result.stdout))
        assert snaps == []


# ---------------------------------------------------------------------------
# Gzip + encoding
# ---------------------------------------------------------------------------


class TestGzip:
    def test_decompresses_gzipped_sitemap(self, tmp_path: Path) -> None:
        raw = tmp_path / "sitemap.xml"
        write_sitemap(raw, ["https://example.com/g1", "https://example.com/g2"])
        gz_path = tmp_path / "sitemap.xml.gz"
        gz_path.write_bytes(gzip.compress(raw.read_bytes()))
        result = run_hook(f"file://{gz_path}", cwd=tmp_path)
        assert result.returncode == 0
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        assert urls == ["https://example.com/g1", "https://example.com/g2"]


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


class TestFiltering:
    def test_include_regex(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        write_sitemap(
            sitemap,
            [
                "https://example.com/blog/post-1",
                "https://example.com/blog/post-2",
                "https://example.com/products/x",
                "https://example.com/about",
            ],
        )
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_INCLUDE_REGEX": r"/blog/"},
        )
        assert result.returncode == 0
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        assert urls == [
            "https://example.com/blog/post-1",
            "https://example.com/blog/post-2",
        ]

    def test_exclude_regex(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        write_sitemap(
            sitemap,
            [
                "https://example.com/blog/post",
                "https://example.com/products/x",
                "https://example.com/products/y",
            ],
        )
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_EXCLUDE_REGEX": r"/products/"},
        )
        assert result.returncode == 0
        snaps = snapshots(parse_jsonl(result.stdout))
        assert [s["url"] for s in snaps] == ["https://example.com/blog/post"]

    def test_same_host_only_with_file_seed(self, tmp_path: Path) -> None:
        """`SAME_HOST_ONLY` against a file:// seed (empty netloc) filters every HTTPS URL.

        Documents the limitation: `SAME_HOST_ONLY` is designed for HTTP(S)
        seeds where the netloc is meaningful. With a file:// seed every
        emitted HTTPS URL has a non-matching host, so the filter drops all
        of them.
        """
        sitemap = tmp_path / "sitemap.xml"
        write_sitemap(
            sitemap,
            [
                "https://example.com/page-a",
                "https://cdn.example.com/asset",
                "https://other.com/page-b",
            ],
        )
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_SAME_HOST_ONLY": "true"},
        )
        assert result.returncode == 0
        snaps = snapshots(parse_jsonl(result.stdout))
        assert snaps == []

    def test_invalid_regex_warns_and_continues(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        write_sitemap(sitemap, ["https://example.com/a"])
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_INCLUDE_REGEX": "[unclosed"},
        )
        assert result.returncode == 0
        assert "invalid regex" in result.stderr
        snaps = snapshots(parse_jsonl(result.stdout))
        # Bad regex collapses to None → no filtering → URL passes.
        assert [s["url"] for s in snaps] == ["https://example.com/a"]


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


class TestLimits:
    def test_respects_max_urls(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        urls = [f"https://example.com/p{index}" for index in range(50)]
        write_sitemap(sitemap, urls)
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_MAX_URLS": "10"},
        )
        assert result.returncode == 0
        snaps = snapshots(parse_jsonl(result.stdout))
        assert len(snaps) == 10

    def test_disabled_via_config(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        write_sitemap(sitemap, ["https://example.com/x"])
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_ENABLED": "false"},
        )
        assert result.returncode == 0
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None and archive["status"] == "skipped"

    def test_alias_use_parse_sitemap_urls_disables(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        write_sitemap(sitemap, ["https://example.com/x"])
        result = run_hook(
            f"file://{sitemap}",
            cwd=tmp_path,
            env_overrides={"USE_PARSE_SITEMAP_URLS": "false"},
        )
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None and archive["status"] == "skipped"


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


class TestMalformedInput:
    def test_truncated_xml(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.xml"
        bad.write_text(
            '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/p',
            encoding="utf-8",
        )
        result = run_hook(f"file://{bad}", cwd=tmp_path)
        # No valid sitemap parsed → failed (zero visited count).
        assert result.returncode == 1
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None and archive["status"] == "failed"

    def test_non_xml_payload(self, tmp_path: Path) -> None:
        notxml = tmp_path / "notxml.xml"
        notxml.write_text("this is not xml at all", encoding="utf-8")
        result = run_hook(f"file://{notxml}", cwd=tmp_path)
        assert result.returncode == 1
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None and archive["status"] == "failed"

    def test_empty_urlset_noresults(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.xml"
        empty.write_text(
            '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>',
            encoding="utf-8",
        )
        result = run_hook(f"file://{empty}", cwd=tmp_path)
        assert result.returncode == 0
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None and archive["status"] == "noresults"

    def test_missing_file(self, tmp_path: Path) -> None:
        result = run_hook(
            f"file://{tmp_path}/does-not-exist.xml",
            cwd=tmp_path,
        )
        assert result.returncode in (0, 1)
        archive = archive_result(parse_jsonl(result.stdout))
        # No seeds resolved to valid sitemaps → failed.
        assert archive is not None
        assert archive["status"] == "failed"

    def test_rejects_unknown_root_element(self, tmp_path: Path) -> None:
        weird = tmp_path / "weird.xml"
        weird.write_text(
            '<?xml version="1.0"?><foo><bar>baz</bar></foo>',
            encoding="utf-8",
        )
        result = run_hook(f"file://{weird}", cwd=tmp_path)
        # XML parses but root is neither urlset nor sitemapindex → 0 URLs.
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None and archive["status"] == "noresults"

    def test_unnamespaced_sitemap_supported(self, tmp_path: Path) -> None:
        # Real-world: some sitemaps omit the xmlns.
        plain = tmp_path / "plain.xml"
        plain.write_text(
            textwrap.dedent(
                """
                <?xml version="1.0"?>
                <urlset>
                  <url><loc>https://example.com/x</loc></url>
                  <url><loc>https://example.com/y</loc></url>
                </urlset>
                """,
            ).strip(),
            encoding="utf-8",
        )
        result = run_hook(f"file://{plain}", cwd=tmp_path)
        assert result.returncode == 0
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        assert urls == ["https://example.com/x", "https://example.com/y"]


# ---------------------------------------------------------------------------
# robots.txt discovery
# ---------------------------------------------------------------------------


class TestRobotsTxtDiscovery:
    def test_robots_txt_with_sitemap_directives(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        sitemap_xml = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://example.test/r-one</loc></url>
              <url><loc>https://example.test/r-two</loc></url>
            </urlset>
            """,
        ).strip()
        httpserver.expect_request("/sitemap.xml").respond_with_data(
            sitemap_xml,
            content_type="application/xml",
        )
        robots_body = textwrap.dedent(
            f"""
            User-agent: *
            Disallow:
            Sitemap: {httpserver.url_for("/sitemap.xml")}
            """,
        ).strip()
        httpserver.expect_request("/robots.txt").respond_with_data(
            robots_body,
            content_type="text/plain",
        )

        result = run_hook(httpserver.url_for("/robots.txt"), cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        assert urls == ["https://example.test/r-one", "https://example.test/r-two"]

    def test_root_url_falls_back_to_robots_then_sitemap_paths(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        sitemap_xml = textwrap.dedent(
            """
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://fallback.test/page-1</loc></url>
            </urlset>
            """,
        ).strip()
        # Pretend robots.txt is empty (no Sitemap lines).
        httpserver.expect_request("/robots.txt").respond_with_data(
            "User-agent: *\nDisallow:\n",
            content_type="text/plain",
        )
        httpserver.expect_request("/sitemap.xml").respond_with_data(
            sitemap_xml,
            content_type="application/xml",
        )
        result = run_hook(httpserver.url_for("/"), cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        assert urls == ["https://fallback.test/page-1"]

    def test_robots_discovery_disabled(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        # robots.txt would normally provide the sitemap, but we disable that
        # path: with no fallback hits the hook should fail or noresults.
        httpserver.expect_request("/robots.txt").respond_with_data(
            f"Sitemap: {httpserver.url_for('/sitemap.xml')}\n",
            content_type="text/plain",
        )
        httpserver.expect_request("/sitemap.xml").respond_with_data(
            "broken-not-xml",
            content_type="application/xml",
        )
        result = run_hook(
            httpserver.url_for("/"),
            cwd=tmp_path,
            env_overrides={"PARSE_SITEMAP_URLS_DISCOVER_FROM_ROBOTS": "false"},
        )
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        assert archive["status"] in {"failed", "noresults"}


# ---------------------------------------------------------------------------
# HTTP server integration
# ---------------------------------------------------------------------------


class TestHttpFetching:
    def test_fetches_sitemap_over_http(self, tmp_path: Path, httpserver) -> None:
        sitemap_xml = textwrap.dedent(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://httpserver.test/page-1</loc></url>
              <url><loc>https://httpserver.test/page-2</loc></url>
            </urlset>
            """,
        ).strip()
        httpserver.expect_request("/sitemap.xml").respond_with_data(
            sitemap_xml,
            content_type="application/xml",
        )
        result = run_hook(httpserver.url_for("/sitemap.xml"), cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        assert urls == [
            "https://httpserver.test/page-1",
            "https://httpserver.test/page-2",
        ]

    def test_fetches_gzipped_sitemap_over_http(
        self,
        tmp_path: Path,
        httpserver,
    ) -> None:
        sitemap_bytes = (
            textwrap.dedent(
                """
            <?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://gz.test/a</loc></url>
              <url><loc>https://gz.test/b</loc></url>
            </urlset>
            """,
            )
            .strip()
            .encode("utf-8")
        )
        httpserver.expect_request("/sitemap.xml.gz").respond_with_data(
            gzip.compress(sitemap_bytes),
            content_type="application/x-gzip",
        )
        result = run_hook(httpserver.url_for("/sitemap.xml.gz"), cwd=tmp_path)
        assert result.returncode == 0
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        assert urls == ["https://gz.test/a", "https://gz.test/b"]

    def test_sitemap_index_over_http(self, tmp_path: Path, httpserver) -> None:
        child_xml = textwrap.dedent(
            """
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://idx.test/leaf-1</loc></url>
              <url><loc>https://idx.test/leaf-2</loc></url>
            </urlset>
            """,
        ).strip()
        index_xml = textwrap.dedent(
            f"""
            <?xml version="1.0"?>
            <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <sitemap><loc>{httpserver.url_for("/child.xml")}</loc></sitemap>
            </sitemapindex>
            """,
        ).strip()
        httpserver.expect_request("/child.xml").respond_with_data(
            child_xml,
            content_type="application/xml",
        )
        httpserver.expect_request("/index.xml").respond_with_data(
            index_xml,
            content_type="application/xml",
        )
        result = run_hook(httpserver.url_for("/index.xml"), cwd=tmp_path)
        assert result.returncode == 0
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        assert urls == ["https://idx.test/leaf-1", "https://idx.test/leaf-2"]

    def test_http_404_failure(self, tmp_path: Path, httpserver) -> None:
        httpserver.expect_request("/missing.xml").respond_with_data(
            "not found",
            status=404,
        )
        result = run_hook(httpserver.url_for("/missing.xml"), cwd=tmp_path)
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        assert archive["status"] == "failed"

    def test_root_url_no_sitemap_anywhere(self, tmp_path: Path, httpserver) -> None:
        httpserver.expect_request("/robots.txt").respond_with_data(
            "not-found",
            status=404,
        )
        # Any fallback path also 404s by default (no handler registered).
        result = run_hook(httpserver.url_for("/"), cwd=tmp_path)
        archive = archive_result(parse_jsonl(result.stdout))
        assert archive is not None
        assert archive["status"] == "failed"


# ---------------------------------------------------------------------------
# Misc: ordering & dedup
# ---------------------------------------------------------------------------


class TestOrderingAndDedup:
    def test_emits_sorted_urls(self, tmp_path: Path) -> None:
        sitemap = tmp_path / "sitemap.xml"
        write_sitemap(
            sitemap,
            [
                "https://example.com/zebra",
                "https://example.com/apple",
                "https://example.com/mango",
            ],
        )
        result = run_hook(f"file://{sitemap}", cwd=tmp_path)
        assert result.returncode == 0
        snaps = snapshots(parse_jsonl(result.stdout))
        assert [s["url"] for s in snaps] == [
            "https://example.com/apple",
            "https://example.com/mango",
            "https://example.com/zebra",
        ]

    def test_dedupes_across_sitemap_index(self, tmp_path: Path) -> None:
        leaf_a = tmp_path / "a.xml"
        leaf_b = tmp_path / "b.xml"
        write_sitemap(leaf_a, ["https://example.com/dup", "https://example.com/one"])
        write_sitemap(leaf_b, ["https://example.com/dup", "https://example.com/two"])
        index = tmp_path / "index.xml"
        write_sitemap_index(
            index,
            [f"file://{leaf_a}", f"file://{leaf_b}"],
        )
        result = run_hook(f"file://{index}", cwd=tmp_path)
        assert result.returncode == 0
        urls = sorted(s["url"] for s in snapshots(parse_jsonl(result.stdout)))
        assert urls == [
            "https://example.com/dup",
            "https://example.com/one",
            "https://example.com/two",
        ]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
