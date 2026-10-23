"""
Integration tests for infiniscroll plugin

Tests verify:
1. Hook script exists
2. Dependencies installed via chrome validation hooks
3. Verify deps with abxpkg
4. INFINISCROLL_ENABLED=False skips without JSONL
5. Fails gracefully when no chrome session exists
6. Full integration test: scrolls page and outputs stats
7. Config options work (scroll limit, min height)
"""

import gzip
import json
import re
import subprocess
import time
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import install_binary_with_abxpkg

# Import shared Chrome test helpers
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    get_test_env,
    chrome_session,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


PLUGIN_DIR = Path(__file__).parent.parent
INFINISCROLL_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_infiniscroll.*"), None)
TEST_URL = "https://example.com/"
CHROME_STARTUP_TIMEOUT_SECONDS = 45
INFINISCROLL_TEST_PAGE_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Infinite Scroll Test Page</title>
  <style>
    body { margin: 0; font-family: sans-serif; }
    #feed { max-width: 860px; margin: 0 auto; padding: 12px; }
    .card {
      margin: 12px 0;
      padding: 16px;
      min-height: 220px;
      border: 1px solid #ddd;
      border-radius: 8px;
      background: #f8f8f8;
    }
    #status {
      position: fixed;
      top: 0;
      right: 0;
      background: #111;
      color: #fff;
      padding: 8px 10px;
      font-size: 12px;
      border-bottom-left-radius: 8px;
    }
  </style>
</head>
<body>
  <div id="status">loads: 0</div>
  <p>Show more context is static text, not a control.</p>
  <a href="/i/connect_people?user_id=262147093">Show more</a>
  <a role="button" href="/explore/tabs/for-you"><span>Load more</span></a>
  <main id="feed"></main>
  <script>
    const feed = document.getElementById('feed');
    const status = document.getElementById('status');
    let loadCount = 0;
    const maxLoads = 5;
    let inFlight = false;

    function addCards(prefix, count) {
      for (let i = 0; i < count; i++) {
        const card = document.createElement('article');
        card.className = 'card';
        card.textContent = `${prefix} item ${i + 1}`;
        feed.appendChild(card);
      }
      status.textContent = `loads: ${loadCount}`;
    }

    function maybeLoadMore() {
      if (inFlight || loadCount >= maxLoads) return;
      const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 120;
      if (!nearBottom) return;

      inFlight = true;
      const nextLoad = loadCount + 1;
      setTimeout(() => {
        loadCount = nextLoad;
        addCards(`batch-${loadCount}`, 8);
        inFlight = false;
      }, 120);
    }

    addCards('initial', 8);
    window.addEventListener('scroll', () => {
      document.body.dataset.maxScroll = Math.max(Number(document.body.dataset.maxScroll || 0), window.scrollY);
      maybeLoadMore();
    }, { passive: true });
    window.addEventListener('load', maybeLoadMore);
  </script>
