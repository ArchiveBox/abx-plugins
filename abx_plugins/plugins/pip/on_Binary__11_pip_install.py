#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pydantic-settings",
#   "click",
#   "rich-click",
#   "abx-pkg",
#   "abx-plugins",
# ]
# [tool.uv.sources]
# abx-plugins = { path = "../../..", editable = true }
# ///
#
# Install a binary using pip package manager.
#
# Usage: on_Binary__11_pip_install.py --binary-id=<uuid> --machine-id=<uuid> --name=<name>
# Output: Binary JSONL record to stdout after installation
#
# Environment variables:
#     LIB_DIR: Library directory (default: ~/.config/abx/lib)

import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import fcntl

from abx_plugins.plugins.base.utils import (
    emit_binary_record,
    emit_machine_record,
    enforce_lib_permissions,
)

import rich_click as click
from abx_pkg import Binary, EnvProvider, PipProvider, SemVer


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _pip_venv_is_ready(pip_venv_path: Path) -> bool:
    return _is_executable(pip_venv_path / "bin" / "python") and _is_executable(
        pip_venv_path / "bin" / "pip",
    )


def _seed_pip_venv(pip_venv_path: Path, preferred_python: str) -> bool:
    cmd = [preferred_python, "-m", "venv", str(pip_venv_path), "--upgrade-deps"]
    if pip_venv_path.exists():
        cmd.append("--clear")
    try:
        subprocess.run(cmd, check=True)
    except Exception:
        return False
    return _pip_venv_is_ready(pip_venv_path)


@contextmanager
def _locked_pip_venv(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@click.command()
@click.option("--binary-id", required=True, help="Binary UUID")
@click.option("--machine-id", required=True, help="Machine UUID")
@click.option("--plugin-name", required=True, help="Requesting plugin name")
@click.option("--hook-name", required=True, help="Requesting hook name")
@click.option("--name", required=True, help="Binary name to install")
@click.option("--binproviders", default="*", help="Allowed providers (comma-separated)")
@click.option("--min-version", default="", help="Minimum acceptable version")
@click.option("--overrides", default=None, help="JSON-encoded overrides dict")
def main(
    binary_id: str,
    machine_id: str,
    plugin_name: str,
    hook_name: str,
    name: str,
    binproviders: str,
    min_version: str,
    overrides: str | None,
):
    """Install binary using pip."""

    # Check if pip provider is allowed
    if binproviders != "*" and "pip" not in binproviders.split(","):
        click.echo(f"pip provider not allowed for {name}", err=True)
        sys.exit(0)

    # Get LIB_DIR from environment (optional)
    lib_dir = os.environ.get("LIB_DIR", "").strip()
    if not lib_dir:
        lib_dir = str(Path.home() / ".config" / "abx" / "lib")

    # Structure: lib/arm64-darwin/pip/venv (PipProvider will create venv automatically)
    pip_venv_path = Path(lib_dir) / "pip" / "venv"
    pip_venv_path.parent.mkdir(parents=True, exist_ok=True)
    pip_lock_path = pip_venv_path.parent / ".venv.lock"

    # Seed the pip venv with the same interpreter running this hook unless explicitly overridden.
    preferred_python = os.environ.get("PIP_VENV_PYTHON", "").strip()
    if not preferred_python and sys.version_info[:2] >= (3, 14):
        for candidate in ("python3.12", "python3.11", "python3.13"):
            candidate_path = shutil.which(candidate)
            if candidate_path:
                preferred_python = candidate_path
                break
    if not preferred_python:
        current_python = Path(sys.executable).resolve()
        if current_python.is_file():
            preferred_python = str(current_python)
        else:
            current_python = shutil.which(Path(sys.executable).name) or sys.executable
            if current_python:
                preferred_python = current_python
    if not preferred_python:
        for candidate in (
            "python3.12",
            "python3.11",
            "python3.10",
            "python3.13",
            "python3.14",
        ):
            candidate_path = shutil.which(candidate)
            if candidate_path:
                preferred_python = candidate_path
                break
    with _locked_pip_venv(pip_lock_path):
        # Repair partially created shared venvs before delegating to abx-pkg.
        if preferred_python and not _pip_venv_is_ready(pip_venv_path):
            _seed_pip_venv(pip_venv_path, preferred_python)

        # Use abx-pkg PipProvider to install binary with custom venv
        provider = PipProvider(pip_venv=pip_venv_path)
        if not provider.INSTALLER_BIN:
            click.echo("pip not available on this system", err=True)
            sys.exit(1)

        click.echo(f"Installing {name} via pip to venv at {pip_venv_path}...", err=True)

        try:
            # Parse overrides if provided
            overrides_dict = None
            if overrides:
                try:
                    overrides_dict = json.loads(overrides)
                    # Extract pip-specific overrides
                    overrides_dict = overrides_dict.get("pip", {})
                    click.echo(
                        f"Using pip install overrides: {overrides_dict}",
                        err=True,
                    )
                except json.JSONDecodeError:
                    click.echo(
                        f"Warning: Failed to parse overrides JSON: {overrides}",
                        err=True,
                    )

            binary = Binary(
                name=name,
                min_version=SemVer(min_version) if min_version else None,
                binproviders=[EnvProvider(), provider],
                overrides={"pip": overrides_dict} if overrides_dict else {},
            ).load_or_install()
        except Exception as e:
            click.echo(f"pip install failed: {e}", err=True)
            sys.exit(1)

    if not binary.abspath:
        click.echo(f"{name} not found after pip install", err=True)
        sys.exit(1)

    # Output Binary JSONL record to stdout
    emit_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version) if binary.version else "",
        sha256=binary.sha256 or "",
        binprovider="pip",
        machine_id=machine_id,
        binary_id=binary_id,
        plugin_name=plugin_name,
        hook_name=hook_name,
    )

    # Emit PATH update for pip bin dir
    pip_bin_dir = str(pip_venv_path / "bin")
    current_path = os.environ.get("PATH", "")

    # Check if pip_bin_dir is already in PATH
    path_dirs = current_path.split(":")
    new_path = f"{pip_bin_dir}:{current_path}" if current_path else pip_bin_dir
    if pip_bin_dir in path_dirs:
        new_path = current_path
    emit_machine_record(
        {
            "PATH": new_path,
        },
    )

    # Log human-readable info to stderr
    click.echo(f"Installed {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    # Lock down lib/ so snapshot hooks can read/execute but not write
    enforce_lib_permissions()

    sys.exit(0)


if __name__ == "__main__":
    main()
