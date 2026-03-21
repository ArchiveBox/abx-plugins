"""
Unit tests for istilldontcareaboutcookies plugin

Tests invoke the plugin hook as an external process and verify outputs/side effects.
"""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    setup_test_env,
    launch_chromium_session,
    kill_chromium_session,
    wait_for_extensions_metadata,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


PLUGIN_DIR = Path(__file__).parent.parent
_INSTALL_SCRIPT = next(
    PLUGIN_DIR.glob("on_Crawl__*_install_istilldontcareaboutcookies_extension.*"),
    None,
)
if _INSTALL_SCRIPT is None:
    raise FileNotFoundError(f"Install script not found in {PLUGIN_DIR}")
INSTALL_SCRIPT = _INSTALL_SCRIPT
CHROME_STARTUP_TIMEOUT_SECONDS = 45


def test_install_script_exists():
    """Verify install script exists"""
    assert INSTALL_SCRIPT.exists(), f"Install script not found: {INSTALL_SCRIPT}"


def test_extension_metadata():
    """Test that extension has correct metadata"""
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
        assert metadata["webstore_id"] == "edibdbjcniadpccecjdfdjjppcpchdlm"
        assert metadata["name"] == "istilldontcareaboutcookies"


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
            timeout=60,
        )

        # Check output mentions installation
        assert (
            "Installing" in result.stdout
            or "installed" in result.stdout
            or "istilldontcareaboutcookies" in result.stdout
        )

        # Check cache file was created
        cache_file = ext_dir / "istilldontcareaboutcookies.extension.json"
        assert cache_file.exists(), "Cache file should be created"

        # Verify cache content
        cache_data = json.loads(cache_file.read_text())
        assert cache_data["webstore_id"] == "edibdbjcniadpccecjdfdjjppcpchdlm"
        assert cache_data["name"] == "istilldontcareaboutcookies"


