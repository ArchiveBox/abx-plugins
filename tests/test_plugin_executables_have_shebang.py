from __future__ import annotations

import json
import stat
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "abx_plugins" / "plugins"
SCRIPT_SUFFIXES = {".py", ".js", ".sh"}


def _iter_plugin_scripts() -> list[Path]:
    return sorted(
        path
        for path in PLUGINS_ROOT.rglob("*")
        if path.is_file() and path.suffix in SCRIPT_SUFFIXES
    )


def _requires_shebang(script_path: Path) -> bool:
    if not script_path.name.startswith("on_"):
        return False
    if script_path.name == "__init__.py":
        return False
    if "tests" in script_path.parts:
        return False
    if "utils" in script_path.stem:
        return False
    return True


def _expected_deps_from(script_path: Path) -> str:
    config_path = script_path.parent / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config_specs = [
        f"../{plugin_name}/config.json:required_binaries"
        for plugin_name in config.get("required_plugins", [])
    ]
    config_specs.append("./config.json:required_binaries")
    return ",".join(config_specs)


def _expected_abxpkg_header(script_path: Path, binary_name: str) -> str:
    return (
        "#!/usr/bin/env -S abxpkg run --script "
        f"--deps-from={_expected_deps_from(script_path)} {binary_name}"
    )


def test_all_plugin_scripts_are_executable_and_have_shebang() -> None:
    failures: list[str] = []

    for script_path in _iter_plugin_scripts():
        rel_path = script_path.relative_to(REPO_ROOT)

        if not _requires_shebang(script_path):
            continue

        mode = script_path.stat().st_mode
        if not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            failures.append(f"{rel_path}: missing executable bit")

        if True:
            first_line = script_path.read_text(
                encoding="utf-8",
                errors="ignore",
            ).splitlines()
            if not first_line or not first_line[0].startswith("#!"):
                failures.append(f"{rel_path}: missing shebang")

    assert not failures, "Plugin script validation failed:\n" + "\n".join(failures)


def test_python_plugin_scripts_use_abxpkg_script_runner_without_inline_dependencies() -> (
    None
):
    failures: list[str] = []

    for script_path in _iter_plugin_scripts():
        if script_path.suffix != ".py" or "tests" in script_path.parts:
            continue
        if not (_requires_shebang(script_path) or script_path.name == "search.py"):
            continue
        rel_path = script_path.relative_to(REPO_ROOT)
        lines = script_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not lines:
            continue
        if lines[0].startswith("#!"):
            if lines[0] != _expected_abxpkg_header(script_path, "python3"):
                failures.append(
                    f"{rel_path}: expected abxpkg script shebang, got {lines[0]!r}",
                )
            if not (script_path.parent / "config.json").is_file():
                failures.append(
                    f"{rel_path}: missing local config.json for --deps-from",
                )
        if any(line.strip().startswith("# dependencies = [") for line in lines[:20]):
            failures.append(f"{rel_path}: must not declare inline script dependencies")

    assert not failures, "Python plugin script runner validation failed:\n" + "\n".join(
        failures,
    )


def test_javascript_plugin_hooks_use_abxpkg_node_script_runner() -> None:
    failures: list[str] = []

    for script_path in _iter_plugin_scripts():
        if script_path.suffix != ".js" or "tests" in script_path.parts:
            continue
        if not _requires_shebang(script_path):
            continue
        rel_path = script_path.relative_to(REPO_ROOT)
        lines = script_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not lines:
            continue
        expected_header = _expected_abxpkg_header(script_path, "node")
        if lines[0] != expected_header:
            failures.append(
                f"{rel_path}: expected abxpkg node script shebang, got {lines[0]!r}",
            )
        if not (script_path.parent / "config.json").is_file():
            failures.append(f"{rel_path}: missing local config.json for --deps-from")
        if not any(line.strip() == "// /// script" for line in lines[:5]):
            failures.append(f"{rel_path}: missing abxpkg script metadata block")
        if not any("runtime_binproviders" in line for line in lines[:20]):
            failures.append(f"{rel_path}: missing runtime_binproviders metadata")
        if any(line.strip().startswith("// dependencies = [") for line in lines[:20]):
            failures.append(f"{rel_path}: must not declare inline script dependencies")

    assert not failures, (
        "JavaScript plugin script runner validation failed:\n"
        + "\n".join(
            failures,
        )
    )
