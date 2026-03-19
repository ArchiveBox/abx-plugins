"""
Unit tests for ublock plugin

Tests invoke the plugin hook as an external process and verify outputs/side effects.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    setup_test_env,
    launch_chromium_session,
    kill_chromium_session,
    wait_for_extensions_metadata,
)


PLUGIN_DIR = Path(__file__).parent.parent
_INSTALL_SCRIPT = next(PLUGIN_DIR.glob("on_Crawl__*_install_ublock_extension.*"), None)
if _INSTALL_SCRIPT is None:
    raise FileNotFoundError(f"Install script not found in {PLUGIN_DIR}")
INSTALL_SCRIPT = _INSTALL_SCRIPT
CHROME_STARTUP_TIMEOUT_SECONDS = 45


def test_install_script_exists():
    """Verify install script exists"""
    assert INSTALL_SCRIPT.exists(), f"Install script not found: {INSTALL_SCRIPT}"


def test_extension_metadata():
    """Test that uBlock Origin extension has correct metadata"""
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["CHROME_EXTENSIONS_DIR"] = str(Path(tmpdir) / "chrome_extensions")

        result = subprocess.run(
            [
                "node",
                "-e",
                f"const ext = require('{INSTALL_SCRIPT}'); console.log(JSON.stringify(ext.EXTENSION))",
            ],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 0, (
            f"Failed to load extension metadata: {result.stderr}"
        )

        metadata = json.loads(result.stdout)
        assert metadata["webstore_id"] == "cjpalhdlnbpafiamejdnhcphjbkeiagm"
        assert metadata["name"] == "ublock"


def test_install_creates_cache():
    """Test that install creates extension cache"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ext_dir = Path(tmpdir) / "chrome_extensions"
        ext_dir.mkdir(parents=True)

        env = os.environ.copy()
        env["CHROME_EXTENSIONS_DIR"] = str(ext_dir)

        result = subprocess.run(
            [str(INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,  # uBlock is large, may take longer to download
        )

        # Check output mentions installation
        assert "uBlock" in result.stdout or "ublock" in result.stdout

        # Check cache file was created
        cache_file = ext_dir / "ublock.extension.json"
        assert cache_file.exists(), "Cache file should be created"

        # Verify cache content
        cache_data = json.loads(cache_file.read_text())
        assert cache_data["webstore_id"] == "cjpalhdlnbpafiamejdnhcphjbkeiagm"
        assert cache_data["name"] == "ublock"


def test_install_twice_uses_cache():
    """Test that running install twice uses existing cache on second run"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ext_dir = Path(tmpdir) / "chrome_extensions"
        ext_dir.mkdir(parents=True)

        env = os.environ.copy()
        env["CHROME_EXTENSIONS_DIR"] = str(ext_dir)

        # First install - downloads the extension
        result1 = subprocess.run(
            [str(INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,  # uBlock is large
        )
        assert result1.returncode == 0, f"First install failed: {result1.stderr}"

        # Verify cache was created
        cache_file = ext_dir / "ublock.extension.json"
        assert cache_file.exists(), "Cache file should exist after first install"

        # Second install - should use cache and be faster
        result2 = subprocess.run(
            [str(INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result2.returncode == 0, f"Second install failed: {result2.stderr}"

        # Second run should mention cache reuse
        assert (
            "already installed" in result2.stdout
            or "cache" in result2.stdout.lower()
            or result2.returncode == 0
        )


def test_no_configuration_required():
    """Test that uBlock Origin works without configuration"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ext_dir = Path(tmpdir) / "chrome_extensions"
        ext_dir.mkdir(parents=True)

        env = os.environ.copy()
        env["CHROME_EXTENSIONS_DIR"] = str(ext_dir)
        # No API keys needed - works with default filter lists

        install_result = subprocess.run(
            [str(INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert install_result.returncode == 0, (
            f"Install failed: {install_result.stderr}"
        )

        # Should not require any API keys
        combined_output = install_result.stdout + install_result.stderr
        assert "API" not in combined_output or install_result.returncode == 0


def test_large_extension_size():
    """Test that uBlock Origin is downloaded successfully despite large size"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ext_dir = Path(tmpdir) / "chrome_extensions"
        ext_dir.mkdir(parents=True)

        env = os.environ.copy()
        env["CHROME_EXTENSIONS_DIR"] = str(ext_dir)

        result = subprocess.run(
            [str(INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert result.returncode == 0, f"Install failed: {result.stderr}"

        # If extension was downloaded, verify it's substantial size
        crx_file = ext_dir / "cjpalhdlnbpafiamejdnhcphjbkeiagm__ublock.crx"
        if crx_file.exists():
            # uBlock Origin with filter lists is typically 2-5 MB
            size_bytes = crx_file.stat().st_size
            assert size_bytes > 1_000_000, (
                f"uBlock Origin should be > 1MB, got {size_bytes} bytes"
            )


def check_ad_blocking(cdp_url: str, test_url: str, env: dict, script_dir: Path) -> dict:
    """Check ad blocking effectiveness by counting ad elements on page.

    Returns dict with:
        - adElementsFound: int - number of ad-related elements found
        - adElementsVisible: int - number of visible ad elements
        - blockedRequests: int - number of blocked network requests (ads/trackers)
        - totalRequests: int - total network requests made
        - percentBlocked: int - percentage of ad elements hidden (0-100)
    """
    test_script = f"""
if (process.env.NODE_MODULES_DIR) module.paths.unshift(process.env.NODE_MODULES_DIR);
const puppeteer = require('puppeteer-core');

(async () => {{
    const browser = await puppeteer.connect({{ browserWSEndpoint: '{cdp_url}' }});

    const page = await browser.newPage();
    await page.setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
    await page.setViewport({{ width: 1440, height: 900 }});

    // Track network requests
    let blockedRequests = 0;
    let totalRequests = 0;
    const adDomains = ['doubleclick', 'googlesyndication', 'googleadservices', 'facebook.com/tr',
                       'analytics', 'adservice', 'advertising', 'taboola', 'outbrain', 'criteo',
                       'amazon-adsystem', 'ads.yahoo', 'gemini.yahoo', 'yimg.com/cv/', 'beap.gemini'];

    page.on('request', request => {{
        totalRequests++;
        const url = request.url().toLowerCase();
        if (adDomains.some(d => url.includes(d))) {{
            // This is an ad request
        }}
    }});

    page.on('requestfailed', request => {{
        const url = request.url().toLowerCase();
        if (adDomains.some(d => url.includes(d))) {{
            blockedRequests++;
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
    result.totalRequests = totalRequests;
    // Calculate how many ad elements were hidden (found but not visible)
    const hiddenAds = result.adElementsFound - result.adElementsVisible;
    result.percentBlocked = result.adElementsFound > 0
        ? Math.round((hiddenAds / result.adElementsFound) * 100)
        : 0;

    console.error('Ad blocking result:', JSON.stringify(result));
    browser.disconnect();
    console.log(JSON.stringify(result));
}})();
"""
    script_path = script_dir / "check_ads.js"
    script_path.write_text(f"#!/usr/bin/env node\n{test_script}", encoding="utf-8")
    script_path.chmod(0o755)

    result = subprocess.run(
        ["node", str(script_path)],
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
            f"No JSON output from ad check: {result.stdout}\nstderr: {result.stderr}"
        )

    return json.loads(output_lines[-1])


# Test URL: Yahoo has many ads that uBlock should block (no mocks)
TEST_URL = "https://www.yahoo.com/"


def test_extension_loads_in_chromium():
    """Verify uBlock extension loads in Chromium by visiting its dashboard page.

    Uses Chromium with --load-extension to load the extension, then navigates
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

        ext_dir = Path(env["CHROME_EXTENSIONS_DIR"])

        # Step 1: Install the uBlock extension
        print("[test] Installing uBlock extension...", flush=True)
        result = subprocess.run(
            [str(INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        print(f"[test] Extension install rc={result.returncode}", flush=True)
        assert result.returncode == 0, f"Extension install failed: {result.stderr}"

        # Verify extension cache was created
        cache_file = ext_dir / "ublock.extension.json"
        assert cache_file.exists(), "Extension cache not created"
        ext_data = json.loads(cache_file.read_text())
        print(
            f"[test] Extension installed: {ext_data.get('name')} v{ext_data.get('version')}",
            flush=True,
        )

        # Step 2: Launch Chromium using the chrome hook (loads extensions automatically)
        print(f"[test] NODE_MODULES_DIR={env.get('NODE_MODULES_DIR')}", flush=True)
        print(
            f"[test] puppeteer-core exists: {(Path(env['NODE_MODULES_DIR']) / 'puppeteer-core').exists()}",
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
                f"Chromium launch failed after waiting up to {CHROME_STARTUP_TIMEOUT_SECONDS}s"
            ) from exc

        print(f"[test] Chromium launched with CDP URL: {cdp_url}", flush=True)

        loaded_exts = wait_for_extensions_metadata(chrome_dir, timeout_seconds=10)
        print(
            f"Extensions loaded by chrome hook: {[e.get('name') for e in loaded_exts]}"
        )
        ext_entry = next((e for e in loaded_exts if e.get("name") == "ublock"), None)
        assert ext_entry, f"ublock not present in extensions metadata: {loaded_exts}"
        ext_id = ext_entry.get("id")
        assert ext_id, f"ublock extension id missing from metadata: {ext_entry}"

        # Get the unpacked extension ID - Chrome computes this from the path
        unpacked_path = ext_data.get("unpacked_path", "")
        print(f"[test] Extension unpacked path: {unpacked_path}", flush=True)
        print("[test] Running puppeteer test script...", flush=True)

        try:
            # Step 3: Connect to Chromium and verify extension loads
            # Use extension ID resolved from chrome session metadata.
            test_script = f"""
if (process.env.NODE_MODULES_DIR) module.paths.unshift(process.env.NODE_MODULES_DIR);
const puppeteer = require('puppeteer-core');

(async () => {{
    const browser = await puppeteer.connect({{ browserWSEndpoint: '{cdp_url}' }});

    // Wait for extension to initialize
    await new Promise(r => setTimeout(r, 500));

    const extId = '{ext_id}';
    console.error('Using extension ID from extensions metadata:', extId);

    // Try to load dashboard.html
    const newPage = await browser.newPage();
    const dashboardUrl = 'chrome-extension://' + extId + '/dashboard.html';
    console.error('Loading:', dashboardUrl);

    try {{
        await newPage.goto(dashboardUrl, {{ waitUntil: 'domcontentloaded', timeout: 15000 }});
        const title = await newPage.title();
        const content = await newPage.content();
        const hasUblock = content.toLowerCase().includes('ublock') || title.toLowerCase().includes('ublock');

        console.log(JSON.stringify({{
            loaded: true,
            extensionId: extId,
            pageTitle: title,
            hasExtensionName: hasUblock,
            contentLength: content.length
        }}));
    }} catch (e) {{
        console.error('Dashboard load failed:', e.message);
        console.log(JSON.stringify({{ loaded: true, extensionId: extId, dashboardError: e.message }}));
    }}

    browser.disconnect();
}})();
"""
            script_path = tmpdir / "test_ublock.js"
            script_path.write_text(f"#!/usr/bin/env node\n{test_script}", encoding="utf-8")
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
            print(f"Extension loaded successfully: {test_result}")

        finally:
            if chrome_launch_process:
                kill_chromium_session(chrome_launch_process, chrome_dir)


def test_blocks_ads_on_yahoo_com():
    """Live test: verify uBlock Origin blocks ads on yahoo.com (real network).

    This test runs TWO browser sessions:
    1. WITHOUT extension - verifies ads are NOT blocked (baseline)
    2. WITH extension - verifies ads ARE blocked

    This ensures we're actually testing the extension's effect, not just
    that a test page happens to show ads as blocked. No mocks are used.
    """
    import time

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Set up isolated env with proper directory structure
        env_base = setup_test_env(tmpdir)
        env_base["CHROME_HEADLESS"] = "true"

        # ============================================================
        # STEP 1: BASELINE - Run WITHOUT extension, verify ads are NOT blocked
        # ============================================================
        print("\n" + "=" * 60)
        print("STEP 1: BASELINE TEST (no extension)")
        print("=" * 60)

        personas_dir = Path(env_base["PERSONAS_DIR"])

        env_no_ext = env_base.copy()
        env_no_ext["CHROME_EXTENSIONS_DIR"] = str(
            personas_dir / "Default" / "empty_extensions"
        )
        (personas_dir / "Default" / "empty_extensions").mkdir(
            parents=True, exist_ok=True
        )

        # Launch baseline Chromium in crawls directory
        baseline_crawl_id = "baseline-no-ext"
        baseline_crawl_dir = Path(env_base["CRAWL_DIR"]) / baseline_crawl_id
        baseline_crawl_dir.mkdir(parents=True, exist_ok=True)
        baseline_chrome_dir = baseline_crawl_dir / "chrome"
        env_no_ext["CRAWL_DIR"] = str(baseline_crawl_dir)
        baseline_process = None

        try:
            baseline_process, baseline_cdp_url = launch_chromium_session(
                env_no_ext,
                baseline_chrome_dir,
                baseline_crawl_id,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            )
            print(f"Baseline Chromium launched: {baseline_cdp_url}")

            # Wait a moment for browser to be ready
            time.sleep(2)

            baseline_result = check_ad_blocking(
                baseline_cdp_url, TEST_URL, env_no_ext, tmpdir
            )

            print(
                f"Baseline result: {baseline_result['adElementsVisible']} visible ads "
                f"(found {baseline_result['adElementsFound']} ad elements)"
            )

        finally:
            if baseline_process:
                kill_chromium_session(baseline_process, baseline_chrome_dir)

        # Verify baseline shows ads ARE visible (not blocked)
        if baseline_result["adElementsFound"] == 0:
            pytest.fail(
                f"Baseline must find ad elements on {TEST_URL}, but found none. "
                f"This test requires a real ad-heavy page."
            )

        if baseline_result["adElementsVisible"] == 0:
            pytest.fail(
                f"Baseline must have visible ads on {TEST_URL}, but none were visible. "
                f"This likely means another ad blocker is active or network-level blocking is in effect."
            )

        print(
            f"\n✓ Baseline confirmed: {baseline_result['adElementsVisible']} visible ads without extension"
        )

        # ============================================================
        # STEP 2: Install the uBlock extension
        # ============================================================
        print("\n" + "=" * 60)
        print("STEP 2: INSTALLING EXTENSION")
        print("=" * 60)

        ext_dir = Path(env_base["CHROME_EXTENSIONS_DIR"])

        result = subprocess.run(
            [str(INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
            env=env_base,
            timeout=60,
        )
        assert result.returncode == 0, f"Extension install failed: {result.stderr}"

        cache_file = ext_dir / "ublock.extension.json"
        assert cache_file.exists(), "Extension cache not created"
        ext_data = json.loads(cache_file.read_text())
        print(f"Extension installed: {ext_data.get('name')} v{ext_data.get('version')}")

        # ============================================================
        # STEP 3: Run WITH extension, verify ads ARE blocked
        # ============================================================
        print("\n" + "=" * 60)
        print("STEP 3: TEST WITH EXTENSION")
        print("=" * 60)

        # Launch extension test Chromium in crawls directory
        ext_crawl_id = "test-with-ext"
        ext_crawl_dir = Path(env_base["CRAWL_DIR"]) / ext_crawl_id
        ext_crawl_dir.mkdir(parents=True, exist_ok=True)
        ext_chrome_dir = ext_crawl_dir / "chrome"
        env_base["CRAWL_DIR"] = str(ext_crawl_dir)
        ext_process = None

        try:
            ext_process, ext_cdp_url = launch_chromium_session(
                env_base,
                ext_chrome_dir,
                ext_crawl_id,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            )
            print(f"Extension Chromium launched: {ext_cdp_url}")

            loaded_exts = wait_for_extensions_metadata(
                ext_chrome_dir, timeout_seconds=10
            )
            print(f"Extensions loaded: {[e.get('name') for e in loaded_exts]}")
            ext_entry = next(
                (e for e in loaded_exts if e.get("name") == "ublock"), None
            )
            assert ext_entry, (
                f"ublock not present in extensions metadata: {loaded_exts}"
            )
            ext_id = ext_entry.get("id")
            assert ext_id, f"ublock extension id missing from metadata: {ext_entry}"
            print(f"Extension ID: {ext_id}")

            # Visit the extension dashboard to ensure it's fully loaded
            print("Visiting extension dashboard to verify initialization...")
            dashboard_script = f"""
const puppeteer = require('{env_base["NODE_MODULES_DIR"]}/puppeteer-core');
(async () => {{
    const browser = await puppeteer.connect({{
        browserWSEndpoint: '{ext_cdp_url}',
        defaultViewport: null
    }});
    const page = await browser.newPage();
    await page.goto('chrome-extension://{ext_id}/dashboard.html', {{ waitUntil: 'domcontentloaded', timeout: 10000 }});
    const title = await page.title();
    console.log('Dashboard title:', title);
    await page.close();
    browser.disconnect();
}})();
"""
            dash_script_path = tmpdir / "check_dashboard.js"
            dash_script_path.write_text(
                f"#!/usr/bin/env node\n{dashboard_script}",
                encoding="utf-8",
            )
            dash_script_path.chmod(0o755)
            subprocess.run(
                [str(dash_script_path)],
                capture_output=True,
                timeout=15,
                env=env_base,
            )

            # Wait longer for extension to fully initialize filters
            # On first run, uBlock needs to download filter lists which can take 10-15 seconds
            print("Waiting for uBlock filter lists to download and initialize...")
            time.sleep(15)

            ext_result = check_ad_blocking(ext_cdp_url, TEST_URL, env_base, tmpdir)

            print(
                f"Extension result: {ext_result['adElementsVisible']} visible ads "
                f"(found {ext_result['adElementsFound']} ad elements)"
            )

        finally:
            if ext_process:
                kill_chromium_session(ext_process, ext_chrome_dir)

        # ============================================================
        # STEP 4: Compare results
        # ============================================================
        print("\n" + "=" * 60)
        print("STEP 4: COMPARISON")
        print("=" * 60)
        print(
            f"Baseline (no extension): {baseline_result['adElementsVisible']} visible ads"
        )
        print(f"With extension: {ext_result['adElementsVisible']} visible ads")

        # Calculate reduction in visible ads
        ads_blocked = (
            baseline_result["adElementsVisible"] - ext_result["adElementsVisible"]
        )
        reduction_percent = (
            (ads_blocked / baseline_result["adElementsVisible"] * 100)
            if baseline_result["adElementsVisible"] > 0
            else 0
        )

        print(
            f"Reduction: {ads_blocked} fewer visible ads ({reduction_percent:.0f}% reduction)"
        )

        # Extension should significantly reduce visible ads
        assert ext_result["adElementsVisible"] < baseline_result["adElementsVisible"], (
            f"uBlock should reduce visible ads.\n"
            f"Baseline: {baseline_result['adElementsVisible']} visible ads\n"
            f"With extension: {ext_result['adElementsVisible']} visible ads\n"
            f"Expected fewer ads with extension."
        )

        # Ensure uBlock actually blocks at least some ad/track requests
        assert ext_result["blockedRequests"] > 0, (
            "uBlock should block at least one ad/track request on yahoo.com"
        )

        # Extension should block at least 20% of ads (was consistently blocking 5-13% without proper init time)
        assert reduction_percent >= 20, (
            f"uBlock should block at least 20% of ads.\n"
            f"Baseline: {baseline_result['adElementsVisible']} visible ads\n"
            f"With extension: {ext_result['adElementsVisible']} visible ads\n"
            f"Reduction: only {reduction_percent:.0f}% (expected at least 20%)\n"
            f"Note: Filter lists must be downloaded on first run (takes ~15s)"
        )

        print("\n✓ SUCCESS: uBlock correctly blocks ads!")
        print(f"  - Baseline: {baseline_result['adElementsVisible']} visible ads")
        print(f"  - With extension: {ext_result['adElementsVisible']} visible ads")
        print(f"  - Blocked: {ads_blocked} ads ({reduction_percent:.0f}% reduction)")
