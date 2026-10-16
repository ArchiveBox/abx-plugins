"""Every provider hook must quickly ignore a real unrelated Chrome page."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_NAVIGATE_HOOK,
    chrome_session,
    get_test_env,
)

PROVIDERS = (
    "calendar",
    "onedrive",
    "microsoft365",
    "notion",
    "figma",
    "tldraw",
    "excalidraw",
    "drawio",
    "miro",
    "canva",
    "box",
    "nextcloud",
    "wetransfer",
    "mega",
    "protondrive",
    "protondocs",
    "iclouddrive",
    "iwork",
)
PLUGINS = Path(__file__).resolve().parents[1] / "abx_plugins/plugins"
UNRELATED_URLS = (
    "https://example.com",
    # Provider names/document URLs in another site's query are not shares.
    "https://example.com/?url=https%3A%2F%2Fwww.figma.com%2Fdesign%2F0YpAEiii3cM0l3xidTbWPk&canva=design&miro=board",
    "https://www.figma.com/",
    "https://www.canva.com/",
    "https://miro.com/",
    "https://unrelated.invalid/",
    # A self-hosted share-looking path alone does not establish Nextcloud.
    "https://unrelated.invalid/s/shared-file",
)


@pytest.fixture(scope="module", params=UNRELATED_URLS)
def unrelated_page(request, tmp_path_factory, ensure_chrome_test_prereqs):
    root = tmp_path_factory.mktemp("provider_noresults")
    url = request.param
    failed_navigation = url.startswith("https://unrelated.invalid/")
    with chrome_session(
        root,
        test_url=url,
        navigate=not failed_navigation,
        timeout=60,
        env_overrides={"AUTH_STORAGE_FILE": ""},
    ) as (_, _, chrome, env):
        if failed_navigation:
            # Preserve the real failed-navigation target, just as the runner
            # does before invoking the remaining snapshot hooks. The normal
            # test helper raises and tears down Chrome on navigation failure.
            result = subprocess.run(
                [str(CHROME_NAVIGATE_HOOK), f"--url={url}"],
                cwd=chrome,
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
            )
            assert result.returncode != 0, result.stdout
            navigation = json.loads((chrome / "navigation.json").read_text())
            assert navigation["error"], navigation
        yield url, chrome.parent, env


@pytest.mark.parametrize("provider", PROVIDERS)
def test_provider_noresults(provider, unrelated_page):
    url, snapshot, env = unrelated_page
    hooks = list((PLUGINS / provider).glob("on_Snapshot__*"))
    assert len(hooks) == 1, f"{provider}: expected one snapshot hook, found {hooks}"
    output = snapshot / provider
    assert not output.exists(), f"{provider}: unexpected pre-existing output"
    started = time.monotonic()
    try:
        result = subprocess.run(
            [str(hooks[0]), f"--url={url}"],
            cwd=snapshot,
            env={**env, f"{provider.upper()}_ENABLED": "true"},
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        print(f"{provider}: {time.monotonic() - started:.3f}s")
    record = parse_jsonl_output(result.stdout)
    assert record is not None, result.stdout
    navigation = json.loads((snapshot / "chrome/navigation.json").read_text())
    if provider == "calendar" and navigation.get("error"):
        # Calendar discovery applies to every page, unlike URL-scoped
        # providers. A failed navigation cannot establish absence of a feed.
        assert result.returncode == 1, result.stdout
        assert record["status"] == "failed", result.stdout
        assert record["output_str"] == navigation["error"]
    else:
        assert result.returncode == 0, result.stderr
        assert record["status"] == "noresults", result.stdout
    assert result.stderr.strip() == record["output_str"]
    assert "\n" not in record["output_str"] and len(record["output_str"]) <= 70
    assert not output.exists(), f"{provider}: irrelevant page created output"


# calendar scans the live page for feed links, so it must always connect.
NAVIGATION_GATED_PROVIDERS = tuple(p for p in PROVIDERS if p != "calendar")


def run_provider_without_browser(provider, url, snapshot, env):
    """Run a provider hook in a snapshot whose chrome dir has no live session."""
    hooks = list((PLUGINS / provider).glob("on_Snapshot__*"))
    assert len(hooks) == 1, f"{provider}: expected one snapshot hook, found {hooks}"
    assert not (snapshot / "chrome/cdp_url.txt").exists()
    assert not (snapshot / "chrome/target_id.txt").exists()
    started = time.monotonic()
    try:
        return subprocess.run(
            [str(hooks[0]), f"--url={url}"],
            cwd=snapshot,
            env={
                **env,
                "SNAP_DIR": str(snapshot),
                "CRAWL_DIR": str(snapshot.parent / "crawl"),
                f"{provider.upper()}_ENABLED": "true",
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        print(f"{provider}: {time.monotonic() - started:.3f}s without browser")


@pytest.mark.parametrize("provider", NAVIGATION_GATED_PROVIDERS)
def test_provider_rules_out_navigated_url_without_connecting(
    provider,
    unrelated_page,
    tmp_path,
):
    """A recorded unrelated navigation is enough to return noresults.

    The snapshot gets the real navigation.json from chrome_navigate but no
    cdp_url.txt or target_id.txt, so any CDP connection attempt fails.
    """
    url, source_snapshot, env = unrelated_page
    navigation_json = (source_snapshot / "chrome/navigation.json").read_text()
    snapshot = tmp_path / "snapshot"
    (snapshot / "chrome").mkdir(parents=True)
    (snapshot / "chrome/navigation.json").write_text(navigation_json)

    result = run_provider_without_browser(provider, url, snapshot, env)
    record = parse_jsonl_output(result.stdout)
    assert record is not None, result.stdout + result.stderr
    if provider == "nextcloud" and url.endswith("/s/shared-file"):
        # A share-looking path still needs the live page to identify its server.
        assert result.returncode == 1, result.stdout
        assert record["status"] == "failed", result.stdout
        assert "No Chrome session found" in record["output_str"], result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assert record["status"] == "noresults", result.stdout
        assert result.stderr.strip() == record["output_str"]
    assert not (snapshot / provider).exists()


@pytest.mark.parametrize("provider", NAVIGATION_GATED_PROVIDERS)
def test_provider_without_navigation_record_inspects_live_page(
    provider,
    tmp_path,
    ensure_chrome_test_prereqs,
):
    """Without navigation.json, hooks keep connecting to check the live page."""
    snapshot = tmp_path / "snapshot"
    (snapshot / "chrome").mkdir(parents=True)
    result = run_provider_without_browser(
        provider,
        "https://example.com",
        snapshot,
        get_test_env(),
    )
    record = parse_jsonl_output(result.stdout)
    assert result.returncode == 1, result.stdout
    assert record is not None and record["status"] == "failed", result.stdout
    assert "No Chrome session found" in record["output_str"], result.stdout
    assert not (snapshot / provider).exists()
