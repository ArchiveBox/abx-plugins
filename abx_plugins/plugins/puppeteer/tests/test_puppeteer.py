"""Integration tests for puppeteer plugin."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import get_hook_script, get_plugin_dir
from abx_plugins.plugins.puppeteer.on_Binary__12_puppeteer_install import (
    _get_install_failure_hint,
    _resolve_binary_reference,
)


PLUGIN_DIR = get_plugin_dir(__file__)
CRAWL_HOOK = get_hook_script(PLUGIN_DIR, "on_Crawl__*_puppeteer_install*.py")
BINARY_HOOK = get_hook_script(PLUGIN_DIR, "on_Binary__*_puppeteer_install.py")
NPM_BINARY_HOOK = PLUGIN_DIR.parent / "npm" / "on_Binary__10_npm_install.py"
CHROME_CRAWL_HOOK = (
    PLUGIN_DIR.parent / "chrome" / "on_Crawl__70_chrome_install.finite.bg.py"
)


def test_hook_scripts_exist():
    assert CRAWL_HOOK and CRAWL_HOOK.exists(), f"Hook not found: {CRAWL_HOOK}"
    assert BINARY_HOOK and BINARY_HOOK.exists(), f"Hook not found: {BINARY_HOOK}"
    assert CRAWL_HOOK.name == "on_Crawl__60_puppeteer_install.py"


def test_crawl_hook_emits_puppeteer_binary():
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        result = subprocess.run(
            [str(CRAWL_HOOK)],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, f"crawl hook failed: {result.stderr}"
        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.strip().startswith("{")
        ]
        binaries = [
            r
            for r in records
            if r.get("type") == "Binary" and r.get("name") == "puppeteer"
        ]
        assert binaries, f"Expected Binary record for puppeteer, got: {records}"
        assert "npm" in binaries[0].get("binproviders", ""), (
            "puppeteer should be installable via npm provider"
        )


def test_puppeteer_install_failure_hint_for_claude_sandbox_dns_error():
    output = """
Error: getaddrinfo EAI_AGAIN storage.googleapis.com
    at GetAddrInfoReqWrap.onlookupall [as oncomplete] (node:dns:122:26) {
  errno: -3001,
  code: 'EAI_AGAIN',
  syscall: 'getaddrinfo',
  hostname: 'storage.googleapis.com'
}
"""
    hint = _get_install_failure_hint(output)
    assert hint is not None
    assert "Claude sandboxes" in hint
    assert (
        'NO_PROXY="localhost,127.0.0.1,169.254.169.254,metadata.google.internal,.svc.cluster.local,.local"'
        in hint
    )
    assert 'no_proxy="$NO_PROXY"' in hint


@pytest.mark.parametrize("browser_name", ["chrome", "chromium"])
def test_crawl_hook_respects_configured_chrome_binary(browser_name):
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["CHROME_BINARY"] = browser_name

        result = subprocess.run(
            [str(CHROME_CRAWL_HOOK)],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, f"crawl hook failed: {result.stderr}"
        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.strip().startswith("{")
        ]
        binary_record = next(
            (r for r in records if r.get("type") == "Binary"),
            None,
        )
        assert binary_record is not None, f"Expected Binary record, got: {records}"
        assert not any(r.get("type") == "ArchiveResult" for r in records), (
            f"Chrome crawl hook must not emit ArchiveResult: {records}"
        )
        assert binary_record["name"] == browser_name
        assert binary_record["overrides"]["puppeteer"] == [
            f"{browser_name}@latest",
            "--install-deps",
        ]


@pytest.mark.parametrize("browser_name", ["chrome", "chromium"])
def test_resolve_binary_reference_accepts_command_names(
    tmp_path: Path,
    monkeypatch,
    browser_name: str,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary_path = bin_dir / browser_name
    binary_path.write_text("#!/bin/sh\necho test\n")
    binary_path.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    assert _resolve_binary_reference(browser_name) == str(binary_path)


def test_puppeteer_installs_chromium():
    assert shutil.which("npm"), "npm is required for puppeteer installation"
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env["HOME"] = str(tmpdir)
        env.pop("LIB_DIR", None)

        crawl_result = subprocess.run(
            [str(CRAWL_HOOK)],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert crawl_result.returncode == 0, f"crawl hook failed: {crawl_result.stderr}"
        crawl_records = [
            json.loads(line)
            for line in crawl_result.stdout.splitlines()
            if line.strip().startswith("{")
        ]
        puppeteer_record = next(
            (
                r
                for r in crawl_records
                if r.get("type") == "Binary" and r.get("name") == "puppeteer"
            ),
            None,
        )
        assert puppeteer_record, (
            f"Expected puppeteer Binary record, got: {crawl_records}"
        )

        npm_result = subprocess.run(
            [
                str(NPM_BINARY_HOOK),
                "--name=puppeteer",
                f"--binproviders={puppeteer_record.get('binproviders', '*')}",
                "--overrides=" + json.dumps(puppeteer_record.get("overrides") or {}),
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert npm_result.returncode == 0, (
            "puppeteer npm install failed\n"
            f"stdout:\n{npm_result.stdout}\n"
            f"stderr:\n{npm_result.stderr}"
        )

        result = subprocess.run(
            [
                str(BINARY_HOOK),
                "--name=chromium",
                "--binproviders=puppeteer",
                "--overrides="
                + json.dumps({"puppeteer": ["chromium@latest", "--install-deps"]}),
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

        assert result.returncode == 0, (
            "puppeteer binary hook failed\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.strip().startswith("{")
        ]
        binaries = [
            r
            for r in records
            if r.get("type") == "Binary" and r.get("name") == "chromium"
        ]
        assert binaries, f"Expected Binary record for chromium, got: {records}"
        abspath = binaries[0].get("abspath")
        assert abspath and Path(abspath).exists(), (
            f"Chromium binary path invalid: {abspath}"
        )
