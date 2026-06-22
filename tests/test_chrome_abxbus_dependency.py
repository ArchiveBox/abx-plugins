from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from abx_plugins.plugins.base.utils import load_required_binary


REPO_ROOT = Path(__file__).resolve().parents[1]
CHROME_CONFIG = REPO_ROOT / "abx_plugins" / "plugins" / "chrome" / "config.json"
ARCHIVEWEBPAGE_CONFIG = (
    REPO_ROOT / "abx_plugins" / "plugins" / "archivewebpage" / "config.json"
)


def test_chrome_config_installs_abxbus_js_module(tmp_path: Path) -> None:
    node_binary = shutil.which("node")
    assert node_binary, "Node.js is required for Chrome JS dependency tests"

    config = json.loads(CHROME_CONFIG.read_text(encoding="utf-8"))
    record = next(
        item for item in config["required_binaries"] if item["name"] == "abxbus"
    )

    lib_dir = tmp_path / "lib"
    env = os.environ.copy()
    env["ABXPKG_LIB_DIR"] = str(lib_dir)
    env["ABXPKG_MIN_RELEASE_AGE"] = "0"

    loaded = load_required_binary(
        record,
        config={"ABXPKG_LIB_DIR": str(lib_dir)},
        environ=env,
        install=True,
    )

    assert loaded.loaded_abspath
    assert Path(loaded.loaded_abspath).exists()

    install_root = Path(
        record["overrides"]["pnpm"]["install_root"].replace(
            "{ABXPKG_LIB_DIR}",
            str(lib_dir),
        ),
    )
    node_modules_dir = install_root / "node_modules"
    result = subprocess.run(
        [
            node_binary,
            "-e",
            "const { retry } = require('abxbus'); process.stdout.write(typeof retry)",
        ],
        capture_output=True,
        text=True,
        env={**env, "NODE_PATH": str(node_modules_dir)},
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "function"


def test_chrome_config_keeps_min_release_age_zero_packages_in_separate_pnpm_root() -> (
    None
):
    config = json.loads(CHROME_CONFIG.read_text(encoding="utf-8"))
    pnpm_records = [
        item
        for item in config["required_binaries"]
        if "pnpm" in str(item.get("binproviders") or "").split(",")
    ]
    roots_by_policy: dict[str, set[object]] = {}
    for record in pnpm_records:
        root = record["overrides"]["pnpm"]["install_root"]
        roots_by_policy.setdefault(root, set()).add(record.get("min_release_age"))

    mixed_roots = {
        root: policies
        for root, policies in roots_by_policy.items()
        # pnpm validates every lockfile entry against the active policy. A
        # package intentionally installed with min_release_age=0 can write newer
        # transitive deps, so it must not share a lockfile with default-strict
        # browser packages that should keep the registry age gate enabled.
        if 0 in policies and len(policies) > 1
    }
    assert not mixed_roots


def test_chrome_config_installs_puppeteer_js_module(tmp_path: Path) -> None:
    _assert_config_installs_puppeteer(CHROME_CONFIG, tmp_path)


def test_archivewebpage_config_depends_on_chrome_for_puppeteer_js_module() -> None:
    config = json.loads(ARCHIVEWEBPAGE_CONFIG.read_text(encoding="utf-8"))
    assert "chrome" in config["required_plugins"]
    assert not any(item["name"] == "browsers" for item in config["required_binaries"])


def _assert_config_installs_puppeteer(config_path: Path, tmp_path: Path) -> None:
    node_binary = shutil.which("node")
    assert node_binary, "Node.js is required for Chrome JS dependency tests"

    config = json.loads(config_path.read_text(encoding="utf-8"))
    record = next(
        item for item in config["required_binaries"] if item["name"] == "browsers"
    )

    lib_dir = tmp_path / "lib"
    env = os.environ.copy()
    env["ABXPKG_LIB_DIR"] = str(lib_dir)
    env["ABXPKG_MIN_RELEASE_AGE"] = "3"

    loaded = load_required_binary(
        record,
        config={"ABXPKG_LIB_DIR": str(lib_dir)},
        environ=env,
        install=True,
    )

    assert loaded.loaded_abspath
    assert Path(loaded.loaded_abspath).exists()

    install_root = Path(
        record["overrides"]["pnpm"]["install_root"].replace(
            "{ABXPKG_LIB_DIR}",
            str(lib_dir),
        ),
    )
    node_modules_dir = install_root / "node_modules"
    result = subprocess.run(
        [
            node_binary,
            "-e",
            "const puppeteer = require('puppeteer'); process.stdout.write(typeof puppeteer.launch)",
        ],
        capture_output=True,
        text=True,
        env={**env, "NODE_PATH": str(node_modules_dir)},
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "function"
