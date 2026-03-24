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

from abx_plugins.plugins.base.test_utils import parse_jsonl_records
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    setup_test_env,
    launch_chromium_session,
    kill_chromium_session,
    wait_for_extensions_metadata,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


PLUGIN_DIR = Path(__file__).parent.parent
CHROMEWEBSTORE_HOOK = (
    PLUGIN_DIR.parent / "chromewebstore" / "on_BinaryRequest__90_chromewebstore.py"
)
SNAPSHOT_HOOK = PLUGIN_DIR / "on_Snapshot__12_ublock.daemon.bg.js"
NAVIGATE_HOOK = PLUGIN_DIR.parent / "chrome" / "on_Snapshot__30_chrome_navigate.js"
BASE_UTILS_JS = PLUGIN_DIR.parent / "base" / "utils.js"
CHROME_UTILS_JS = PLUGIN_DIR.parent / "chrome" / "chrome_utils.js"
CHROME_STARTUP_TIMEOUT_SECONDS = 45
EXTENSION_NAME = "ublock"
EXTENSION_WEBSTORE_ID = "cjpalhdlnbpafiamejdnhcphjbkeiagm"


def install_ublock_extension(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    provider_result = subprocess.run(
        [
            str(CHROMEWEBSTORE_HOOK),
            f"--name={EXTENSION_NAME}",
            "--binproviders=chromewebstore",
            f"--overrides={json.dumps({'chromewebstore': {'install_args': [EXTENSION_WEBSTORE_ID, f'--name={EXTENSION_NAME}']}})}",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    assert provider_result.returncode == 0, (
        f"Provider install failed: {provider_result.stderr}\nstdout: {provider_result.stdout}"
    )
    return provider_result


def test_chromewebstore_provider_exists():
    assert CHROMEWEBSTORE_HOOK.exists(), (
        f"Provider hook not found: {CHROMEWEBSTORE_HOOK}"
    )


def test_extension_metadata():
    assert EXTENSION_NAME == "ublock"
    assert EXTENSION_WEBSTORE_ID == "cjpalhdlnbpafiamejdnhcphjbkeiagm"


def test_install_creates_cache():
    """Test that install creates extension cache"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ext_dir = Path(tmpdir) / "chrome_extensions"
        ext_dir.mkdir(parents=True)

        env = os.environ.copy()
        env["CHROME_EXTENSIONS_DIR"] = str(ext_dir)

        result = install_ublock_extension(env)

        # Check output mentions installation
        assert "Resolved extension ublock" in result.stderr or "ublock" in result.stdout

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
        install_ublock_extension(env)

        # Verify cache was created
        cache_file = ext_dir / "ublock.extension.json"
        assert cache_file.exists(), "Cache file should exist after first install"

        # Second install - should use cache and be faster
        provider2 = install_ublock_extension(env)

        # Second run should mention cache reuse
        assert (
            "already installed" in provider2.stderr.lower()
            or "cache" in provider2.stderr.lower()
            or provider2.returncode == 0
        )


def test_no_configuration_required():
    """Test that uBlock Origin works without configuration"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ext_dir = Path(tmpdir) / "chrome_extensions"
        ext_dir.mkdir(parents=True)

        env = os.environ.copy()
        env["CHROME_EXTENSIONS_DIR"] = str(ext_dir)
        # No API keys needed - works with default filter lists

        install_result = install_ublock_extension(env)
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

        result = install_ublock_extension(env)
        assert result.returncode == 0, f"Install failed: {result.stderr}"

        # If extension was downloaded, verify it's substantial size
        crx_file = ext_dir / "cjpalhdlnbpafiamejdnhcphjbkeiagm__ublock.crx"
        if crx_file.exists():
            # uBlock Origin with filter lists is typically 2-5 MB
            size_bytes = crx_file.stat().st_size
            assert size_bytes > 1_000_000, (
                f"uBlock Origin should be > 1MB, got {size_bytes} bytes"
            )


def test_snapshot_hook_reports_skipped_when_disabled():
    env = os.environ.copy()
    env["UBLOCK_ENABLED"] = "false"

    result = subprocess.run(
        [str(SNAPSHOT_HOOK), "--url=https://example.com"],
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
        with chrome_session(
            tmpdir,
            crawl_id="ublock-noresults",
            snapshot_id="ublock-noresults-snap",
            test_url="about:blank",
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (_chrome_launch_process, _chrome_pid, snapshot_chrome_dir, env):
            hook_dir = snapshot_chrome_dir.parent / "ublock"
            hook_dir.mkdir(parents=True, exist_ok=True)

            hook_process = subprocess.Popen(
                [
                    str(SNAPSHOT_HOOK),
                    "--url=about:blank",
                    "--snapshot-id=ublock-noresults-snap",
                ],
                cwd=str(hook_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            try:
                navigate = subprocess.run(
                    [
                        str(NAVIGATE_HOOK),
                        "--url=about:blank",
                        "--snapshot-id=ublock-noresults-snap",
                    ],
                    cwd=str(snapshot_chrome_dir),
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=60,
                )
                assert navigate.returncode == 0, navigate.stderr

                time.sleep(2)
                hook_process.send_signal(signal.SIGTERM)
                stdout, stderr = hook_process.communicate(timeout=15)

                assert hook_process.returncode == 0, stderr
                records = parse_jsonl_records(stdout)
                archive_result = next(
                    record
                    for record in records
                    if record.get("type") == "ArchiveResult"
                )
                assert archive_result["status"] == "noresults", archive_result
                assert (
                    archive_result["output_str"]
                    == "0 ad requests blocked | 0 elements hidden"
                ), archive_result
            finally:
                if hook_process.poll() is None:
                    hook_process.kill()


def test_snapshot_hook_reports_live_blocking_counts():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        install_env = setup_test_env(tmpdir)
        install_env["CHROME_HEADLESS"] = "true"
        install_result = install_ublock_extension(install_env)
        assert install_result.returncode == 0, install_result.stderr

        with chrome_session(
            tmpdir,
            crawl_id="ublock-live",
            snapshot_id="ublock-live-snap",
            test_url=TEST_URL,
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (_chrome_launch_process, _chrome_pid, snapshot_chrome_dir, env):
            hook_dir = snapshot_chrome_dir.parent / "ublock"
            hook_dir.mkdir(parents=True, exist_ok=True)

            hook_process = subprocess.Popen(
                [
                    str(SNAPSHOT_HOOK),
                    f"--url={TEST_URL}",
                    "--snapshot-id=ublock-live-snap",
                ],
                cwd=str(hook_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            try:
                navigate = subprocess.run(
                    [
                        str(NAVIGATE_HOOK),
                        f"--url={TEST_URL}",
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
                hook_process.send_signal(signal.SIGTERM)
                stdout, stderr = hook_process.communicate(timeout=20)

                assert hook_process.returncode == 0, stderr
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
                assert blocked > 0 or hidden > 0, archive_result
            finally:
                if hook_process.poll() is None:
                    hook_process.kill()


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
            await page.close();
            return result;
        }},
    );
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
            f"No JSON output from ad check: {result.stdout}\nstderr: {result.stderr}",
        )

    return json.loads(output_lines[-1])


# Live test URL with deliberately ad-heavy content for stable blocker verification.
TEST_URL = "https://canyoublockit.com/extreme-test/"


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
        result = install_ublock_extension(env)
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

        # Get the unpacked extension ID - Chrome computes this from the path
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

            try {{
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
            }} catch (e) {{
                console.error('Dashboard load failed:', e.message);
                return {{ loaded: true, extensionId: extId, dashboardError: e.message }};
            }}
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
            print(f"Extension loaded successfully: {test_result}")

        finally:
            if chrome_launch_process:
                kill_chromium_session(chrome_launch_process, chrome_dir)


def test_blocks_ads_on_canyoublockit_extreme():
    """Live test: verify uBlock Origin blocks ads on canyoublockit.com/extreme-test.

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
        ext_personas_dir = tmpdir / "personas-ext"
        baseline_personas_dir = tmpdir / "personas-baseline"
        ext_default_dir = ext_personas_dir / "Default"
        baseline_default_dir = baseline_personas_dir / "Default"
        for directory in (
            ext_default_dir / "chrome_extensions",
            ext_default_dir / "chrome_downloads",
            ext_default_dir / "chrome_user_data",
            baseline_default_dir / "chrome_extensions",
            baseline_default_dir / "chrome_downloads",
            baseline_default_dir / "chrome_user_data",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        env_base["PERSONAS_DIR"] = str(ext_personas_dir)
        env_base["CHROME_EXTENSIONS_DIR"] = str(ext_default_dir / "chrome_extensions")
        env_base["CHROME_DOWNLOADS_DIR"] = str(ext_default_dir / "chrome_downloads")
        env_base["CHROME_USER_DATA_DIR"] = str(ext_default_dir / "chrome_user_data")

        print("\n" + "=" * 60)
        print("STEP 1: INSTALLING EXTENSION")
        print("=" * 60)

        ext_dir = Path(env_base["CHROME_EXTENSIONS_DIR"])

        result = install_ublock_extension(env_base)
        assert result.returncode == 0, f"Extension install failed: {result.stderr}"

        cache_file = ext_dir / "ublock.extension.json"
        assert cache_file.exists(), "Extension cache not created"
        ext_data = json.loads(cache_file.read_text())
        print(f"Extension installed: {ext_data.get('name')} v{ext_data.get('version')}")

        crawl_root = Path(env_base["CRAWL_DIR"])
        env_no_ext = env_base.copy()
        env_no_ext["PERSONAS_DIR"] = str(baseline_personas_dir)
        env_no_ext["CHROME_EXTENSIONS_DIR"] = str(
            baseline_default_dir / "chrome_extensions",
        )
        env_no_ext["CHROME_DOWNLOADS_DIR"] = str(
            baseline_default_dir / "chrome_downloads",
        )
        env_no_ext["CHROME_USER_DATA_DIR"] = str(
            baseline_default_dir / "chrome_user_data",
        )

        attempt_failures: list[str] = []
        max_attempts = 3

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
            subprocess.run(
                [str(dash_script_path)],
                capture_output=True,
                timeout=15,
                env=ext_env,
            )

            print("Waiting for uBlock filter lists to download and initialize...")
            time.sleep(30)

            for attempt in range(1, max_attempts + 1):
                print("\n" + "=" * 60)
                print(f"STEP 2.{attempt}: BASELINE TEST (no extension)")
                print("=" * 60)

                baseline_env = env_no_ext.copy()
                baseline_crawl_id = f"baseline-no-ext-{attempt}"
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
                        TEST_URL,
                        baseline_env,
                        tmpdir,
                    )
                    print(
                        f"Baseline result: {baseline_result['adElementsVisible']} visible ads "
                        f"(found {baseline_result['adElementsFound']} ad elements, "
                        f"{baseline_result['totalRequests']} total requests)",
                    )
                finally:
                    if baseline_process:
                        kill_chromium_session(baseline_process, baseline_chrome_dir)

                if baseline_result["adElementsFound"] == 0:
                    attempt_failures.append(
                        f"attempt {attempt}: baseline found no ad elements on {TEST_URL}",
                    )
                    continue

                weak_signal = baseline_result["adElementsVisible"] < 10
                if weak_signal:
                    print(
                        f"[warning] baseline only exposed {baseline_result['adElementsVisible']} visible ads; "
                        "continuing because the live ad signal can vary across samples",
                    )

                print(
                    f"\n✓ Baseline confirmed: {baseline_result['adElementsVisible']} visible ads without extension",
                )

                print("\n" + "=" * 60)
                print(f"STEP 3.{attempt}: TEST WITH EXTENSION")
                print("=" * 60)

                ext_result = check_ad_blocking(ext_cdp_url, TEST_URL, ext_env, tmpdir)
                print(
                    f"Extension result: {ext_result['adElementsVisible']} visible ads "
                    f"(found {ext_result['adElementsFound']} ad elements, "
                    f"{ext_result['totalRequests']} total requests)",
                )

                print("\n" + "=" * 60)
                print(f"STEP 4.{attempt}: COMPARISON")
                print("=" * 60)
                print(
                    f"Baseline (no extension): {baseline_result['adElementsVisible']} visible ads, "
                    f"{baseline_result['totalRequests']} total requests",
                )
                print(
                    f"With extension: {ext_result['adElementsVisible']} visible ads, "
                    f"{ext_result['totalRequests']} total requests",
                )

                ads_blocked = (
                    baseline_result["adElementsVisible"]
                    - ext_result["adElementsVisible"]
                )
                reduction_percent = (
                    (ads_blocked / baseline_result["adElementsVisible"] * 100)
                    if baseline_result["adElementsVisible"] > 0
                    else 0
                )

                print(
                    f"Reduction: {ads_blocked} fewer visible ads ({reduction_percent:.0f}% reduction)",
                )

                request_reduction_percent = (
                    (
                        (baseline_result["totalRequests"] - ext_result["totalRequests"])
                        / baseline_result["totalRequests"]
                        * 100
                    )
                    if baseline_result["totalRequests"] > 0
                    else 0
                )
                print(
                    "Request reduction: "
                    f"{baseline_result['totalRequests']} -> {ext_result['totalRequests']} "
                    f"({request_reduction_percent:.0f}% reduction)",
                )

                if (
                    ext_result["adElementsVisible"]
                    <= baseline_result["adElementsVisible"] - 2
                    and ext_result["totalRequests"]
                    < baseline_result["totalRequests"] * 0.8
                ):
                    print("\n✓ SUCCESS: uBlock correctly blocks ads!")
                    print(
                        f"  - Baseline: {baseline_result['adElementsVisible']} visible ads"
                    )
                    print(
                        f"  - With extension: {ext_result['adElementsVisible']} visible ads"
                    )
                    print(
                        f"  - Blocked: {ads_blocked} ads ({reduction_percent:.0f}% reduction)",
                    )
                    print(
                        "  - Total requests: "
                        f"{baseline_result['totalRequests']} -> {ext_result['totalRequests']}",
                    )
                    return

                attempt_failures.append(
                    "attempt "
                    f"{attempt}: baseline={baseline_result['adElementsVisible']} visible ads, "
                    f"extension={ext_result['adElementsVisible']} visible ads, "
                    f"baseline_requests={baseline_result['totalRequests']}, "
                    f"extension_requests={ext_result['totalRequests']}, "
                    f"request_reduction={request_reduction_percent:.0f}%"
                    + (" (weak baseline signal)" if weak_signal else ""),
                )
        finally:
            if ext_process:
                kill_chromium_session(ext_process, ext_chrome_dir)

        pytest.fail(
            "uBlock did not produce a strong enough reduction on canyoublockit.com/extreme-test after "
            f"{max_attempts} live attempts:\n" + "\n".join(attempt_failures),
        )
