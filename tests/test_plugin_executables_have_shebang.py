from __future__ import annotations

import json
import shlex
import stat
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "abx_plugins" / "plugins"
SCRIPT_SUFFIXES = {".py", ".js", ".sh"}


def _iter_plugin_entrypoints() -> list[Path]:
    """Return hooks and manifest commands users can launch through abx-dl."""
    entrypoints = {
        path
        for path in PLUGINS_ROOT.rglob("on_*.*")
        if path.is_file()
        and path.suffix in SCRIPT_SUFFIXES
        and "tests" not in path.parts
    }
    for config_path in PLUGINS_ROOT.glob("*/config.json"):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for command in config.get("commands", {}).values():
            entrypoints.add(config_path.parent / command[0])
    return sorted(entrypoints)


def test_plugin_entrypoints_are_executable_and_have_shebang() -> None:
    failures: list[str] = []

    for script_path in _iter_plugin_entrypoints():
        rel_path = script_path.relative_to(REPO_ROOT)

        mode = script_path.stat().st_mode
        if not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            failures.append(f"{rel_path}: missing executable bit")

        first_line = script_path.read_text(
            encoding="utf-8",
            errors="ignore",
        ).splitlines()
        if not first_line or not first_line[0].startswith("#!"):
            failures.append(f"{rel_path}: missing shebang")

    assert not failures, "Plugin script validation failed:\n" + "\n".join(failures)


def test_plugin_entrypoints_declare_valid_abxpkg_script_commands() -> None:
    """Validate the command that the OS executes without guessing its dependencies.

    ``required_plugins`` controls orchestration order.  A hook's ``--deps-from``
    arguments independently declare which binary environments that process needs.
    Keeping those contracts separate lets hooks consume another plugin's files
    without unnecessarily installing or exporting that plugin's binaries.
    """
    failures: list[str] = []

    for script_path in _iter_plugin_entrypoints():
        lines = script_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not lines or not lines[0].startswith("#!"):
            continue

        rel_path = script_path.relative_to(REPO_ROOT)
        command = shlex.split(lines[0][2:])
        expected_runtime = {".py": "python3", ".js": "node"}.get(script_path.suffix)
        if command[:5] != ["/usr/bin/env", "-S", "abxpkg", "run", "--script"]:
            failures.append(f"{rel_path}: invalid abxpkg script command: {lines[0]!r}")
            continue
        if expected_runtime is not None and command[-1] != expected_runtime:
            failures.append(
                f"{rel_path}: expected {expected_runtime!r} runtime, got {command[-1]!r}",
            )

        metadata_marker = (
            "# /// script" if script_path.suffix == ".py" else "// /// script"
        )
        if metadata_marker not in lines[:5]:
            failures.append(f"{rel_path}: missing abxpkg script metadata block")

        for argument in command:
            if not argument.startswith("--deps-from="):
                continue
            for config_spec in argument.removeprefix("--deps-from=").split(","):
                config_name, separator, field_name = config_spec.partition(":")
                config_path = (script_path.parent / config_name).resolve()
                if separator != ":" or not config_path.is_file():
                    failures.append(
                        f"{rel_path}: invalid dependency source {config_spec!r}",
                    )
                    continue
                config = json.loads(config_path.read_text(encoding="utf-8"))
                if not isinstance(config.get(field_name), list):
                    failures.append(
                        f"{rel_path}: {config_spec!r} does not reference a list",
                    )

    assert not failures, "Plugin script runner validation failed:\n" + "\n".join(
        failures,
    )


def test_sonic_client_commands_do_not_resolve_the_server_binary() -> None:
    """Client RPCs must not install the separately managed Sonic daemon."""
    search_script = PLUGINS_ROOT / "search_backend_sonic" / "search.py"
    command = shlex.split(search_script.read_text(encoding="utf-8").splitlines()[0][2:])

    assert not any(argument.startswith("--deps-from=") for argument in command)
