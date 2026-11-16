"""Live providers remain visible failures without gating deterministic releases."""

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AUTHENTICATED = {
    f"abx_plugins/plugins/{plugin}/tests/{name}"
    for plugin, name in (
        ("canva", "test_canva_authenticated.py"),
        ("figma", "test_figma_authenticated.py"),
        ("miro", "test_miro_authenticated.py"),
        ("ytdlp", "test_substack_authenticated.py"),
    )
}
PUBLIC = {"abx_plugins/plugins/iwork/tests/test_iwork.py"}


@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_live_provider_partition_preserves_coverage(platform):
    result = subprocess.run(
        [sys.executable, str(ROOT / ".github/ci_test_matrix.py")],
        env={**os.environ, "CI_TEST_PLATFORM": platform},
        capture_output=True,
        text=True,
        check=True,
    )
    matrices = {
        key: json.loads(value)
        for key, value in (line.split("=", 1) for line in result.stdout.splitlines())
    }
    paths = {
        key: [path for item in items for path in item["paths"]]
        for key, items in matrices.items()
    }
    assert set(paths["provider-matrix"]) == AUTHENTICATED
    assert set(paths["public-provider-matrix"]) == PUBLIC
    assert not (set(paths["test-matrix"]) & (AUTHENTICATED | PUBLIC))
    assert all(not item["ugnas"] for item in matrices["public-provider-matrix"])
    assigned = Counter(path for partition in paths.values() for path in partition)
    assert all(count == 1 for count in assigned.values())
    if platform == "linux":
        discovered = {
            str(path.relative_to(ROOT))
            for folder in (ROOT / "tests", ROOT / "abx_plugins/plugins")
            for path in folder.rglob("test_*.py")
        }
        assert set(assigned) == discovered
        # Public/no-login and local cookie lifecycle tests are not reclassified.
        for path in (
            "abx_plugins/plugins/canva/tests/test_canva.py",
            "abx_plugins/plugins/figma/tests/test_figma.py",
            "abx_plugins/plugins/miro/tests/test_miro.py",
            "abx_plugins/plugins/chrome/tests/test_persona_cookie_sources.py",
        ):
            assert path in paths["test-matrix"]


def test_live_workflow_is_independent_and_restricted():
    required = (ROOT / ".github/workflows/test-parallel.yml").read_text()
    live = (ROOT / ".github/workflows/test-providers.yml").read_text()
    release = (ROOT / ".github/workflows/release.yml").read_text()
    assert "  provider-capture:" not in required
    assert "workflows: [Parallel Tests]" in release
    assert "test-providers.yml" not in release
    triggers = live.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
    assert "  schedule:" in triggers and "  workflow_dispatch:" in triggers
    assert "push:" not in triggers and "pull_request:" not in triggers
    for key in ("prepare", "provider-capture", "public-providers"):
        section = live.split(f"  {key}:\n", 1)[1].split("    steps:\n", 1)[0]
        assert "github.repository == 'ArchiveBox/abx-plugins'" in section
        assert "github.ref == 'refs/heads/main'" in section
    assert "continue-on-error" not in live
    assert 'pytest -q "$TEST_PATH"' in live
    assert "Missing provider-capture authentication secret" in live
    assert "Missing provider-capture Substack cookies" in live
    assert live.count("ref: ${{ github.sha }}") == 3
    public = live.split("  public-providers:\n", 1)[1]
    assert "secrets." not in public and "    environment:" not in public
    assert 'AUTH_STORAGE_FILE: ""' in public and 'COOKIES_FILE: ""' in public
