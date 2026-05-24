"""Integration tests for puppeteer plugin."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


from abx_plugins.plugins.base.test_utils import (
    get_hook_script,
    get_hydrated_required_binaries,
    get_plugin_dir,
)
from abx_plugins.plugins.puppeteer.on_BinaryRequest__12_puppeteer import (
    _is_explicit_path,
    _load_binary_from_path,
)


PLUGIN_DIR = get_plugin_dir(__file__)
BINARY_HOOK = get_hook_script(PLUGIN_DIR, "on_BinaryRequest__*_puppeteer.py")
NPM_BINARY_HOOK = PLUGIN_DIR.parent / "npm" / "on_BinaryRequest__10_npm.py"
CHROME_PLUGIN_DIR = PLUGIN_DIR.parent / "chrome"


def test_hook_scripts_exist():
    assert BINARY_HOOK and BINARY_HOOK.exists(), f"Hook not found: {BINARY_HOOK}"


def test_crawl_hook_emits_puppeteer_binary_request():
    binary = next(
        record
        for record in get_hydrated_required_binaries(PLUGIN_DIR)
        if record.get("name") == "puppeteer"
    )
    assert "npm" in binary.get("binproviders", ""), (
        "puppeteer should be installable via npm provider"
    )
    install_args = binary["overrides"]["npm"]["install_args"]
    assert "abxbus@^2.5.4" in install_args
    assert "--min-release-age=0" in install_args


def test_chrome_plugin_declares_puppeteer_dependency():
    config = json.loads((CHROME_PLUGIN_DIR / "config.json").read_text())
    assert "puppeteer" in config["required_plugins"]


def test_crawl_hook_respects_configured_chrome_binary():
    browser_name = "chrome"
    env = os.environ.copy()
    env["CHROME_BINARY"] = browser_name
    binary_record = next(
        record
        for record in get_hydrated_required_binaries(CHROME_PLUGIN_DIR, env=env)
        if record.get("name") == browser_name
    )
    assert binary_record is not None
    assert binary_record.get("type", "BinaryRequest") == "BinaryRequest"
    assert binary_record["name"] == browser_name
    assert binary_record["overrides"]["puppeteer"] == {
        "install_args": [
            "chromium@latest",
        ],
    }


def test_resolve_binary_reference_accepts_explicit_paths(
    tmp_path: Path,
):
    browser_name = "chrome"
    binary_path = tmp_path / browser_name
    version_output = "Google Chrome for Testing 123.4.5\n"
    binary_path.write_text(f"#!/bin/sh\necho '{version_output.strip()}'\n")
    binary_path.chmod(0o755)

    binary = _load_binary_from_path(str(binary_path), browser_name)
    assert binary is not None
    assert str(binary.abspath) == str(binary_path)


def test_resolve_binary_reference_rejects_bare_names():
    browser_name = "chrome"
    assert not _is_explicit_path(browser_name)
    assert _load_binary_from_path(browser_name, browser_name) is None


def test_binary_hook_fast_path_does_not_emit_machine_record(tmp_path: Path):
    fake_browser = tmp_path / "fake-chrome"
    fake_browser.write_text(
        "#!/bin/sh\necho 'Google Chrome for Testing 123.4.5'\n",
    )
    fake_browser.chmod(0o755)

    env = os.environ.copy()
    env["CHROME_BINARY"] = str(fake_browser)

    result = subprocess.run(
        [str(BINARY_HOOK), "--name=chrome", "--binproviders=puppeteer"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, (
        "puppeteer binary hook fast path failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    records = [
        json.loads(line)
        for line in result.stdout.splitlines()
        if line.strip().startswith("{")
    ]
    binary_record = next(
        (
            r
            for r in records
            if (r.get("type") == "Binary" and r.get("name") == "chrome")
        ),
        None,
    )
    assert binary_record is not None, f"Expected Binary record, got: {records}"
    assert not any(r.get("type") == "Machine" for r in records), records


def test_puppeteer_installs_chromium():
    assert shutil.which("npm"), "npm is required for puppeteer installation"
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env["HOME"] = str(tmpdir)
        env.pop("LIB_DIR", None)

        puppeteer_record = next(
            record
            for record in get_hydrated_required_binaries(PLUGIN_DIR)
            if record.get("name") == "puppeteer"
        )
        assert puppeteer_record

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
            if (r.get("type") == "Binary" and r.get("name") == "chromium")
        ]
        assert binaries, f"Expected Binary record for chromium, got: {records}"
        abspath = binaries[0].get("abspath")
        assert abspath and Path(abspath).exists(), (
            f"Chromium binary path invalid: {abspath}"
        )
