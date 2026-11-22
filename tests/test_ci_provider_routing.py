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
PUBLIC = {
    "abx_plugins/plugins/archiveimport/tests/test_archiveimport_public.py",
    "abx_plugins/plugins/calendar/tests/test_calendar_public.py",
    "abx_plugins/plugins/iwork/tests/test_iwork.py",
    "abx_plugins/plugins/microsoft365/tests/test_microsoft365.py",
    "abx_plugins/plugins/onedrive/tests/test_onedrive.py",
}
CAPTCHA_API = "abx_plugins/plugins/twocaptcha/tests/test_twocaptcha_api.py"
PAID_CAPTCHA = "abx_plugins/plugins/twocaptcha/tests/test_twocaptcha_paid.py"
API = {
    CAPTCHA_API,
    "abx_plugins/plugins/claudechrome/tests/test_claudechrome_api.py",
    "abx_plugins/plugins/browserharness/tests/test_browserharness_api.py",
    "abx_plugins/plugins/stagehand/tests/test_stagehand_api.py",
    "abx_plugins/plugins/opencode/tests/test_task.py",
}


@pytest.mark.parametrize(
    ("platform", "event", "opt_in", "paid"),
    [
        ("linux", "push", "true", False),
        ("linux", "pull_request", "true", False),
        ("macos", "schedule", "true", False),
        ("linux", "workflow_dispatch", "false", False),
        ("linux", "workflow_dispatch", "true", True),
    ],
)
def test_live_provider_partition_preserves_coverage(platform, event, opt_in, paid):
    result = subprocess.run(
        [sys.executable, str(ROOT / ".github/ci_test_matrix.py")],
        env={
            **os.environ,
            "CI_TEST_PLATFORM": platform,
            "CI_TEST_EVENT": event,
            "CI_RUN_PAID_CAPTCHA": opt_in,
        },
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
    assert set(paths["provider-matrix"]) == (
        AUTHENTICATED | ({PAID_CAPTCHA} if paid else set())
    )
    assert {
        item["path"] for item in matrices["provider-matrix"] if item["paid_captcha"]
    } == ({PAID_CAPTCHA} if paid else set())
    assert PAID_CAPTCHA not in paths["test-matrix"]
    assert set(paths["public-provider-matrix"]) == PUBLIC | API
    assert {
        item["path"] for item in matrices["public-provider-matrix"] if item["api"]
    } == API
    assert {
        item["path"] for item in matrices["public-provider-matrix"] if not item["api"]
    } == PUBLIC
    assert not (set(paths["test-matrix"]) & (AUTHENTICATED | PUBLIC | API))
    assert all(not item["ugnas"] for item in matrices["public-provider-matrix"])
    assigned = Counter(path for partition in paths.values() for path in partition)
    assert all(count == 1 for count in assigned.values())
    if platform == "linux":
        discovered = {
            str(path.relative_to(ROOT))
            for folder in (ROOT / "tests", ROOT / "abx_plugins/plugins")
            for path in folder.rglob("test_*.py")
        }
        assert set(assigned) == (discovered if paid else discovered - {PAID_CAPTCHA})
        # Public/no-login and local cookie lifecycle tests are not reclassified.
        for path in (
            "abx_plugins/plugins/claudechrome/tests/test_claudechrome.py",
            "abx_plugins/plugins/browserharness/tests/test_browserharness.py",
            "abx_plugins/plugins/stagehand/tests/test_stagehand.py",
            "tests/test_provider_noresults.py",
            "abx_plugins/plugins/twocaptcha/tests/test_twocaptcha.py",
            "abx_plugins/plugins/canva/tests/test_canva.py",
            "abx_plugins/plugins/figma/tests/test_figma.py",
            "abx_plugins/plugins/miro/tests/test_miro.py",
            "abx_plugins/plugins/microsoft365/tests/test_microsoft365_local.py",
            "abx_plugins/plugins/chrome/tests/test_persona_cookie_sources.py",
        ):
            assert path in paths["test-matrix"]


def test_live_workflow_is_independent_and_restricted():
    required = (ROOT / ".github/workflows/test-parallel.yml").read_text()
    live = (ROOT / ".github/workflows/test-providers.yml").read_text()
    release = (ROOT / ".github/workflows/release.yml").read_text()
    assert "  provider-capture:" not in required
    assert "TWOCAPTCHA_API_KEY" not in required
    assert "CI_RUN_PAID_CAPTCHA" not in required
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
    assert "default: false" in triggers.split("run_paid_captcha:", 1)[1]
    guard = (
        "github.event_name == 'workflow_dispatch' && inputs.run_paid_captcha == true"
    )
    assert guard in live.split("- id: matrix", 1)[1].split("\n  provider-capture:")[0]
    protected = live.split("  provider-capture:\n", 1)[1].split(
        "\n  public-providers:",
    )[0]
    assert "environment: provider-capture" in protected
    before_paid, paid_step = protected.split(
        "      - name: Run explicitly approved paid 2Captcha solve\n",
        1,
    )
    assert "TWOCAPTCHA_API_KEY" not in before_paid
    assert f"if: matrix.test.paid_captcha && {guard}" in paid_step
    assert "TWOCAPTCHA_API_KEY: ${{ secrets.TWOCAPTCHA_API_KEY }}" in paid_step
    assert "if: failure() && !matrix.test.paid_captcha" in paid_step
    public = live.split("  public-providers:\n", 1)[1]
    assert "    environment:" not in public
    assert 'AUTH_STORAGE_FILE: ""' in public and 'COOKIES_FILE: ""' in public
    assert "PROVIDER_AUTH_STORAGE_JSON" not in public
    before_test, test_and_after = public.split(
        "      - name: Run real hosted provider acceptance\n",
        1,
    )
    test_step, after_test = test_and_after.split(
        "      - name: Save failed public export evidence\n",
        1,
    )
    assert "secrets." not in before_test + after_test
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert (
            f"{key}: ${{{{ matrix.test.api && !contains(matrix.test.path, '/twocaptcha/') && secrets.{key} || '' }}}}"
            in test_step
        )
    assert (
        "matrix.test.api && contains(matrix.test.path, '/twocaptcha/') && secrets.TWOCAPTCHA_API_KEY"
        in test_step
    )
    assert "if: failure() && !matrix.test.api" in after_test
    api_results = after_test.split("      - name: Save failed API test results\n", 1)[1]
    api_results = api_results.split("      - name:", 1)[0]
    assert "if: failure() && matrix.test.api" in api_results
    assert "path: ${{ runner.temp }}/provider-pytest/results.xml\n" in api_results
    assert "**" not in api_results


def test_api_partition_retains_local_script_coverage():
    import ast

    for plugin in ("browserharness", "stagehand"):
        tests = ROOT / f"abx_plugins/plugins/{plugin}/tests"
        required = ast.parse((tests / f"test_{plugin}.py").read_text())
        live = ast.parse((tests / f"test_{plugin}_api.py").read_text())
        required_names = {
            node.name for node in required.body if isinstance(node, ast.FunctionDef)
        }
        live_names = {
            node.name for node in live.body if isinstance(node, ast.FunctionDef)
        }
        assert live_names == {"test_prompt_capture", "test_ten_challenges"}
        assert {
            "test_unconfigured_hook",
            "test_script_capture",
            "test_script_failure_preserves_tab",
            "test_saved_files",
            "test_domain_filtered_script",
        } <= required_names
        assert not required_names & live_names
    required = (
        ROOT / "abx_plugins/plugins/claudechrome/tests/test_claudechrome.py"
    ).read_text()
    assert "class TestClaudeChromePlugin:" in required
    assert "class TestClaudeChromeIntegration:" not in required
    assert "test_snapshot_hook_fails_without_api_key" in required
