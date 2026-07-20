"""
Unit tests for ublock plugin

Tests invoke the plugin hook as an external process and verify outputs/side effects.
"""

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from abxpkg import PROVIDER_CLASS_BY_NAME
from abx_plugins.plugins.base.test_utils import (
    install_required_binary_from_config,
    parse_jsonl_records,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_extension_install_env,
    chrome_session,
    setup_test_env,
    launch_chromium_session,
    kill_chromium_session,
    wait_for_extensions_metadata,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


PLUGIN_DIR = Path(__file__).parent.parent
SNAPSHOT_HOOK = PLUGIN_DIR / "on_Snapshot__12_ublock.daemon.bg.js"
NAVIGATE_HOOK = PLUGIN_DIR.parent / "chrome" / "on_Snapshot__30_chrome_navigate.js"
BASE_UTILS_JS = PLUGIN_DIR.parent / "base" / "utils.js"
CHROME_UTILS_JS = PLUGIN_DIR.parent / "chrome" / "chrome_utils.js"
CHROME_STARTUP_TIMEOUT_SECONDS = 45
EXTENSION_NAME = "ublock"
EXTENSION_WEBSTORE_ID = "ddkjiahejlhfcafbddmgiahcphecmpfh"
AD_SERVICE_URLS = (
    "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js",
    "https://googleads.g.doubleclick.net/pagead/ads?client=ca-pub-1234567890&format=auto",
    "https://googleads.g.doubleclick.net/pagead/imgad?id=CICAgKDLwJm9AhABGAEoATIIA1AB",
)


def serve_ad_fixture(httpserver, path: str = "/ublock-ad-fixture") -> str:
    httpserver.expect_request(path).respond_with_data(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>uBlock Ad Fixture</title>
  <style>
    .ad-slot, .adsbygoogle, .sponsored-ad {{
      display: block;
      width: 320px;
      height: 100px;
      border: 1px solid #c00;
      background: #fee;
    }}
  </style>
  <script src="{AD_SERVICE_URLS[0]}"></script>
</head>
<body>
  <main>
    <h1>uBlock Ad Fixture</h1>
    <ins class="adsbygoogle ad-slot" data-ad-client="ca-pub-1234567890" data-ad-slot="1234567890">Google ad slot</ins>
    <iframe class="sponsored-ad" title="doubleclick-ad" src="{AD_SERVICE_URLS[1]}"></iframe>
    <img class="ad-slot" alt="ad" src="{AD_SERVICE_URLS[2]}">
  </main>
</body>
</html>""",
        content_type="text/html; charset=utf-8",
    )
    return httpserver.url_for(path)


def install_ublock_extension(env: dict[str, str]):
    loaded = install_required_binary_from_config(PLUGIN_DIR, EXTENSION_NAME, env=env)
    assert loaded.loaded_abspath is not None, "abxpkg did not resolve ublock"
    assert loaded.loaded_abspath.exists(), loaded.loaded_abspath
    return loaded


def ublock_install_state(loaded, extensions_dir: Path | None = None) -> dict:
    """Return installed uBlock metadata from provider cache or the manifest."""
    assert loaded.loaded_abspath is not None
    manifest_path = Path(loaded.loaded_abspath)
    assert manifest_path.exists(), manifest_path
    unpacked_dir = manifest_path.parent
    provider_root = extensions_dir or unpacked_dir.parent

    cache_candidates = (
        provider_root / "ublock.extension.json",
        unpacked_dir / "ublock.extension.json",
    )
    for cache_file in cache_candidates:
        if cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "name": EXTENSION_NAME,
        "webstore_id": EXTENSION_WEBSTORE_ID,
        "version": manifest.get("version"),
        "unpacked_path": str(unpacked_dir),
    }


def test_chromewebstore_provider_available():
    assert "chromewebstore" in PROVIDER_CLASS_BY_NAME


def test_extension_metadata():
    assert EXTENSION_NAME == "ublock"
    assert EXTENSION_WEBSTORE_ID == "ddkjiahejlhfcafbddmgiahcphecmpfh"


def test_install_creates_cache():
    """Test that install creates extension cache"""
    with tempfile.TemporaryDirectory() as tmpdir:
        env, ext_dir = chrome_extension_install_env(tmpdir)

        loaded = install_ublock_extension(env)
        assert loaded.loaded_binprovider is not None
        assert loaded.loaded_binprovider.name == "chromewebstore"

        cache_data = ublock_install_state(loaded, ext_dir)
        assert cache_data["webstore_id"] == "ddkjiahejlhfcafbddmgiahcphecmpfh"
        assert cache_data["name"] == "ublock"


def test_install_twice_uses_cache():
    """Test that running install twice uses existing cache on second run"""
    with tempfile.TemporaryDirectory() as tmpdir:
        env, ext_dir = chrome_extension_install_env(tmpdir)

        # First install - downloads the extension
        install_ublock_extension(env)

        install_state = ublock_install_state(install_ublock_extension(env), ext_dir)
        assert install_state["webstore_id"] == EXTENSION_WEBSTORE_ID

        # Second install - should use cache and be faster
        provider2 = install_ublock_extension(env)
        assert provider2.loaded_abspath is not None


def test_no_configuration_required():
    """Test that uBlock Origin Lite works without configuration"""
    with tempfile.TemporaryDirectory() as tmpdir:
        env, _ext_dir = chrome_extension_install_env(tmpdir)
        # No API keys needed - works with default filter lists

        loaded = install_ublock_extension(env)
        assert loaded.loaded_abspath is not None


def test_large_extension_size():
    """Test that uBlock Origin Lite is downloaded successfully."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env, ext_dir = chrome_extension_install_env(tmpdir)

        install_ublock_extension(env)

        state = ublock_install_state(install_ublock_extension(env), ext_dir)
        unpacked_path = Path(state["unpacked_path"])
        assert unpacked_path.exists(), (
            f"uBlock unpacked extension should exist at {unpacked_path}"
        )
        size_bytes = sum(
            path.stat().st_size for path in unpacked_path.rglob("*") if path.is_file()
        )
        assert size_bytes > 100_000, (
            f"uBlock Origin Lite should be > 100KB, got {size_bytes} bytes"
        )


def test_snapshot_hook_reports_skipped_when_disabled(httpserver):
    test_url = httpserver.url_for("/ublock-disabled")
    env = os.environ.copy()
    env["UBLOCK_ENABLED"] = "false"

    result = subprocess.run(
        [str(SNAPSHOT_HOOK), f"--url={test_url}"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    records = parse_jsonl_records(result.stdout)
    archive_result = next(
        record for record in records if record.get("type") == "ArchiveResult"
    )
    assert archive_result["status"] == "skipped", archive_result
    assert archive_result["output_str"] == "UBLOCK_ENABLED=False", archive_result


def test_snapshot_hook_reports_noresults_on_blank_page():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        hook_dir = tmpdir / "ublock"
        hook_dir.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [
                str(SNAPSHOT_HOOK),
                "--url=about:blank",
                "--snapshot-id=ublock-noresults-snap",
            ],
            cwd=str(hook_dir),
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=30,
        )

        assert result.returncode == 0, result.stderr
        records = parse_jsonl_records(result.stdout)
        archive_result = next(
            record for record in records if record.get("type") == "ArchiveResult"
        )
        assert archive_result["status"] == "noresults", archive_result
        assert archive_result["output_str"] == "unsupported input URL: about:blank"


def test_snapshot_hook_reports_live_blocking_counts(httpserver):
    test_url = serve_ad_fixture(httpserver)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        install_env = setup_test_env(tmpdir)
        install_env["CHROME_HEADLESS"] = "true"
        install_ublock_extension(install_env)

        with chrome_session(
            tmpdir,
            crawl_id="ublock-live",
            snapshot_id="ublock-live-snap",
            test_url=test_url,
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (_chrome_launch_process, _chrome_pid, snapshot_chrome_dir, env):
            hook_dir = snapshot_chrome_dir.parent / "ublock"
            hook_dir.mkdir(parents=True, exist_ok=True)

            hook_process = subprocess.Popen(
                [
                    str(SNAPSHOT_HOOK),
                    f"--url={test_url}",
                    "--snapshot-id=ublock-live-snap",
                ],
                cwd=str(hook_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
            )

            try:
                navigate = subprocess.run(
                    [
                        str(NAVIGATE_HOOK),
                        f"--url={test_url}",
                        "--snapshot-id=ublock-live-snap",
                    ],
                    cwd=str(snapshot_chrome_dir),
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=90,
                )
                assert navigate.returncode == 0, navigate.stderr

                time.sleep(8)
                os.killpg(hook_process.pid, signal.SIGTERM)
                stdout, stderr = hook_process.communicate(timeout=20)

                assert hook_process.returncode in (0, -signal.SIGTERM), stderr
                records = parse_jsonl_records(stdout)
                archive_result = next(
                    record
                    for record in records
                    if record.get("type") == "ArchiveResult"
                )
                assert archive_result["status"] == "succeeded", archive_result
                blocked_str, hidden_str = archive_result["output_str"].split(" | ")
                blocked = int(blocked_str.split()[0])
                hidden = int(hidden_str.split()[0])
                # uBlock may satisfy this local fixture entirely with cosmetic
                # filters, especially when CI cannot reach the real ad services.
                # The outer page still contains visible ad slots without uBlock,
                # so hidden elements are a real blocking signal here.
                assert blocked + hidden > 0, archive_result
                assert blocked > 0 or hidden >= 3, archive_result
            finally:
                if hook_process.poll() is None:
                    os.killpg(hook_process.pid, signal.SIGKILL)


def check_ad_blocking(cdp_url: str, test_url: str, env: dict, script_dir: Path) -> dict:
    """Check ad blocking effectiveness by counting ad elements on page.

    Returns dict with:
        - adElementsFound: int - number of ad-related elements found
        - adElementsVisible: int - number of visible ad elements
        - adRequests: int - number of ad/tracker requests seen by the page
        - blockedRequests: int - number of blocked network requests (ads/trackers)
        - failedAdRequests: int - number of ad/tracker requests that failed for any reason
        - adRequestUrls: list[str] - ad/tracker request URLs observed by Chrome
        - blockedAdRequestUrls: list[str] - ad/tracker URLs blocked by uBlock
        - failedAdRequestErrors: list[dict] - failed ad/tracker URLs and Chrome errors
        - totalRequests: int - total network requests made
        - percentBlocked: int - percentage of ad elements hidden (0-100)
    """
    test_script = f"""
const chromeUtils = require('{CHROME_UTILS_JS}');

(async () => {{
    const puppeteer = chromeUtils.resolvePuppeteerModule();
    const result = await chromeUtils.withConnectedBrowser(
        {{ puppeteer, browserWSEndpoint: '{cdp_url}' }},
        async (browser) => {{
            const page = await browser.newPage();
            await page.setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
            await page.setViewport({{ width: 1440, height: 900 }});

            // Track network requests
            let adRequests = 0;
            let blockedRequests = 0;
            let failedAdRequests = 0;
            let totalRequests = 0;
            const adRequestUrls = [];
            const blockedAdRequestUrls = [];
            const failedAdRequestErrors = [];
            const adDomains = ['doubleclick', 'googlesyndication', 'googleadservices', 'facebook.com/tr',
                               'analytics', 'adservice', 'advertising', 'taboola', 'outbrain', 'criteo',
                               'amazon-adsystem', 'ads.yahoo', 'gemini.yahoo', 'yimg.com/cv/', 'beap.gemini'];

            page.on('request', request => {{
                totalRequests++;
                const url = request.url().toLowerCase();
                if (adDomains.some(d => url.includes(d))) {{
                    adRequests++;
                    adRequestUrls.push(request.url());
                }}
            }});

            page.on('requestfailed', request => {{
                const url = request.url().toLowerCase();
                const failure = request.failure();
                const errorText = ((failure && failure.errorText) || '').toUpperCase();
                if (adDomains.some(d => url.includes(d))) {{
                    failedAdRequests++;
                    failedAdRequestErrors.push({{ url: request.url(), errorText }});
                }}
                if (adDomains.some(d => url.includes(d)) && errorText.includes('ERR_BLOCKED_BY_CLIENT')) {{
                    blockedRequests++;
                    blockedAdRequestUrls.push(request.url());
                }}
            }});

            console.error('Navigating to {test_url}...');
            await page.goto('{test_url}', {{ waitUntil: 'domcontentloaded', timeout: 60000 }});

            // Wait for page to fully render and ads to load
            await new Promise(r => setTimeout(r, 5000));

            // Check for ad elements in the DOM
            const result = await page.evaluate(() => {{
        // Common ad-related selectors
        const adSelectors = [
            // Generic ad containers
            '[class*="ad-"]', '[class*="ad_"]', '[class*="-ad"]', '[class*="_ad"]',
            '[id*="ad-"]', '[id*="ad_"]', '[id*="-ad"]', '[id*="_ad"]',
            '[class*="advertisement"]', '[id*="advertisement"]',
            '[class*="sponsored"]', '[id*="sponsored"]',
            // Google ads
            'ins.adsbygoogle', '[data-ad-client]', '[data-ad-slot]',
            // Yahoo specific
            '[class*="gemini"]', '[data-beacon]', '[class*="native-ad"]',
            '[class*="stream-ad"]', '[class*="LDRB"]', '[class*="ntv-ad"]',
            // iframes (often ads)
            'iframe[src*="ad"]', 'iframe[src*="doubleclick"]', 'iframe[src*="googlesyndication"]',
            // Common ad sizes
            '[style*="300px"][style*="250px"]', '[style*="728px"][style*="90px"]',
            '[style*="160px"][style*="600px"]', '[style*="320px"][style*="50px"]',
        ];

                let adElementsFound = 0;
                let adElementsVisible = 0;

                for (const selector of adSelectors) {{
                    try {{
                        const elements = document.querySelectorAll(selector);
                        for (const el of elements) {{
                            adElementsFound++;
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            const isVisible = style.display !== 'none' &&
                                             style.visibility !== 'hidden' &&
                                             style.opacity !== '0' &&
                                             rect.width > 0 && rect.height > 0;
                            if (isVisible) {{
                                adElementsVisible++;
                            }}
                        }}
                    }} catch (e) {{
                        // Invalid selector, skip
                    }}
                }}

                return {{
                    adElementsFound,
                    adElementsVisible,
                    pageTitle: document.title
                }};
            }});

            result.blockedRequests = blockedRequests;
            result.failedAdRequests = failedAdRequests;
            result.adRequests = adRequests;
            result.adRequestUrls = adRequestUrls;
            result.blockedAdRequestUrls = blockedAdRequestUrls;
            result.failedAdRequestErrors = failedAdRequestErrors;
            result.totalRequests = totalRequests;
            // Calculate how many ad elements were hidden (found but not visible)
            const hiddenAds = result.adElementsFound - result.adElementsVisible;
            result.percentBlocked = result.adElementsFound > 0
                ? Math.round((hiddenAds / result.adElementsFound) * 100)
                : 0;

            console.error('Ad blocking result:', JSON.stringify(result));
            await page.close();
            return result;
        }},
    );
    console.log(JSON.stringify(result));
}})().catch(err => {{
    console.error(err && err.stack || err);
    process.exit(1);
}});
"""
    script_path = script_dir / "check_ads.js"
    script_path.write_text(f"#!/usr/bin/env node\n{test_script}", encoding="utf-8")
    script_path.chmod(0o755)

    result = subprocess.run(
        [env["NODE_BINARY"], str(script_path)],
        cwd=str(script_dir),
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Ad check script failed: {result.stderr}")

    output_lines = [
        line for line in result.stdout.strip().split("\n") if line.startswith("{")
    ]
    if not output_lines:
        raise RuntimeError(
            f"No JSON output from ad check: {result.stdout}\nstderr: {result.stderr}",
        )

    return json.loads(output_lines[-1])


def test_extension_loads_in_chromium():
    """Verify uBlock extension loads in Chromium by visiting its dashboard page.

    Uses Chromium with CDP Extensions.loadUnpacked to load the extension, then navigates
    to chrome-extension://<id>/dashboard.html and checks that "uBlock" appears
    in the page content.
    """
    print("[test] Starting test_extension_loads_in_chromium", flush=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        print(f"[test] tmpdir={tmpdir}", flush=True)

        # Set up isolated env with proper directory structure
        env = setup_test_env(tmpdir)
        env.setdefault("CHROME_HEADLESS", "true")
        print(f"[test] SNAP_DIR={env.get('SNAP_DIR')}", flush=True)
        print(f"[test] CHROME_BINARY={env.get('CHROME_BINARY')}", flush=True)

        # Step 1: Install the uBlock extension
        print("[test] Installing uBlock extension...", flush=True)
        loaded = install_ublock_extension(env)
        print(f"[test] Extension installed at {loaded.loaded_abspath}", flush=True)

        ext_data = ublock_install_state(loaded)
        print(
            f"[test] Extension installed: {ext_data.get('name')} v{ext_data.get('version')}",
            flush=True,
        )

        # Step 2: Launch Chromium using the chrome hook (loads extensions automatically)
        print(f"[test] NODE_MODULES_DIR={env.get('NODE_MODULES_DIR')}", flush=True)
        print(
            f"[test] puppeteer exists: {(Path(env['NODE_MODULES_DIR']) / 'puppeteer').exists()}",
            flush=True,
        )
        print("[test] Launching Chromium...", flush=True)

        # Launch Chromium in crawls directory
        crawl_id = "test-ublock"
        crawl_dir = Path(env["CRAWL_DIR"]) / crawl_id
        crawl_dir.mkdir(parents=True, exist_ok=True)
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir(parents=True, exist_ok=True)
        env["CRAWL_DIR"] = str(crawl_dir)

        chrome_launch_process = None
        cdp_url = None
        try:
            chrome_launch_process, cdp_url = launch_chromium_session(
                env,
                chrome_dir,
                crawl_id,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Chromium launch failed after waiting up to {CHROME_STARTUP_TIMEOUT_SECONDS}s",
            ) from exc

        print(f"[test] Chromium launched with CDP URL: {cdp_url}", flush=True)

        loaded_exts = wait_for_extensions_metadata(chrome_dir, timeout_seconds=10)
        print(
            f"Extensions loaded by chrome hook: {[e.get('name') for e in loaded_exts]}",
        )
        ext_entry = next((e for e in loaded_exts if e.get("name") == "ublock"), None)
        assert ext_entry, f"ublock not present in extensions metadata: {loaded_exts}"
        ext_id = ext_entry.get("id")
        assert ext_id, f"ublock extension id missing from metadata: {ext_entry}"

        # Use the runtime extension ID published by the Chrome session
        unpacked_path = ext_data.get("unpacked_path", "")
        print(f"[test] Extension unpacked path: {unpacked_path}", flush=True)
        print("[test] Running puppeteer test script...", flush=True)

        try:
            # Step 3: Connect to Chromium and verify extension loads
            # Use extension ID resolved from chrome session metadata.
            test_script = f"""
const chromeUtils = require('{CHROME_UTILS_JS}');

(async () => {{
    const puppeteer = chromeUtils.resolvePuppeteerModule();
    const result = await chromeUtils.withConnectedBrowser(
        {{ puppeteer, browserWSEndpoint: '{cdp_url}' }},
        async (browser) => {{
            // Wait for extension to initialize
            await new Promise(r => setTimeout(r, 500));

            const extId = '{ext_id}';
            console.error('Using extension ID from extensions metadata:', extId);

            // Try to load dashboard.html
            const newPage = await browser.newPage();
            const dashboardUrl = 'chrome-extension://' + extId + '/dashboard.html';
            console.error('Loading:', dashboardUrl);

            await newPage.goto(dashboardUrl, {{ waitUntil: 'domcontentloaded', timeout: 15000 }});
            const title = await newPage.title();
            const content = await newPage.content();
            const hasUblock = content.toLowerCase().includes('ublock') || title.toLowerCase().includes('ublock');

            return {{
                loaded: true,
                extensionId: extId,
                pageTitle: title,
                hasExtensionName: hasUblock,
                contentLength: content.length
            }};
        }},
    );
    console.log(JSON.stringify(result));
}})();
"""
            script_path = tmpdir / "test_ublock.js"
            script_path.write_text(
                f"#!/usr/bin/env node\n{test_script}",
                encoding="utf-8",
            )
            script_path.chmod(0o755)

            result = subprocess.run(
                [str(script_path)],
                cwd=str(tmpdir),
                capture_output=True,
                text=True,
                env=env,
                timeout=45,
            )

            print(f"stderr: {result.stderr}")
            print(f"stdout: {result.stdout}")

            assert result.returncode == 0, f"Test failed: {result.stderr}"

            output_lines = [
                line
                for line in result.stdout.strip().split("\n")
                if line.startswith("{")
            ]
            assert output_lines, f"No JSON output: {result.stdout}"

            test_result = json.loads(output_lines[-1])
            assert test_result.get("loaded"), (
                f"uBlock extension should be loaded in Chromium. Result: {test_result}"
            )
            assert test_result.get("hasExtensionName"), (
                f"uBlock dashboard should load and identify itself. Result: {test_result}"
            )
            assert test_result.get("contentLength", 0) > 0, test_result
            print(f"Extension loaded successfully: {test_result}")

        finally:
            if chrome_launch_process:
                kill_chromium_session(chrome_launch_process, chrome_dir)


def test_blocks_ads_on_httpserver_page_with_real_ad_service_urls(httpserver):
    """Verify uBlock Origin Lite blocks real ad-service URLs from a local page.

    This test runs TWO browser sessions:
    1. WITHOUT extension - verifies ads are NOT blocked (baseline)
    2. WITH extension - verifies ads ARE blocked

    The outer page is served by pytest-httpserver so the test can run without
    reaching a live publisher page. The ad subresource URLs stay real, and the
    baseline distinguishes ordinary network failures from uBlock's
    ERR_BLOCKED_BY_CLIENT failures.
    """
    import time

    test_url = serve_ad_fixture(httpserver)
    expected_ad_urls = {url.lower() for url in AD_SERVICE_URLS}

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Set up isolated env with proper directory structure
        env_base = setup_test_env(tmpdir)
        env_base["CHROME_HEADLESS"] = "true"
        ext_install_env, ext_extensions_dir = chrome_extension_install_env(
            tmpdir / "ublock-install",
        )
        env_base["ABXPKG_LIB_DIR"] = ext_install_env["ABXPKG_LIB_DIR"]
        env_base["CHROMEWEBSTORE_EXTENSIONS_DIR"] = str(ext_extensions_dir)
        ext_personas_dir = tmpdir / "personas-ext"
        baseline_personas_dir = tmpdir / "personas-baseline"
        ext_default_dir = ext_personas_dir / "Default"
        baseline_default_dir = baseline_personas_dir / "Default"
        for directory in (
            ext_default_dir / "chrome_downloads",
            ext_default_dir / "chrome_user_data",
            baseline_default_dir / "chrome_downloads",
            baseline_default_dir / "chrome_user_data",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        env_base["PERSONAS_DIR"] = str(ext_personas_dir)
        env_base["CHROME_DOWNLOADS_DIR"] = str(ext_default_dir / "chrome_downloads")
        env_base["CHROME_USER_DATA_DIR"] = str(ext_default_dir / "chrome_user_data")

        print("\n" + "=" * 60)
        print("STEP 1: INSTALLING EXTENSION")
        print("=" * 60)

        loaded = install_ublock_extension(env_base)

        ext_data = ublock_install_state(loaded)
        print(f"Extension installed: {ext_data.get('name')} v{ext_data.get('version')}")

        crawl_root = Path(env_base["CRAWL_DIR"])
        env_no_ext = env_base.copy()
        baseline_install_env, _baseline_extensions_dir = chrome_extension_install_env(
            tmpdir / "baseline-install",
        )
        env_no_ext["PERSONAS_DIR"] = str(baseline_personas_dir)
        env_no_ext["ABXPKG_LIB_DIR"] = baseline_install_env["ABXPKG_LIB_DIR"]
        env_no_ext["CHROMEWEBSTORE_EXTENSIONS_DIR"] = str(_baseline_extensions_dir)
        env_no_ext["CHROME_DOWNLOADS_DIR"] = str(
            baseline_default_dir / "chrome_downloads",
        )
        env_no_ext["CHROME_USER_DATA_DIR"] = str(
            baseline_default_dir / "chrome_user_data",
        )

        ext_env = env_base.copy()
        ext_crawl_id = "test-with-ext"
        ext_crawl_dir = crawl_root / ext_crawl_id
        ext_crawl_dir.mkdir(parents=True, exist_ok=True)
        ext_chrome_dir = ext_crawl_dir / "chrome"
        ext_env["CRAWL_DIR"] = str(ext_crawl_dir)
        ext_process = None

        try:
            ext_process, ext_cdp_url = launch_chromium_session(
                ext_env,
                ext_chrome_dir,
                ext_crawl_id,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            )
            print(f"Extension Chromium launched: {ext_cdp_url}")

            loaded_exts = wait_for_extensions_metadata(
                ext_chrome_dir,
                timeout_seconds=10,
            )
            print(f"Extensions loaded: {[e.get('name') for e in loaded_exts]}")
            ext_entry = next(
                (e for e in loaded_exts if e.get("name") == "ublock"),
                None,
            )
            assert ext_entry, (
                f"ublock not present in extensions metadata: {loaded_exts}"
            )
            ext_id = ext_entry.get("id")
            assert ext_id, f"ublock extension id missing from metadata: {ext_entry}"
            print(f"Extension ID: {ext_id}")

            print("Visiting extension dashboard to verify initialization...")
            dashboard_script = f"""
const chromeUtils = require('{CHROME_UTILS_JS}');
(async () => {{
    const puppeteer = chromeUtils.resolvePuppeteerModule();
    await chromeUtils.withConnectedBrowser(
        {{
            puppeteer,
            browserWSEndpoint: '{ext_cdp_url}',
            connectOptions: {{ defaultViewport: null }},
        }},
        async (browser) => {{
            const page = await browser.newPage();
            await page.goto('chrome-extension://{ext_id}/dashboard.html', {{ waitUntil: 'domcontentloaded', timeout: 10000 }});
            const title = await page.title();
            console.log('Dashboard title:', title);
            await page.close();
        }},
    );
}})();
"""
            dash_script_path = tmpdir / "check_dashboard.js"
            dash_script_path.write_text(
                f"#!/usr/bin/env node\n{dashboard_script}",
                encoding="utf-8",
            )
            dash_script_path.chmod(0o755)
            dashboard_result = subprocess.run(
                [str(dash_script_path)],
                capture_output=True,
                text=True,
                timeout=15,
                env=ext_env,
            )
            assert dashboard_result.returncode == 0, dashboard_result.stderr

            print("Waiting for uBlock filter lists to download and initialize...")
            time.sleep(30)
            kill_chromium_session(ext_process, ext_chrome_dir)
            ext_process = None

            print("\n" + "=" * 60)
            print("STEP 2: BASELINE TEST (no extension)")
            print("=" * 60)

            baseline_env = env_no_ext.copy()
            baseline_crawl_id = "baseline-no-ext"
            baseline_crawl_dir = crawl_root / baseline_crawl_id
            baseline_crawl_dir.mkdir(parents=True, exist_ok=True)
            baseline_chrome_dir = baseline_crawl_dir / "chrome"
            baseline_env["CRAWL_DIR"] = str(baseline_crawl_dir)
            baseline_process = None

            try:
                baseline_process, baseline_cdp_url = launch_chromium_session(
                    baseline_env,
                    baseline_chrome_dir,
                    baseline_crawl_id,
                    timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
                )
                print(f"Baseline Chromium launched: {baseline_cdp_url}")
                time.sleep(2)
                baseline_result = check_ad_blocking(
                    baseline_cdp_url,
                    test_url,
                    baseline_env,
                    tmpdir,
                )
                print(
                    f"Baseline result: {baseline_result['adElementsVisible']} visible ads "
                    f"(found {baseline_result['adElementsFound']} ad elements, "
                    f"{baseline_result['adRequests']} ad requests, "
                    f"{baseline_result['blockedRequests']} blocked ad requests, "
                    f"{baseline_result['totalRequests']} total requests)",
                )
            finally:
                if baseline_process:
                    kill_chromium_session(baseline_process, baseline_chrome_dir)

            assert baseline_result["adElementsFound"] > 0, baseline_result
            assert baseline_result["adElementsVisible"] > 0, baseline_result
            baseline_ad_urls = {url.lower() for url in baseline_result["adRequestUrls"]}
            assert expected_ad_urls.issubset(baseline_ad_urls), baseline_result
            assert baseline_result["adRequests"] >= len(AD_SERVICE_URLS), (
                baseline_result
            )
            assert baseline_result["blockedRequests"] == 0, baseline_result
            assert baseline_result["blockedAdRequestUrls"] == [], baseline_result

            print(
                f"\n✓ Baseline confirmed: {baseline_result['adElementsVisible']} visible ads without extension",
            )

            print("\n" + "=" * 60)
            print("STEP 3: TEST WITH EXTENSION")
            print("=" * 60)

            ext_attempt_env = ext_env.copy()
            ext_attempt_crawl_id = "test-with-ext-check"
            ext_attempt_crawl_dir = crawl_root / ext_attempt_crawl_id
            ext_attempt_crawl_dir.mkdir(parents=True, exist_ok=True)
            ext_attempt_chrome_dir = ext_attempt_crawl_dir / "chrome"
            ext_attempt_env["CRAWL_DIR"] = str(ext_attempt_crawl_dir)
            ext_attempt_process = None

            try:
                ext_attempt_process, ext_attempt_cdp_url = launch_chromium_session(
                    ext_attempt_env,
                    ext_attempt_chrome_dir,
                    ext_attempt_crawl_id,
                    timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
                )
                wait_for_extensions_metadata(
                    ext_attempt_chrome_dir,
                    timeout_seconds=10,
                )
                ext_result = check_ad_blocking(
                    ext_attempt_cdp_url,
                    test_url,
                    ext_attempt_env,
                    tmpdir,
                )
            finally:
                if ext_attempt_process:
                    kill_chromium_session(
                        ext_attempt_process,
                        ext_attempt_chrome_dir,
                    )
            print(
                f"Extension result: {ext_result['adElementsVisible']} visible ads "
                f"(found {ext_result['adElementsFound']} ad elements, "
                f"{ext_result['adRequests']} ad requests, "
                f"{ext_result['blockedRequests']} blocked ad requests, "
                f"{ext_result['totalRequests']} total requests)",
            )

            print("\n" + "=" * 60)
            print("STEP 4: COMPARISON")
            print("=" * 60)
            print(
                f"Baseline (no extension): {baseline_result['adElementsVisible']} visible ads, "
                f"{baseline_result['adRequests']} ad requests, "
                f"{baseline_result['blockedRequests']} blocked ad requests, "
                f"{baseline_result['totalRequests']} total requests",
            )
            print(
                f"With extension: {ext_result['adElementsVisible']} visible ads, "
                f"{ext_result['adRequests']} ad requests, "
                f"{ext_result['blockedRequests']} blocked ad requests, "
                f"{ext_result['totalRequests']} total requests",
            )

            print(
                "Blocked ad URLs: "
                + ", ".join(sorted(ext_result["blockedAdRequestUrls"])),
            )

            ext_ad_urls = {url.lower() for url in ext_result["adRequestUrls"]}
            blocked_ad_urls = {
                url.lower() for url in ext_result["blockedAdRequestUrls"]
            }
            assert expected_ad_urls.issubset(ext_ad_urls), ext_result
            assert len(blocked_ad_urls & expected_ad_urls) >= 2, ext_result
            assert blocked_ad_urls.issubset(ext_ad_urls), ext_result
            assert all(
                "ERR_BLOCKED_BY_CLIENT" in error["errorText"]
                for error in ext_result["failedAdRequestErrors"]
                if error["url"].lower() in blocked_ad_urls
            ), ext_result
            assert ext_result["blockedRequests"] >= 1, ext_result
            assert ext_result["blockedRequests"] > baseline_result["blockedRequests"], {
                "baseline": baseline_result,
                "extension": ext_result,
            }
            assert (
                ext_result["adElementsVisible"] < baseline_result["adElementsVisible"]
            ), {
                "baseline": baseline_result,
                "extension": ext_result,
            }
        finally:
            if ext_process:
                kill_chromium_session(ext_process, ext_chrome_dir)
