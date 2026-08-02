from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from abx_plugins.plugins.base.utils import load_required_binary


REPO_ROOT = Path(__file__).resolve().parents[1]
CHROME_CONFIG = REPO_ROOT / "abx_plugins" / "plugins" / "chrome" / "config.json"
ARCHIVEWEBPAGE_CONFIG = (
    REPO_ROOT / "abx_plugins" / "plugins" / "archivewebpage" / "config.json"
)


def _resolve_node_binary(config: dict, lib_dir: Path, env: dict[str, str]) -> str:
    record = next(
        item for item in config["required_binaries"] if item["name"] == "{NODE_BINARY}"
    )
    node_record = {
        **record,
        "name": str(config["properties"]["NODE_BINARY"]["default"]),
    }
    loaded = load_required_binary(
        node_record,
        config={"ABXPKG_LIB_DIR": str(lib_dir)},
        environ=env,
        install=True,
    )
    assert loaded.loaded_abspath
    assert Path(loaded.loaded_abspath).is_file()
    assert loaded.loaded_binprovider
    assert loaded.loaded_binprovider.name == "node"
    return str(loaded.loaded_abspath)


def test_chrome_config_installs_abxbus_js_module(tmp_path: Path) -> None:
    config = json.loads(CHROME_CONFIG.read_text(encoding="utf-8"))
    record = next(
        item for item in config["required_binaries"] if item["name"] == "abxbus"
    )

    lib_dir = tmp_path / "lib"
    env = os.environ.copy()
    env["ABXPKG_LIB_DIR"] = str(lib_dir)
    env["ABXPKG_MIN_RELEASE_AGE"] = "0"
    clean_path = []
    for entry in env.get("PATH", "").split(os.pathsep):
        if any(
            (Path(entry) / binary_name).is_file()
            and os.access(Path(entry) / binary_name, os.X_OK)
            for binary_name in ("abxbus", "node", "npm")
        ):
            continue
        clean_path.append(entry)
    env["PATH"] = os.pathsep.join(clean_path)
    node_binary = _resolve_node_binary(config, lib_dir, env)

    loaded = load_required_binary(
        record,
        config={"ABXPKG_LIB_DIR": str(lib_dir)},
        environ=env,
        install=True,
    )

    assert loaded.loaded_abspath
    assert Path(loaded.loaded_abspath).exists()
    assert loaded.loaded_binprovider
    assert loaded.loaded_binprovider.name == "pnpm"
    package_root = Path(loaded.loaded_abspath).parents[2]
    package = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    assert package["version"] == "2.5.45"
    legacy_semaphore_dirname = "_".join(("browser", "use", "semaphores"))
    assert not any(
        legacy_semaphore_dirname in path.read_text(encoding="utf-8")
        for path in package_root.rglob("*")
        if path.is_file() and path.suffix in {".js", ".map", ".ts"}
    )

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


def test_chrome_host_modules_are_validated_before_reuse() -> None:
    config = json.loads(CHROME_CONFIG.read_text(encoding="utf-8"))
    records = {item["name"]: item for item in config["required_binaries"]}

    assert records["abxbus"]["overrides"]["env"]["version"] == [
        "node",
        "-p",
        "require('abxbus/package.json').version",
    ]
    assert records["abxbus"]["min_version"] == "2.5.45"
    assert records["abxbus"]["overrides"]["pnpm"]["install_args"] == ["abxbus@2.5.45"]
    assert records["abxbus"]["overrides"]["pnpm"]["version"] == "2.5.45"
    assert records["browsers"]["overrides"]["env"]["version"] == [
        "{NODE_BINARY}",
        "-p",
        "require('puppeteer/package.json').version",
    ]
    assert records["browsers"]["overrides"]["pnpm"]["version"] == "3.0.4"


def test_chrome_config_pins_puppeteer_dependencies() -> None:
    config = json.loads(CHROME_CONFIG.read_text(encoding="utf-8"))
    record = next(
        item for item in config["required_binaries"] if item["name"] == "browsers"
    )

    assert record["overrides"]["pnpm"]["install_args"] == [
        "@puppeteer/browsers@3.0.4",
        "puppeteer@25.1.0",
    ]


def test_chrome_config_installs_puppeteer_js_module(tmp_path: Path) -> None:
    _assert_config_installs_puppeteer(CHROME_CONFIG, tmp_path)


def test_archivewebpage_config_depends_on_chrome_for_puppeteer_js_module() -> None:
    config = json.loads(ARCHIVEWEBPAGE_CONFIG.read_text(encoding="utf-8"))
    assert "chrome" in config["required_plugins"]
    assert not any(item["name"] == "browsers" for item in config["required_binaries"])


def _assert_config_installs_puppeteer(config_path: Path, tmp_path: Path) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    record = next(
        item for item in config["required_binaries"] if item["name"] == "browsers"
    )

    lib_dir = tmp_path / "lib"
    env = os.environ.copy()
    env["ABXPKG_LIB_DIR"] = str(lib_dir)
    env["ABXPKG_MIN_RELEASE_AGE"] = "3"
    clean_path = []
    for entry in env.get("PATH", "").split(os.pathsep):
        if any(
            (Path(entry) / binary_name).is_file()
            and os.access(Path(entry) / binary_name, os.X_OK)
            for binary_name in ("browsers", "node", "npm")
        ):
            continue
        clean_path.append(entry)
    env["PATH"] = os.pathsep.join(clean_path)
    node_binary = _resolve_node_binary(config, lib_dir, env)

    loaded = load_required_binary(
        record,
        config={"ABXPKG_LIB_DIR": str(lib_dir)},
        environ=env,
        install=True,
    )

    assert loaded.loaded_abspath
    assert Path(loaded.loaded_abspath).exists()
    assert loaded.loaded_binprovider
    assert loaded.loaded_binprovider.name == "pnpm"

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