</body>
</html>
""".strip()


@pytest.fixture
def infiniscroll_test_url(httpserver):
    """Serve a deterministic page that appends DOM content while scrolling."""
    httpserver.expect_request("/").respond_with_data(
        INFINISCROLL_TEST_PAGE_HTML,
        content_type="text/html",
    )
    return httpserver.url_for("/").replace("localhost", "127.0.0.1", 1)


def save_dom(snapshot_chrome_dir, env, url):
    dom_dir = snapshot_chrome_dir.parent / "dom"
    dom_dir.mkdir(exist_ok=True)
    hook = PLUGIN_DIR.parent / "dom" / "on_Snapshot__53_dom.js"
    result = subprocess.run(
        [str(hook), f"--url={url}"],
        cwd=dom_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return (dom_dir / "output.html").read_text()


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://reddit.com/", True),
        ("https://old.reddit.com/r/popular/top/?t=day", True),
        ("https://reddit.com/r/trees/comments/123/title", False),
        ("https://reddit.com/r/trees/", False),
        ("https://reddit.com.evil.test/", False),
        ("https://m.facebook.com/home.php?sk=h_chr", True),
        ("https://facebook.com/?story_fbid=123", False),
        ("https://facebook.com/user/posts/123", False),
        ("https://www.instagram.com/", True),
        ("https://instagram.com/reels/", True),
        ("https://instagram.com/p/abc/", False),
        ("https://instagram.com/reel/abc/", False),
        ("https://www.tiktok.com/foryou", True),
        ("https://tiktok.com/@user/video/123", False),
        ("https://example.com/", False),
        ("invalid url", False),
    ],
)
def test_home_feed_detection(url, expected):
    result = subprocess.run(
        [
            "node",
            "-e",
            "console.log(require(process.argv[1]).isHomeFeed(process.argv[2]))",
            str(INFINISCROLL_HOOK),
            url,
        ],
        capture_output=True,
        text=True,
        env=get_test_env(),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected).lower()


def test_expansion_keeps_new_content_without_repeating_inert_buttons(
    httpserver,
    tmp_path,
):
    html = INFINISCROLL_TEST_PAGE_HTML.replace(
        '<main id="feed">',
        """
      <button onclick="document.body.dataset.inertClicks = Number(document.body.dataset.inertClicks || 0) + 1">Show replies</button>
      <button onclick="this.insertAdjacentHTML('beforebegin', '<p>Expanded real comment</p>'); this.remove()">Load more</button>
      <details><summary role="button">Show more</summary>Expanded details text</details>
      <main id="feed">""",
    )
    httpserver.expect_request("/").respond_with_data(html, content_type="text/html")
    url = httpserver.url_for("/").replace("localhost", "127.0.0.1", 1)
    with chrome_session(
        tmp_path,
        test_url=url,
        timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        env_overrides={
            "CHROME_WAIT_FOR": "domcontentloaded",
            "CHROME_RESOLUTION": "1280,720",
        },
    ) as (_, _, chrome_dir, env):
        env.update(
            {"INFINISCROLL_SCROLL_LIMIT": "1", "INFINISCROLL_SCROLL_DELAY": "500"},
        )
        started = time.monotonic()
        result = subprocess.run(
            [str(INFINISCROLL_HOOK), f"--url={url}"],
            cwd=chrome_dir.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert time.monotonic() - started < 8, result.stderr
        saved = save_dom(chrome_dir, env, url)
        assert "<p>Expanded real comment</p>" in saved
        assert '<details open="">' in saved
        assert 'data-inert-clicks="2"' in saved


def test_expansion_rechecks_aria_state_for_controls_sharing_a_native_popover(
    httpserver,
    tmp_path,
):
    html = """<!doctype html><html><body>
      <button popovertarget="replies" aria-expanded="false">Show more</button>
      <button popovertarget="replies" aria-expanded="false">Show replies</button>
      <div id="replies" popover="manual">Visible replies after expansion</div>
      <script>
        const replies = document.getElementById('replies');
        replies.addEventListener('toggle', event => {
          replies.dataset.state = event.newState;
          document.querySelectorAll('button').forEach(button => {
            button.setAttribute('aria-expanded', String(event.newState === 'open'));
          });
        });
        document.addEventListener('click', event => {
          if (event.target.matches('button')) {
            document.body.dataset.clicks = Number(document.body.dataset.clicks || 0) + 1;
          }
        }, true);
      </script></body></html>"""
    httpserver.expect_request("/").respond_with_data(html, content_type="text/html")
    url = httpserver.url_for("/").replace("localhost", "127.0.0.1", 1)
    with chrome_session(
        tmp_path,
        test_url=url,
        timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        env_overrides={"CHROME_WAIT_FOR": "domcontentloaded"},
    ) as (_, _, chrome_dir, env):
        env.update(
            {"INFINISCROLL_SCROLL_LIMIT": "1", "INFINISCROLL_SCROLL_DELAY": "500"},
        )
        result = subprocess.run(
            [str(INFINISCROLL_HOOK), f"--url={url}"],
            cwd=chrome_dir.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        saved = save_dom(chrome_dir, env, url)
        assert '<body data-clicks="1">' in saved
        assert 'id="replies" popover="manual" data-state="open"' in saved
        assert saved.count('aria-expanded="true"') == 2
        assert 'Clicked 1 "load more" buttons' in result.stderr


def test_comment_toggles_expand_without_collapsing_visible_replies(
    httpserver,
    tmp_path,
):
    """Run Reddit's real client handler, including two controls for one comment."""
    native_js = gzip.decompress(
        (Path(__file__).parent / "fixtures" / "reddit-init.js.gz").read_bytes(),
    )
    httpserver.expect_request("/reddit-init.js").respond_with_data(
        native_js,
        content_type="application/javascript",
    )
    controls = (
        '<a class="expand" href="javascript:void(0)" onclick="return togglecomment(this)">[+]</a>'
        '<a class="numchildren" href="javascript:void(0)" onclick="return togglecomment(this)">(1 child)</a>'
    )
    html = f"""<!doctype html><html><head>
      <style>.collapsed > .entry > .usertext, .collapsed > .child {{display:none}}</style>
      <script src="/reddit-init.js"></script></head><body>
      <div id="visible" class="thing comment noncollapsed"><div class="entry">
        {controls.replace("[+]", "[–]")}<div class="usertext">Already visible comment</div></div></div>
      <div id="hidden" class="thing comment collapsed"><div class="entry">
        {controls}<div class="usertext">Initially hidden comment</div></div>
        <div class="child"><div id="nested" class="thing comment noncollapsed"><div class="entry">
          {controls.replace("[+]", "[–]")}<div class="usertext">Nested visible reply</div></div></div></div>
      </div>
      <script>
        document.addEventListener('click', event => {{
          const control = event.target.closest('a[onclick]');
          if (!control) return;
          const comment = control.closest('.thing');
          comment.dataset.clicks = Number(comment.dataset.clicks || 0) + 1;
        }}, true);
      </script></body></html>"""
    httpserver.expect_request("/").respond_with_data(html, content_type="text/html")
    url = httpserver.url_for("/").replace("localhost", "127.0.0.1", 1)
    with chrome_session(
        tmp_path,
        test_url=url,
        timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        env_overrides={"CHROME_WAIT_FOR": "domcontentloaded"},
    ) as (_, _, chrome_dir, env):
        env.update(
            {"INFINISCROLL_SCROLL_LIMIT": "1", "INFINISCROLL_SCROLL_DELAY": "500"},
        )
        started = time.monotonic()
        result = subprocess.run(
            [str(INFINISCROLL_HOOK), f"--url={url}"],
            cwd=chrome_dir.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert time.monotonic() - started < 8, result.stderr
        saved = save_dom(chrome_dir, env, url)
        assert (
            '<div id="hidden" class="thing comment noncollapsed" data-clicks="1">'
            in saved
        )
        assert '<div id="visible" class="thing comment noncollapsed">' in saved
        assert '<div id="nested" class="thing comment noncollapsed">' in saved
        assert 'Clicked 1 "load more" buttons' in result.stderr
        assert "Initially hidden comment" in saved
        assert "Already visible comment" in saved
        assert "Nested visible reply" in saved


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert INFINISCROLL_HOOK is not None, "Infiniscroll hook not found"
    assert INFINISCROLL_HOOK.exists(), f"Hook not found: {INFINISCROLL_HOOK}"


def test_verify_deps_with_abxpkg():
    """Verify dependencies are available via abxpkg after hook installation."""
    node_loaded = install_binary_with_abxpkg(
        "node",
        binproviders="env,node,brew,apt",
    )
    assert node_loaded and node_loaded.abspath, (
        "Node.js required for infiniscroll plugin"
    )


def test_config_infiniscroll_disabled_skips():
    """Test that INFINISCROLL_ENABLED=False exits without emitting JSONL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        env = get_test_env() | {"SNAP_DIR": str(snap_dir)}
        env["INFINISCROLL_ENABLED"] = "False"

        result = subprocess.run(
            [
                str(INFINISCROLL_HOOK),
                f"--url={TEST_URL}",
                "--snapshot-id=test-disabled",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, (
            f"Should exit 0 when feature disabled: {result.stderr}"
        )
        assert "Skipping" in result.stderr or "False" in result.stderr, (
            "Should log skip reason to stderr"
        )

        jsonl_lines = [
            line
            for line in result.stdout.strip().split("\n")
            if line.strip().startswith("{")
        ]
        assert len(jsonl_lines) == 1, f"Expected skipped JSONL, got: {jsonl_lines}"
        result_json = json.loads(jsonl_lines[0])
        assert result_json["status"] == "skipped", result_json
        assert result_json["output_str"] == "INFINISCROLL_ENABLED=False", result_json


def test_fails_gracefully_without_chrome_session():
    """Test that hook fails gracefully when no chrome session exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        infiniscroll_dir = snap_dir / "infiniscroll"
        infiniscroll_dir.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [
                str(INFINISCROLL_HOOK),
                f"--url={TEST_URL}",
                "--snapshot-id=test-no-chrome",
            ],
            cwd=infiniscroll_dir,
            capture_output=True,
            text=True,
            env=get_test_env() | {"SNAP_DIR": str(snap_dir)},
            timeout=30,
        )

        # Should fail (exit 1) when no chrome session
        assert result.returncode != 0, "Should fail when no chrome session exists"
        # Error could be about chrome/CDP not found, or puppeteer module missing
        err_lower = result.stderr.lower()
        assert any(x in err_lower for x in ["chrome", "cdp", "puppeteer", "module"]), (
            f"Should mention chrome/CDP/puppeteer in error: {result.stderr}"
        )


