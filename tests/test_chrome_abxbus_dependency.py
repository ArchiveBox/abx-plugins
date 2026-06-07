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
    env["LIB_DIR"] = str(lib_dir)
    env["ABXPKG_MIN_RELEASE_AGE"] = "0"

    loaded = load_required_binary(
        record,
        config={"LIB_DIR": str(lib_dir)},
        environ=env,
        install=True,
    )

    assert loaded.loaded_abspath
    assert Path(loaded.loaded_abspath).exists()

    install_root = Path(
        record["overrides"]["pnpm"]["install_root"].replace("{LIB_DIR}", str(lib_dir)),
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
    env["LIB_DIR"] = str(lib_dir)
    env["ABXPKG_MIN_RELEASE_AGE"] = "0"

    loaded = load_required_binary(
        record,
        config={"LIB_DIR": str(lib_dir)},
        environ=env,
        install=True,
    )

    assert loaded.loaded_abspath
    assert Path(loaded.loaded_abspath).exists()

    install_root = Path(
        record["overrides"]["pnpm"]["install_root"].replace("{LIB_DIR}", str(lib_dir)),
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
