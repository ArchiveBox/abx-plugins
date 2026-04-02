#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "click",
#   "rich-click",
#   "abx-pkg",
#   "abx-plugins",
# ]
# ///
#
# Install a binary using pip package manager.
#
# Usage: on_BinaryRequest__11_pip.py --name=<name>
# Output: Binary JSONL record to stdout after installation
#
# Environment variables:
#     LIB_DIR: Library directory (default: ~/.config/abx/lib)

import os
import subprocess
import sys
import json
from contextlib import contextmanager
from pathlib import Path

import fcntl

from abx_plugins.plugins.base.utils import (
    emit_installed_binary_record,
    enforce_lib_permissions,
    load_config,
    parse_extra_hook_args,
)

import rich_click as click

from abx_pkg import (
    Binary,
    EnvProvider,
    PipProvider,
)


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _pip_venv_is_ready(pip_venv_path: Path) -> bool:
    return _is_executable(pip_venv_path / "bin" / "python") and _is_executable(
        pip_venv_path / "bin" / "pip",
    )


def _direct_pip_install(
    pip_venv_path: Path,
    name: str,
    min_version: str = "",
) -> tuple[str, str] | None:
    """Fallback: install directly using the venv's pip, bypassing PipProvider/uv.

    This handles cases where abx-pkg's PipProvider fails (e.g. uv build issues
    in sandboxed/CI environments) but a direct pip install would succeed.
    """
    venv_pip = pip_venv_path / "bin" / "pip"
    if not _is_executable(venv_pip):
        return None

    # Ensure setuptools and wheel are available for building sdists
    subprocess.run(
        [str(venv_pip), "install", "--quiet", "setuptools", "wheel"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    # Build install spec with optional minimum version constraint
    install_spec = f"{name}>={min_version}" if min_version else name

    # Install the package with --prefer-binary to avoid build failures where possible
    proc = subprocess.run(
        [str(venv_pip), "install", "--prefer-binary", install_spec],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        return None

    # Check if binary exists in venv bin/
    binary_path = pip_venv_path / "bin" / name
    if not binary_path.is_file():
        return None

    # Get version via pip show
    version = ""
    try:
        show_proc = subprocess.run(
            [str(venv_pip), "show", name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        for line in show_proc.stdout.splitlines():
            if line.startswith("Version:"):
                version = line.split(":", 1)[1].strip()
                break
    except Exception:
        pass

    return str(binary_path), version


def _seed_pip_venv(pip_venv_path: Path, preferred_python: str) -> bool:
    cmd = [preferred_python, "-m", "venv", str(pip_venv_path), "--upgrade-deps"]
    if pip_venv_path.exists():
        cmd.append("--clear")
    try:
        subprocess.run(cmd, check=True)
    except Exception:
        return False
    return _pip_venv_is_ready(pip_venv_path)


def _load_env_binary_abspath(binary_ref: str) -> str | None:
    raw_ref = str(binary_ref or "").strip()
    if not raw_ref:
        return None

    path_ref = Path(raw_ref).expanduser()
    overrides: dict[str, object] = {}
    if raw_ref.startswith(("~", ".", "/")) or "/" in raw_ref or "\\" in raw_ref:
        overrides = {"env": {"abspath": str(path_ref)}}
    lookup_name = path_ref.name if overrides else raw_ref

    try:
        binary = Binary.model_validate(
            {
                "name": lookup_name,
                "binproviders": [EnvProvider()],
                "overrides": overrides,
            },
        ).load()
    except Exception:
        return None
    if not binary or not binary.abspath:
        return None
    return str(binary.abspath)


@contextmanager
def _locked_pip_venv(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--name", required=True, help="Binary name to install")
@click.option("--binproviders", default="*", help="Allowed providers (comma-separated)")
@click.option("--min-version", default="", help="Minimum acceptable version")
@click.option("--overrides", default=None, help="JSON-encoded overrides dict")
@click.option(
    "--postinstall-scripts",
    is_flag=True,
    default=False,
    help="Use direct pip install to ensure console_scripts entry points are created",
)
def main(
    name: str,
    binproviders: str,
    min_version: str,
    overrides: str | None,
    postinstall_scripts: bool,
):
    """Install binary using pip."""
    config = load_config()

    # Check if pip provider is allowed
    if binproviders != "*" and "pip" not in binproviders.split(","):
        click.echo(f"pip provider not allowed for {name}", err=True)
        sys.exit(0)

    # Get LIB_DIR from environment (optional)
    lib_dir = (config.LIB_DIR or "").strip()
    if not lib_dir:
        lib_dir = str(Path.home() / ".config" / "abx" / "lib")

    # Structure: lib/arm64-darwin/pip/venv (PipProvider will create venv automatically)
    pip_venv_path = Path(lib_dir) / "pip" / "venv"
    pip_venv_path.parent.mkdir(parents=True, exist_ok=True)
    pip_lock_path = pip_venv_path.parent / ".venv.lock"

    # Seed the pip venv with the same interpreter running this hook unless explicitly overridden.
    preferred_python = (config.PIP_VENV_PYTHON or "").strip()
    if not preferred_python and sys.version_info[:2] >= (3, 14):
        for candidate in ("python3.12", "python3.11", "python3.13"):
            candidate_path = _load_env_binary_abspath(candidate)
            if candidate_path:
                preferred_python = candidate_path
                break
    if not preferred_python:
        current_python = Path(sys.executable).resolve()
        if current_python.is_file():
            preferred_python = str(current_python)
        else:
            current_python = (
                _load_env_binary_abspath(Path(sys.executable).name) or sys.executable
            )
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
            candidate_path = _load_env_binary_abspath(candidate)
            if candidate_path:
                preferred_python = candidate_path
                break
    with _locked_pip_venv(pip_lock_path):
        # Repair partially created shared venvs before delegating to abx-pkg.
        if preferred_python and not _pip_venv_is_ready(pip_venv_path):
            _seed_pip_venv(pip_venv_path, preferred_python)

        # If postinstall_scripts is set (from config.json), use direct pip to ensure
        # console_scripts entry points are properly created (bypasses uv/PipProvider
        # which can fail to create entry points in sandboxed/CI environments).
        if postinstall_scripts:
            click.echo(
                f"Installing {name} via direct pip (postinstall_scripts=true)...",
                err=True,
            )
            fallback = _direct_pip_install(pip_venv_path, name, min_version)
            if fallback:
                abspath, version = fallback
                emit_installed_binary_record(
                    name=name,
                    abspath=abspath,
                    version=version,
                    sha256="",
                    binprovider="pip",
                )
                click.echo(f"Installed {name} at {abspath}", err=True)
                click.echo(f"  version: {version}", err=True)
                enforce_lib_permissions()
                sys.exit(0)
            click.echo(
                f"Direct pip install failed for {name}, falling back to PipProvider...",
                err=True,
            )

        # Use abx-pkg PipProvider to install binary with custom venv
        provider = PipProvider(pip_venv=pip_venv_path)
        if not provider.INSTALLER_BIN_ABSPATH:
            click.echo("pip not available on this system", err=True)
            sys.exit(0)

        click.echo(f"Installing {name} via pip to venv at {pip_venv_path}...", err=True)

        try:
            context = click.get_current_context(silent=True)
            extra_kwargs = parse_extra_hook_args(context.args if context else [])
            binary = Binary.model_validate(
                {
                    **extra_kwargs,
                    "name": name,
                    "binproviders": [provider],
                    "min_version": min_version
                    or extra_kwargs.get("min_version")
                    or None,
                    "overrides": json.loads(overrides) if overrides else {},
                },
            )
            provider_overrides = binary.overrides.get("pip", {})
            if provider_overrides:
                click.echo(
                    f"Using pip install overrides: {provider_overrides}",
                    err=True,
                )

            binary = binary.load_or_install()
        except Exception as e:
            # PipProvider failed (e.g. uv build issues in sandboxed/CI environments),
            # fall back to direct pip install using the venv's own pip binary.
            click.echo(
                f"PipProvider failed ({e}), trying direct pip fallback...",
                err=True,
            )
            fallback = _direct_pip_install(pip_venv_path, name, min_version)
            if fallback:
                abspath, version = fallback
                click.echo(f"Direct pip fallback succeeded: {abspath}", err=True)
                emit_installed_binary_record(
                    name=name,
                    abspath=abspath,
                    version=version,
                    sha256="",
                    binprovider="pip",
                )
                click.echo(f"Installed {name} at {abspath}", err=True)
                click.echo(f"  version: {version}", err=True)
                enforce_lib_permissions()
                sys.exit(0)
            click.echo(f"pip install failed: {e}", err=True)
            sys.exit(1)

        if not binary.abspath:
            # Binary.load_or_install returned but abspath is empty, try direct fallback
            click.echo(
                f"{name} not found after PipProvider install, trying direct pip fallback...",
                err=True,
            )
            fallback = _direct_pip_install(pip_venv_path, name, min_version)
            if fallback:
                abspath, version = fallback
                click.echo(f"Direct pip fallback succeeded: {abspath}", err=True)
                emit_installed_binary_record(
                    name=name,
                    abspath=abspath,
                    version=version,
                    sha256="",
                    binprovider="pip",
                )
                click.echo(f"Installed {name} at {abspath}", err=True)
                click.echo(f"  version: {version}", err=True)
                enforce_lib_permissions()
                sys.exit(0)
            click.echo(f"{name} not found after pip install", err=True)
            sys.exit(1)

    # Output Binary JSONL record to stdout
    emit_installed_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version) if binary.version else "",
        sha256=binary.sha256 or "",
        binprovider="pip",
    )

    # Log human-readable info to stderr
    click.echo(f"Installed {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    # Lock down lib/ so snapshot hooks can read/execute but not write
    enforce_lib_permissions()

    sys.exit(0)


if __name__ == "__main__":
    main()
