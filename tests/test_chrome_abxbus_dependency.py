from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from abx_plugins.plugins.base.utils import load_required_binary


REPO_ROOT = Path(__file__).resolve().parents[1]
CHROME_CONFIG = REPO_ROOT / "abx_plugins" / "plugins" / "chrome" / "config.json"


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

    node_modules_dir = lib_dir / "npm" / "node_modules"
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
    node_binary = shutil.which("node")
    assert node_binary, "Node.js is required for Chrome JS dependency tests"

    config = json.loads(CHROME_CONFIG.read_text(encoding="utf-8"))
    record = next(
        item
        for item in config["required_binaries"]
        if item["name"] == "puppeteer-browsers"
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

    node_modules_dir = lib_dir / "npm" / "node_modules"
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
