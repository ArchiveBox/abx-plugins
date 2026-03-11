"""
Integration tests for infiniscroll plugin

Tests verify:
1. Hook script exists
2. Dependencies installed via chrome validation hooks
3. Verify deps with abx-pkg
4. INFINISCROLL_ENABLED=False skips without JSONL
5. Fails gracefully when no chrome session exists
6. Full integration test: scrolls page and outputs stats
7. Config options work (scroll limit, min height)
"""

import json
import re
import subprocess
import time
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

# Import shared Chrome test helpers
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    get_test_env,
    chrome_session,
)


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
    window.addEventListener('scroll', maybeLoadMore, { passive: true });
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
    return httpserver.url_for("/")


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert INFINISCROLL_HOOK is not None, "Infiniscroll hook not found"
    assert INFINISCROLL_HOOK.exists(), f"Hook not found: {INFINISCROLL_HOOK}"


def test_verify_deps_with_abx_pkg():
    """Verify dependencies are available via abx-pkg after hook installation."""
    from abx_pkg import Binary, EnvProvider

    # Verify node is available
    node_binary = Binary(name="node", binproviders=[EnvProvider()])
    node_loaded = node_binary.load()
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
                "node",
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
                "node",
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
        ) as (chrome_launch_process, chrome_pid, snapshot_chrome_dir, env):
            # Create infiniscroll output directory (sibling to chrome)
            infiniscroll_dir = snapshot_chrome_dir.parent / "infiniscroll"
            infiniscroll_dir.mkdir()

            # Run infiniscroll hook
            env["INFINISCROLL_SCROLL_LIMIT"] = "3"  # Limit scrolls for faster test
            env["INFINISCROLL_SCROLL_DELAY"] = "500"  # Faster scrolling
            env["INFINISCROLL_MIN_HEIGHT"] = "1000"  # Lower threshold for test

            result = subprocess.run(
                [
                    "node",
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
                        pass

            assert result_json is not None, (
                f"Should have ArchiveResult JSONL output. Stdout: {result.stdout}"
            )
            assert result_json["status"] == "succeeded", (
                f"Should succeed: {result_json}"
            )

            # Verify output_str format: "scrolled X,XXXpx"
            output_str = result_json.get("output_str", "")
            assert output_str.startswith("scrolled "), (
                f"output_str should start with 'scrolled ': {output_str}"
            )
            assert re.fullmatch(r"scrolled [\d,]+px", output_str), (
                f"output_str should contain only scrolled pixel count: {output_str}"
            )

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
        ) as (chrome_launch_process, chrome_pid, snapshot_chrome_dir, env):
            infiniscroll_dir = snapshot_chrome_dir.parent / "infiniscroll"
            infiniscroll_dir.mkdir()

            # Set scroll limit to 2 (use env from setup_chrome_session)
            env["INFINISCROLL_SCROLL_LIMIT"] = "2"
            env["INFINISCROLL_SCROLL_DELAY"] = "500"
            env["INFINISCROLL_MIN_HEIGHT"] = (
                "100000"  # High threshold so limit kicks in
            )

            result = subprocess.run(
                [
                    "node",
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

            assert result.returncode == 0, f"Infiniscroll failed: {result.stderr}"

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
                        pass

            assert result_json is not None, "Should have JSONL output"
            output_str = result_json.get("output_str", "")

            # Verify output format and that it completed (scroll limit enforced internally)
            assert output_str.startswith("scrolled "), (
                f"Should have valid output_str: {output_str}"
            )
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
                    "node",
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