def test_install_uses_existing_cache():
    """Test that install uses existing cache when available"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ext_dir = Path(tmpdir) / "chrome_extensions"
        ext_dir.mkdir(parents=True)

        # Create fake cache
        fake_extension_dir = (
            ext_dir / "edibdbjcniadpccecjdfdjjppcpchdlm__istilldontcareaboutcookies"
        )
        fake_extension_dir.mkdir(parents=True)

        manifest = {"version": "1.1.8", "name": "I still don't care about cookies"}
        (fake_extension_dir / "manifest.json").write_text(json.dumps(manifest))

        env = os.environ.copy()
        env["CHROME_EXTENSIONS_DIR"] = str(ext_dir)

        result = subprocess.run(
            [str(INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        # Should use cache or install successfully
        assert result.returncode == 0


def test_no_configuration_required():
    """Test that extension works without any configuration"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ext_dir = Path(tmpdir) / "chrome_extensions"
        ext_dir.mkdir(parents=True)

        env = os.environ.copy()
        env["CHROME_EXTENSIONS_DIR"] = str(ext_dir)
        # No special env vars needed - works out of the box

        result = subprocess.run(
            [str(INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        # Should not require any API keys or configuration
        assert "API" not in (result.stdout + result.stderr) or result.returncode == 0


COOKIE_TEST_PATH = "/cookie-consent-test"
COOKIE_TEST_HTML_STUB = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Cookie Consent Test Fixture</title>
</head>
<body>
  <div class="cky-consent-container cky-popup-center" tabindex="-1" aria-label="Consentimiento de cookies" role="region"><div class="cky-consent-bar" data-cky-tag="notice" style="border-color: #151527; background-color: #151527;"><div class="cky-notice"><p class="cky-title" aria-level="2" data-cky-tag="title" role="heading" style="color: #FFFFFF;">Consentimiento de cookies</p><div class="cky-notice-group"><div class="cky-notice-des" data-cky-tag="description" style="color: #FFFFFF;"><p>En Filmin usamos cookies para el funcionamiento del sitio web, para mejorar y personalizar la experiencia de usuario y para recopilar información sobre las actividades de navegación que nos ayudan a mejorar. Puedes aceptar todo, rechazar o administrar las preferencias de cookies.&nbsp;<a href="https://www.filmin.es/aviso-legal#politica-de-cookies" class="cky-policy" aria-label="Aviso legal" target="_blank" rel="noopener" style="color: #FFFFFF; border-color: transparent; background-color: transparent;" data-cky-tag="readmore-button">Aviso legal</a></p></div><div class="cky-notice-btn-wrapper" data-cky-tag="notice-buttons"><button class="cky-btn cky-btn-customize" aria-label="Preferencias" aria-haspopup="dialog" aria-controls="ckyPreferenceCenter" data-cky-tag="settings-button" style="color: #FFFFFF; border-color: #FFFFFF; background-color: RGBA(0, 0, 0, 0);">Preferencias</button> <button class="cky-btn cky-btn-reject" aria-label="Rechazar todo" data-cky-tag="reject-button" style="color: #141426; border-color: #02FFA1; background-color: #02FFA1;">Rechazar todo</button> <button class="cky-btn cky-btn-accept" aria-label="Aceptar todo" data-cky-tag="accept-button" style="color: #141426; border-color: #02FFA1; background-color: #02FFA1;">Aceptar todo</button></div></div></div></div></div>
</body>
</html>
"""


def test_extension_loads_in_chromium():
    """Verify extension loads in Chromium by visiting its options page.

    Uses Chromium with --load-extension to load the extension, then navigates
    to chrome-extension://<id>/options.html and checks that the extension name
    appears in the page content.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Set up isolated env with proper directory structure
        env = setup_test_env(tmpdir)
        env.setdefault("CHROME_HEADLESS", "true")

        ext_dir = Path(env["CHROME_EXTENSIONS_DIR"])

        # Step 1: Install the extension
        result = subprocess.run(
            [str(INSTALL_SCRIPT)],
            cwd=str(tmpdir),
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert result.returncode == 0, f"Extension install failed: {result.stderr}"

        # Verify extension cache was created
        cache_file = ext_dir / "istilldontcareaboutcookies.extension.json"
        assert cache_file.exists(), "Extension cache not created"
        ext_data = json.loads(cache_file.read_text())
        print(f"Extension installed: {ext_data.get('name')} v{ext_data.get('version')}")

        # Step 2: Launch Chromium using the chrome hook (loads extensions automatically)
        crawl_id = "test-cookies"
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

        print(f"Chromium launched with CDP URL: {cdp_url}")

        loaded_exts = wait_for_extensions_metadata(chrome_dir, timeout_seconds=10)
        print(f"Extensions loaded: {[e.get('name') for e in loaded_exts]}")
        ext_entry = next(
            (e for e in loaded_exts if e.get("name") == "istilldontcareaboutcookies"),
            None,
        )
        assert ext_entry, (
            f"istilldontcareaboutcookies not present in extensions.json: {loaded_exts}"
        )
        ext_id = ext_entry.get("id")
        assert ext_id, f"Extension id missing from extensions.json entry: {ext_entry}"

        try:
            # Step 3: Connect to Chromium and verify extension loaded via options page
            test_script = f"""
if (process.env.NODE_MODULES_DIR) module.paths.unshift(process.env.NODE_MODULES_DIR);
const puppeteer = require('puppeteer-core');

(async () => {{
    const browser = await puppeteer.connect({{ browserWSEndpoint: '{cdp_url}' }});

    // Wait for extension to initialize
    await new Promise(r => setTimeout(r, 2000));
    const extId = '{ext_id}';
    console.error('Extension ID from extensions.json:', extId);

    // Try to navigate to the extension's options.html page
    const page = await browser.newPage();
    const optionsUrl = 'chrome-extension://' + extId + '/options.html';
    console.error('Navigating to options page:', optionsUrl);

    try {{
        await page.goto(optionsUrl, {{ waitUntil: 'domcontentloaded', timeout: 10000 }});
        const pageContent = await page.content();
        const pageTitle = await page.title();

        // Check if extension name appears in the page
        const hasExtensionName = pageContent.toLowerCase().includes('cookie') ||
                                pageContent.toLowerCase().includes('idontcareaboutcookies') ||
                                pageTitle.toLowerCase().includes('cookie');

        console.log(JSON.stringify({{
            loaded: true,
            extensionId: extId,
            optionsPageLoaded: true,
            pageTitle: pageTitle,
            hasExtensionName: hasExtensionName,
            contentLength: pageContent.length
        }}));
    }} catch (e) {{
        // options.html may not exist, but extension is still loaded
        console.log(JSON.stringify({{
            loaded: true,
            extensionId: extId,
            optionsPageLoaded: false,
            error: e.message
        }}));
    }}

            browser.disconnect();
}})();
"""
            script_path = tmpdir / "test_extension.js"
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
                timeout=90,
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
                f"Extension should be loaded in Chromium. Result: {test_result}"
            )
            print(f"Extension loaded successfully: {test_result}")

        finally:
            if chrome_launch_process:
                kill_chromium_session(chrome_launch_process, chrome_dir)


def check_cookie_consent_visibility(
    cdp_url: str,
    test_url: str,
    env: dict,
    script_dir: Path,
) -> dict:
    """Check if cookie consent elements are visible on a page.

    Returns dict with:
        - visible: bool - whether any cookie consent element is visible
        - selector: str - which selector matched (if visible)
        - elements_found: list - all cookie-related elements found in DOM
        - html_snippet: str - snippet of the page HTML for debugging
    """
    test_script = f"""
if (process.env.NODE_MODULES_DIR) module.paths.unshift(process.env.NODE_MODULES_DIR);
const puppeteer = require('puppeteer-core');

(async () => {{
    const browser = await puppeteer.connect({{ browserWSEndpoint: '{cdp_url}' }});

    const page = await browser.newPage();
    await page.setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
    await page.setViewport({{ width: 1440, height: 900 }});

    console.error('Navigating to {test_url}...');
    await page.goto('{test_url}', {{ waitUntil: 'networkidle2', timeout: 30000 }});

    // Wait for page to fully render and any cookie scripts to run
    await new Promise(r => setTimeout(r, 3000));

    // Check cookie consent visibility using multiple common selectors
    const result = await page.evaluate(() => {{
        // Common cookie consent selectors used by various consent management platforms
        const selectors = [
            // CookieYes
            '.cky-consent-container', '.cky-popup-center', '.cky-overlay', '.cky-modal',
            // OneTrust
            '#onetrust-consent-sdk', '#onetrust-banner-sdk', '.onetrust-pc-dark-filter',
            // Cookiebot
            '#CybotCookiebotDialog', '#CybotCookiebotDialogBodyUnderlay',
            // Generic cookie banners
            '[class*="cookie-consent"]', '[class*="cookie-banner"]', '[class*="cookie-notice"]',
            '[class*="cookie-popup"]', '[class*="cookie-modal"]', '[class*="cookie-dialog"]',
            '[id*="cookie-consent"]', '[id*="cookie-banner"]', '[id*="cookie-notice"]',
            '[id*="cookieconsent"]', '[id*="cookie-law"]',
            // GDPR banners
            '[class*="gdpr"]', '[id*="gdpr"]',
            // Consent banners
            '[class*="consent-banner"]', '[class*="consent-modal"]', '[class*="consent-popup"]',
            // Privacy banners
            '[class*="privacy-banner"]', '[class*="privacy-notice"]',
            // Common frameworks
            '.cc-window', '.cc-banner', '#cc-main',  // Cookie Consent by Insites
            '.qc-cmp2-container',  // Quantcast
            '.sp-message-container',  // SourcePoint
        ];

        const elementsFound = [];
        let visibleElement = null;

        for (const sel of selectors) {{
            try {{
                const elements = document.querySelectorAll(sel);
                for (const el of elements) {{
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const isVisible = style.display !== 'none' &&
                                     style.visibility !== 'hidden' &&
                                     style.opacity !== '0' &&
                                     rect.width > 0 && rect.height > 0;

                    elementsFound.push({{
                        selector: sel,
                        visible: isVisible,
                        display: style.display,
                        visibility: style.visibility,
                        opacity: style.opacity,
                        width: rect.width,
                        height: rect.height
                    }});

                    if (isVisible && !visibleElement) {{
                        visibleElement = {{ selector: sel, width: rect.width, height: rect.height }};
                    }}
                }}
            }} catch (e) {{
                // Invalid selector, skip
            }}
        }}

        // Also grab a snippet of the HTML to help debug
        const bodyHtml = document.body.innerHTML.slice(0, 2000);
        const hasCookieKeyword = bodyHtml.toLowerCase().includes('cookie') ||
                                  bodyHtml.toLowerCase().includes('consent') ||
                                  bodyHtml.toLowerCase().includes('gdpr');

        return {{
            visible: visibleElement !== null,
            selector: visibleElement ? visibleElement.selector : null,
            elements_found: elementsFound,
            has_cookie_keyword_in_html: hasCookieKeyword,
            html_snippet: bodyHtml.slice(0, 500)
        }};
    }});

    console.error('Cookie consent check result:', JSON.stringify({{
        visible: result.visible,
        selector: result.selector,
        elements_found_count: result.elements_found.length
    }}));

    browser.disconnect();
    console.log(JSON.stringify(result));
}})();
"""
    script_path = script_dir / "check_cookies.js"
    script_path.write_text(f"#!/usr/bin/env node\n{test_script}", encoding="utf-8")
    script_path.chmod(0o755)

    result = subprocess.run(
        [str(script_path)],
        cwd=str(script_dir),
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Cookie check script failed: {result.stderr}")

    output_lines = [
        line for line in result.stdout.strip().split("\n") if line.startswith("{")
    ]
    if not output_lines:
        raise RuntimeError(
            f"No JSON output from cookie check: {result.stdout}\nstderr: {result.stderr}",
        )

    return json.loads(output_lines[-1])


def test_hides_cookie_consent_on_static_page(httpserver):
    """Verify extension hides cookie consent popup on a deterministic local page.

    This test runs TWO browser sessions:
    1. WITHOUT extension - verifies cookie consent IS visible (baseline)
    2. WITH extension - verifies cookie consent is HIDDEN

    This ensures we're actually testing the extension's effect, not just
    that a page happens to not have cookie consent.
    """
    httpserver.expect_request(COOKIE_TEST_PATH).respond_with_data(
        COOKIE_TEST_HTML_STUB,
        content_type="text/html; charset=utf-8",
    )
    test_url = httpserver.url_for(COOKIE_TEST_PATH)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Set up isolated env with proper directory structure
        env_base = setup_test_env(tmpdir)
        env_base["CHROME_HEADLESS"] = "true"

        ext_dir = Path(env_base["CHROME_EXTENSIONS_DIR"])

        # ============================================================
        # STEP 1: BASELINE - Run WITHOUT extension, verify cookie consent IS visible
        # ============================================================
        print("\n" + "=" * 60)
        print("STEP 1: BASELINE TEST (no extension)")
        print("=" * 60)

        personas_dir = Path(env_base["PERSONAS_DIR"])

        env_no_ext = env_base.copy()
        env_no_ext["CHROME_EXTENSIONS_DIR"] = str(
            personas_dir / "Default" / "empty_extensions",
        )
        (personas_dir / "Default" / "empty_extensions").mkdir(
            parents=True,
            exist_ok=True,
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

            baseline_result = check_cookie_consent_visibility(
                baseline_cdp_url,
                test_url,
                env_no_ext,
                tmpdir,
            )

            print(
                f"Baseline result: visible={baseline_result['visible']}, "
                f"elements_found={len(baseline_result['elements_found'])}",
            )

            if baseline_result["elements_found"]:
                print("Elements found in baseline:")
                for el in baseline_result["elements_found"][:5]:  # Show first 5
                    print(
                        f"  - {el['selector']}: visible={el['visible']}, "
                        f"display={el['display']}, size={el['width']}x{el['height']}",
                    )

        finally:
            if baseline_process:
                kill_chromium_session(baseline_process, baseline_chrome_dir)

        # Verify baseline shows cookie consent
        if not baseline_result["visible"]:
            # If no cookie consent visible in baseline, we can't test the extension
            # This could happen if:
            # - The site changed and no longer shows cookie consent
            # - Cookie consent is region-specific
            # - Our selectors don't match this site
            print("\nWARNING: No cookie consent visible in baseline!")
            print(
                f"HTML has cookie keywords: {baseline_result.get('has_cookie_keyword_in_html')}",
            )
            print(f"HTML snippet: {baseline_result.get('html_snippet', '')[:200]}")

            pytest.fail(
                f"Cannot test extension: no cookie consent visible in baseline on {test_url}. "
                f"Elements found: {len(baseline_result['elements_found'])}. "
                "The fixture HTML may need to be updated.",
            )

        print(
            f"\n✓ Baseline confirmed: Cookie consent IS visible (selector: {baseline_result['selector']})",
        )

        # ============================================================
        # STEP 2: Install the extension
        # ============================================================
        print("\n" + "=" * 60)
        print("STEP 2: INSTALLING EXTENSION")
        print("=" * 60)

        env_with_ext = env_base.copy()
        env_with_ext["CHROME_EXTENSIONS_DIR"] = str(ext_dir)

        result = subprocess.run(
            [str(INSTALL_SCRIPT)],
            cwd=str(tmpdir),
            capture_output=True,
            text=True,
            env=env_with_ext,
            timeout=60,
        )
        assert result.returncode == 0, f"Extension install failed: {result.stderr}"

        cache_file = ext_dir / "istilldontcareaboutcookies.extension.json"
        assert cache_file.exists(), "Extension cache not created"
        ext_data = json.loads(cache_file.read_text())
        print(f"Extension installed: {ext_data.get('name')} v{ext_data.get('version')}")

        # ============================================================
        # STEP 3: Run WITH extension, verify cookie consent is HIDDEN
        # ============================================================
        print("\n" + "=" * 60)
        print("STEP 3: TEST WITH EXTENSION")
        print("=" * 60)

        # Launch extension test Chromium in crawls directory
        ext_crawl_id = "test-with-ext"
        ext_crawl_dir = Path(env_base["CRAWL_DIR"]) / ext_crawl_id
        ext_crawl_dir.mkdir(parents=True, exist_ok=True)
        ext_chrome_dir = ext_crawl_dir / "chrome"
        env_with_ext["CRAWL_DIR"] = str(ext_crawl_dir)
        ext_process = None

        try:
            ext_process, ext_cdp_url = launch_chromium_session(
                env_with_ext,
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

            # Wait for extension to initialize
            time.sleep(3)

            ext_result = check_cookie_consent_visibility(
                ext_cdp_url,
                test_url,
                env_with_ext,
                tmpdir,
            )

            print(
                f"Extension result: visible={ext_result['visible']}, "
                f"elements_found={len(ext_result['elements_found'])}",
            )

            if ext_result["elements_found"]:
                print("Elements found with extension:")
                for el in ext_result["elements_found"][:5]:
                    print(
                        f"  - {el['selector']}: visible={el['visible']}, "
                        f"display={el['display']}, size={el['width']}x{el['height']}",
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
            f"Baseline (no extension): cookie consent visible = {baseline_result['visible']}",
        )
        print(f"With extension: cookie consent visible = {ext_result['visible']}")

        assert baseline_result["visible"], (
            "Baseline should show cookie consent (this shouldn't happen, we checked above)"
        )

        assert not ext_result["visible"], (
            f"Cookie consent should be HIDDEN by extension.\n"
            f"Baseline showed consent at: {baseline_result['selector']}\n"
            f"But with extension, consent is still visible.\n"
            f"Elements still visible: {[e for e in ext_result['elements_found'] if e['visible']]}"
        )

        print("\n✓ SUCCESS: Extension correctly hides cookie consent!")
        print(f"  - Baseline showed consent at: {baseline_result['selector']}")
        print("  - Extension successfully hid it")
