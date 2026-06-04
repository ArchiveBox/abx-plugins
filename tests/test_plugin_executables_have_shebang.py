from __future__ import annotations

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
            if lines[0] != "#!/usr/bin/env -S abxpkg run --script python3":
                failures.append(
                    f"{rel_path}: expected abxpkg script shebang, got {lines[0]!r}",
                )
        if any(line.strip().startswith("# dependencies = [") for line in lines[:20]):
            failures.append(f"{rel_path}: must not declare inline script dependencies")

    assert not failures, "Python plugin script runner validation failed:\n" + "\n".join(
        failures,
    )