def test_scrolls_page_and_outputs_stats(infiniscroll_test_url):
    """Integration test: scroll page and verify JSONL output format."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with chrome_session(
            Path(tmpdir),
            crawl_id="test-infiniscroll",
            snapshot_id="snap-infiniscroll",
            test_url=infiniscroll_test_url,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            env_overrides={
                "CHROME_WAIT_FOR": "domcontentloaded",
                "CHROME_RESOLUTION": "1280,720",
            },
        ) as (chrome_launch_process, chrome_pid, snapshot_chrome_dir, env):
            # Create infiniscroll output directory (sibling to chrome)
            infiniscroll_dir = snapshot_chrome_dir.parent / "infiniscroll"
            infiniscroll_dir.mkdir()

            # Run infiniscroll hook
            env["INFINISCROLL_SCROLL_LIMIT"] = "3"  # Limit scrolls for faster test
            env["INFINISCROLL_SCROLL_DELAY"] = "500"  # Faster scrolling
            env["INFINISCROLL_MIN_HEIGHT"] = "1000"  # Lower threshold for test
            env["INFINISCROLL_TIMEOUT"] = "8"

            result = subprocess.run(
                [
                    str(INFINISCROLL_HOOK),
                    f"--url={infiniscroll_test_url}",
                    "--snapshot-id=snap-infiniscroll",
                ],
                cwd=str(infiniscroll_dir),
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )

            assert result.returncode == 0, (
                f"Infiniscroll failed: {result.stderr}\nStdout: {result.stdout}"
            )
            assert "Clicked" not in result.stderr, result.stderr

            # Parse JSONL output
            result_json = None
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        record = json.loads(line)
                        if record.get("type") == "ArchiveResult":
                            result_json = record
                            break
                    except json.JSONDecodeError:
                        continue

            assert result_json is not None, (
                f"Should have ArchiveResult JSONL output. Stdout: {result.stdout}"
            )
            assert result_json["status"] == "succeeded", (
                f"Should succeed: {result_json}"
            )

            # Verify output_str format: "scrolled X,XXXpx"
            output_str = result_json.get("output_str", "")
            assert re.fullmatch(r"scrolled [\d,]+px", output_str), (
                f"output_str should contain only scrolled pixel count: {output_str}"
            )

            html = save_dom(snapshot_chrome_dir, env, infiniscroll_test_url)
            assert "batch-1 item 1</article>" in html, result.stderr + html
            assert 'href="/i/connect_people?user_id=262147093"' in html

            # Verify no files created in output directory
            output_files = list(infiniscroll_dir.iterdir())
            assert len(output_files) == 0, (
                f"Should not create any files, but found: {output_files}"
            )


def test_config_scroll_limit_honored(infiniscroll_test_url):
    """Test that INFINISCROLL_SCROLL_LIMIT config is respected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with chrome_session(
            Path(tmpdir),
            crawl_id="test-scroll-limit",
            snapshot_id="snap-limit",
            test_url=infiniscroll_test_url,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            env_overrides={
                "CHROME_WAIT_FOR": "domcontentloaded",
                "CHROME_RESOLUTION": "1280,720",
            },
        ) as (chrome_launch_process, chrome_pid, snapshot_chrome_dir, env):
            infiniscroll_dir = snapshot_chrome_dir.parent / "infiniscroll"
            infiniscroll_dir.mkdir()

            # Set scroll limit to 2 (use env from setup_chrome_session)
            env["INFINISCROLL_SCROLL_LIMIT"] = "2"
            env["INFINISCROLL_SCROLL_DISTANCE"] = "500"
            env["INFINISCROLL_SCROLL_DELAY"] = "500"
            env["INFINISCROLL_MIN_HEIGHT"] = (
                "100000"  # High threshold so limit kicks in
            )

            start_time = time.time()
            result = subprocess.run(
                [
                    str(INFINISCROLL_HOOK),
                    f"--url={infiniscroll_test_url}",
                    "--snapshot-id=snap-limit",
                ],
                cwd=str(infiniscroll_dir),
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
            elapsed = time.time() - start_time
            html = save_dom(snapshot_chrome_dir, env, infiniscroll_test_url)
            maximum = re.search(r'data-max-scroll="(\d+)"', html)
            assert maximum and 0 < int(maximum[1]) <= 1000, html
            assert "batch-1 item 1</article>" not in html

            assert result.returncode == 0, f"Infiniscroll failed: {result.stderr}"
            assert elapsed < 8, (
                f"INFINISCROLL_SCROLL_LIMIT=2 should stop after two short scroll delays, took {elapsed:.1f}s"
            )

            # Parse output and verify scroll count
            result_json = None
            for line in result.stdout.strip().split("\n"):
                if line.strip().startswith("{"):
                    try:
                        record = json.loads(line)
                        if record.get("type") == "ArchiveResult":
                            result_json = record
                            break
                    except json.JSONDecodeError:
                        continue

            assert result_json is not None, "Should have JSONL output"
            output_str = result_json.get("output_str", "")

            assert re.fullmatch(r"scrolled [\d,]+px", output_str), (
                f"Should have valid output_str: {output_str}"
            )
            scrolled_px = int(
                output_str.removeprefix("scrolled ")
                .removesuffix("px")
                .replace(",", ""),
            )
            assert scrolled_px > 0, result_json
            assert result_json["status"] == "succeeded", (
                f"Should succeed with scroll limit: {result_json}"
            )


def test_config_timeout_honored(infiniscroll_test_url):
    """Test that INFINISCROLL_TIMEOUT config is respected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with chrome_session(
            Path(tmpdir),
            crawl_id="test-timeout",
            snapshot_id="snap-timeout",
            test_url=infiniscroll_test_url,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            env_overrides={
                "CHROME_WAIT_FOR": "domcontentloaded",
                "CHROME_RESOLUTION": "1280,720",
            },
        ) as (chrome_launch_process, chrome_pid, snapshot_chrome_dir, env):
            infiniscroll_dir = snapshot_chrome_dir.parent / "infiniscroll"
            infiniscroll_dir.mkdir()

            # Set very short timeout (use env from setup_chrome_session)
            env["INFINISCROLL_TIMEOUT"] = "3"  # 3 seconds
            env["INFINISCROLL_SCROLL_DELAY"] = (
                "2000"  # 2s delay - timeout should trigger
            )
            env["INFINISCROLL_SCROLL_LIMIT"] = "100"  # High limit
            env["INFINISCROLL_MIN_HEIGHT"] = "100000"

            start_time = time.time()
            result = subprocess.run(
                [
                    str(INFINISCROLL_HOOK),
                    f"--url={infiniscroll_test_url}",
                    "--snapshot-id=snap-timeout",
                ],
                cwd=str(infiniscroll_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            elapsed = time.time() - start_time

            # Should complete within reasonable time (timeout + buffer)
            assert elapsed < 15, f"Should respect timeout, took {elapsed:.1f}s"
            assert result.returncode == 0, (
                f"Should complete even with timeout: {result.stderr}"
            )
            result_json = None
            for line in result.stdout.strip().split("\n"):
                if line.strip().startswith("{"):
                    record = json.loads(line)
                    if record.get("type") == "ArchiveResult":
                        result_json = record
                        break
            assert result_json is not None, result.stdout
            assert result_json["status"] == "succeeded", result_json
            assert re.fullmatch(
                r"scrolled [\d,]+px",
                result_json.get("output_str", ""),
            ), result_json


@pytest.mark.parametrize(
    "case",
    [
        "article",
        "short",
        "outside_details",
        "outside_button",
        "headline_mismatch",
        "canonical_mismatch",
        "ambiguous_schema",
        "ambiguous_main",
        "outside_article",
    ],
)
def test_article_scroll_covers_live_main_and_lazy_asset(httpserver, tmp_path, case):
    """The article boundary includes opened content; outside comments retain normal scrolling."""
    url = httpserver.url_for("/article").replace("localhost", "127.0.0.1", 1)
    schema = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "NewsArticle",
                "headline": "A complete long article",
                "mainEntityOfPage": {"@id": url},
            },
        ],
    }
    if case == "headline_mismatch":
        schema["@graph"][0]["headline"] = "Another article"
    elif case == "ambiguous_schema":
        schema["@graph"].append({**schema["@graph"][0], "headline": "Another article"})
    canonical = url + "/different" if case == "canonical_mismatch" else url
    outside = ""
    if case == "outside_details":
        outside = "<details><summary>Comments</summary><p>Valuable outside comments.</p></details>"
    elif case == "outside_button":
        outside = """<button onclick="this.insertAdjacentHTML('beforebegin', '<p>Valuable outside comments.</p>'); this.remove()">Load more</button>"""
    elif case == "ambiguous_main":
        outside = "<main>Another article with its own main.</main>"
    elif case == "outside_article":
        outside = '<article>Valuable outside continuation.<img loading="lazy" width="100" height="100" src="/continuation-image.svg"></article>'
    html = f'''<!doctype html><html><head><title>A complete long article</title>
      <link rel="canonical" href="{canonical}">
      <script type="application/ld+json">{json.dumps(schema)}</script>
      <style>body {{ margin: 0 }} main {{ margin: 0 }} h1 {{ margin: 0; height: 100px }} button {{ display: block; height: 20px; margin: 0 }}</style>
      </head><body><main><h1>A complete long article</h1>
      <p style="height: 2400px; margin: 0">The first part of the original article.</p>
      <details><summary>Continue reading</summary>
        <p style="height: 3000px; margin: 0">The rest of the same article must remain available.</p>
        <img loading="lazy" width="100" height="100" src="/article-image.svg">
        <p>The valuable original conclusion.</p>
        <button onclick="this.insertAdjacentHTML('beforebegin', '<p style=height:200px>Expanded real comment</p>'); this.remove()">Load more</button>
      </details></main>
      <footer style="height: 5000px">Other material below the article.</footer>
      {outside}
      <script>window.addEventListener('scroll', () => {{
        document.body.dataset.maxScroll = Math.max(Number(document.body.dataset.maxScroll || 0), window.scrollY);
      }}, {{passive: true}});</script></body></html>'''
    if case == "short":
        html = html.replace("height: 2400px", "height: 20px").replace(
            "height: 3000px",
            "height: 20px",
        )
    httpserver.expect_request("/article").respond_with_data(
        html,
        content_type="text/html",
    )
    httpserver.expect_request("/article-image.svg").respond_with_data(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="green"/></svg>',
        content_type="image/svg+xml",
    )
    httpserver.expect_request("/continuation-image.svg").respond_with_data(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="blue"/></svg>',
        content_type="image/svg+xml",
    )
    with chrome_session(
        tmp_path,
        test_url=url,
        timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        env_overrides={
            "CHROME_WAIT_FOR": "domcontentloaded",
            "CHROME_RESOLUTION": "1280,720",
        },
    ) as (_, _, chrome_dir, env):
        env.update({"INFINISCROLL_SCROLL_DELAY": "100"})
        result = subprocess.run(
            [str(INFINISCROLL_HOOK), f"--url={url}"],
            cwd=chrome_dir.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        # Read native layout/loading state without modifying the document.
        script = """const chrome = require(process.argv[1]);
          (async () => {
            const {browser, page} = await chrome.connectToPage({chromeSessionDir: process.argv[2],
              timeoutMs: 10000, waitForNavigationComplete: true, puppeteer: chrome.resolvePuppeteerModule()});
            console.log(JSON.stringify(await page.evaluate(() => ({
              maxScroll: Number(document.body.dataset.maxScroll || 0), viewport: innerHeight,
              bottom: document.querySelector('main').getBoundingClientRect().bottom + scrollY,
              imageLoaded: [...document.querySelectorAll('img')].every(img => img.complete && img.naturalWidth > 0),
              detailsOpen: [...document.querySelectorAll('details')].every(el => el.open),
              text: document.body.innerText, y: scrollY,
            }))));
            await browser.disconnect();
          })().catch(err => {console.error(err); process.exit(1)});"""
        state_result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                script,
                str(PLUGIN_DIR.parent / "chrome" / "chrome_utils.js"),
                str(chrome_dir),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert state_result.returncode == 0, state_result.stderr
        state = json.loads(state_result.stdout)
        assert state["detailsOpen"]
        assert state["imageLoaded"]
        assert "The valuable original conclusion." in state["text"]
        assert state["maxScroll"] + state["viewport"] >= state["bottom"] - 1
        assert state["y"] == 0
        assert "Expanded real comment" in state["text"]
        if case in {"outside_details", "outside_button"}:
            assert "Valuable outside comments." in state["text"]
        if case == "outside_article":
            assert "Valuable outside continuation." in state["text"]
        if case in {"article", "short"}:
            assert state["maxScroll"] <= max(0, state["bottom"] - state["viewport"]) + 1
        else:
            assert state["maxScroll"] + state["viewport"] > state["bottom"] + 1000


@pytest.mark.parametrize("control_type", ["checkbox", "radio"])
def test_expansion_keeps_native_label_disclosures_open(
    httpserver,
    tmp_path,
    control_type,
):
    html = INFINISCROLL_TEST_PAGE_HTML.replace(
        '<main id="feed">',
        f'''
      <style>
        #disclosure {{ display: none }}
        #disclosure:not(:checked) ~ label .hide,
        #disclosure:not(:checked) ~ .menu {{ display: none }}
        #disclosure:checked ~ label .show {{ display: none }}
      </style>
      <input id="disclosure" type="{control_type}"
        onchange="document.body.dataset.disclosureOpen = this.checked">
      <label role="button" for="disclosure"
        onclick="document.body.dataset.disclosureClicks = Number(document.body.dataset.disclosureClicks || 0) + 1">
        <span class="show">Show more</span><span class="hide">Hide expanded menu</span>
      </label>
      <nav class="menu">Expanded navigation content</nav>
      <main id="feed">''',
    )
    httpserver.expect_request("/").respond_with_data(html, content_type="text/html")
    url = httpserver.url_for("/").replace("localhost", "127.0.0.1", 1)
    with chrome_session(
        tmp_path,
        test_url=url,
        timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        env_overrides={"CHROME_WAIT_FOR": "domcontentloaded"},
    ) as (_, _, chrome_dir, env):
        env.update(
            {
                "INFINISCROLL_TIMEOUT": "8",
                "INFINISCROLL_SCROLL_LIMIT": "1",
                "INFINISCROLL_SCROLL_DELAY": "40",
            },
        )
        result = subprocess.run(
            [str(INFINISCROLL_HOOK), f"--url={url}"],
            cwd=chrome_dir.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        saved = save_dom(chrome_dir, env, url)
        assert 'data-disclosure-clicks="1"' in saved, result.stderr
        assert 'data-disclosure-open="true"' in saved
        assert "Expanded navigation content" in saved


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
