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
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record is not None, result.stdout
    assert record["status"] == "noresults", result.stdout
    assert result.stderr.strip() == record["output_str"]
    assert "\n" not in record["output_str"] and len(record["output_str"]) <= 70
    assert not output.exists(), f"{provider}: irrelevant page created output"
