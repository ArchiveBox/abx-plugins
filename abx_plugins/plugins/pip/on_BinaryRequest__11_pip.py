#!/usr/bin/env -S uv run --active --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "click",
#   "rich-click",
#   "abxpkg>=1.10.4",
#   "abx-plugins>=1.10.27",
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
from importlib import metadata
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

from abxpkg import (
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


def _resolve_python_abspath(binary_ref: str) -> str | None:
    raw_ref = str(binary_ref or "").strip()
    if not raw_ref:
        return None

    path_ref = Path(raw_ref).expanduser()
    if (raw_ref.startswith(("~", ".", "/")) or "/" in raw_ref) and _is_executable(
        path_ref,
    ):
        return str(path_ref.resolve())

    return _load_env_binary_abspath(raw_ref)


def _python_candidates(preferred_python: str) -> list[str]:
    if preferred_python:
        return [preferred_python]

    candidates: list[str] = []
    current_python = Path(sys.executable).resolve()
    if sys.version_info < (3, 13):
        candidates.append(
            str(current_python)
            if current_python.is_file()
            else Path(sys.executable).name,
        )

    candidates.extend(("python3.12", "python3.11", "python3.10", "python3.9"))

    current_python_ref = (
        str(current_python) if current_python.is_file() else Path(sys.executable).name
    )
    candidates.append(current_python_ref)
    candidates.extend(("python3.13", "python3.14"))

    deduped: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in deduped:
            deduped.append(candidate)
    return deduped


def _seed_first_pip_venv(
    pip_venv_path: Path,
    preferred_python: str,
) -> tuple[str | None, list[str]]:
    errors: list[str] = []
    for python_ref in _python_candidates(preferred_python):
        python_path = _resolve_python_abspath(python_ref)
        if not python_path:
            errors.append(f"{python_ref}: not found")
            continue
        if _seed_pip_venv(pip_venv_path, python_path):
            return python_path, errors
        errors.append(f"{python_ref}: failed to create venv")
    return None, errors


def _find_installed_module(
    pip_venv_path: Path,
    *,
    module_name: str,
    package_name: str,
) -> tuple[Path | None, str]:
    module_relpath = Path(*module_name.split("."))
    for site_packages in sorted((pip_venv_path / "lib").glob("python*/site-packages")):
        module_dir = site_packages / module_relpath
        module_file = site_packages / f"{module_relpath}.py"
        abspath = module_dir / "__init__.py" if module_dir.is_dir() else module_file
        if not abspath.exists():
            continue
        version = ""
        for dist in metadata.distributions(path=[str(site_packages)]):
            dist_name = dist.metadata["Name"] if "Name" in dist.metadata else ""
            if dist_name.lower().replace("_", "-") == package_name.lower().replace(
                "_",
                "-",
            ):
                version = dist.version
                break
        return abspath, version
    return None, ""


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
def main(
    name: str,
    binproviders: str,
    min_version: str,
    overrides: str | None,
):
    """Install binary using pip."""
    config = load_config()
    context = click.get_current_context(silent=True)
    extra_kwargs = parse_extra_hook_args(context.args if context else [])
    binary_overrides = json.loads(overrides) if overrides else {}
    provider_overrides = (
        binary_overrides.get("pip", {})
        if isinstance(binary_overrides, dict)
        and isinstance(binary_overrides.get("pip", {}), dict)
        else {}
    )

    # Check if pip provider is allowed
    if binproviders != "*" and "pip" not in binproviders.split(","):
        click.echo(f"pip provider not allowed for {name}", err=True)
        sys.exit(0)

    # Get LIB_DIR from environment (optional)
    lib_dir = (config.LIB_DIR or "").strip()
    if not lib_dir:
        lib_dir = str(Path.home() / ".config" / "abx" / "lib")

    # Structure: lib/pip/venv by default, or any plugin-declared provider
    # install_root override (PipProvider creates venv under install_root/venv).
    pip_install_root = Path(lib_dir) / "pip"
    effective_install_root = Path(
        provider_overrides.get("install_root") or pip_install_root,
    ).expanduser()
    pip_venv_path = effective_install_root / "venv"
    effective_install_root.mkdir(parents=True, exist_ok=True)
    pip_lock_path = effective_install_root / ".venv.lock"

    # Seed the pip venv with the preferred interpreter before abxpkg reuses it.
    preferred_python = (config.PIP_VENV_PYTHON or "").strip()
    with _locked_pip_venv(pip_lock_path):
        # Repair partially created shared venvs before delegating to abxpkg.
        if preferred_python and not _pip_venv_is_ready(pip_venv_path):
            seeded_python, seed_errors = _seed_first_pip_venv(
                pip_venv_path,
                preferred_python,
            )
            if not seeded_python:
                click.echo(
                    "Unable to create pip virtualenv with configured PIP_VENV_PYTHON. "
                    f"Tried: {', '.join(seed_errors)}",
                    err=True,
                )
                sys.exit(1)
        elif not _pip_venv_is_ready(pip_venv_path):
            seeded_python, seed_errors = _seed_first_pip_venv(
                pip_venv_path,
                preferred_python,
            )
            if not seeded_python:
                click.echo(
                    f"Unable to create pip virtualenv. Tried: {', '.join(seed_errors)}",
                    err=True,
                )
                sys.exit(1)

        # Use abxpkg PipProvider to install binary with the effective venv.
        provider = PipProvider(install_root=effective_install_root)
        try:
            provider.INSTALLER_BINARY()
        except Exception:
            click.echo("pip not available on this system", err=True)
            sys.exit(0)

        click.echo(f"Installing {name} via pip to venv at {pip_venv_path}...", err=True)

        try:
            module_name = str(provider_overrides.get("module_name") or "").strip()
            if module_name:
                install_args = provider_overrides.get("install_args") or [name]
                if not isinstance(install_args, list):
                    install_args = [str(install_args)]
                click.echo(
                    f"Using pip install overrides: {provider_overrides}",
                    err=True,
                )
                provider.default_install_handler(
                    name,
                    install_args=install_args,
                    postinstall_scripts=bool(
                        provider_overrides.get("postinstall_scripts", True),
                    ),
                    min_release_age=float(
                        provider_overrides.get(
                            "min_release_age",
                            provider.min_release_age or 0,
                        ),
                    ),
                    min_version=None,
                    no_cache=False,
                )
                package_name = str(provider_overrides.get("package_name") or name)
                module_path, module_version = _find_installed_module(
                    pip_venv_path,
                    module_name=module_name,
                    package_name=package_name,
                )
                if not module_path:
                    click.echo(
                        f"{module_name} not importable after pip install",
                        err=True,
                    )
                    sys.exit(1)
                binary = Binary.model_validate(
                    {
                        **extra_kwargs,
                        "name": name,
                        "binproviders": [provider],
                        "loaded_abspath": str(module_path),
                        "loaded_version": module_version,
                        "loaded_binprovider": provider,
                    },
                )
            else:
                binary = Binary.model_validate(
                    {
                        **extra_kwargs,
                        "name": name,
                        "binproviders": [provider],
                        "min_version": min_version
                        or extra_kwargs.get("min_version")
                        or None,
                        "overrides": binary_overrides,
                    },
                )
                provider_overrides = binary.overrides.get("pip", {})
                if provider_overrides:
                    click.echo(
                        f"Using pip install overrides: {provider_overrides}",
                        err=True,
                    )

                binary = binary.install()
        except Exception as e:
            click.echo(f"pip install failed: {e}", err=True)
            sys.exit(1)

    if not binary.abspath:
        click.echo(f"{name} not found after pip install", err=True)
        sys.exit(1)

    # Output Binary JSONL record to stdout
    emit_installed_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version) if binary.version else "",
        sha256=binary.sha256 or "",
        binprovider="pip",
        binary=binary,
    )

    # Log human-readable info to stderr
    click.echo(f"Installed {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    # Lock down lib/ so snapshot hooks can read/execute but not write
    enforce_lib_permissions()

    sys.exit(0)


if __name__ == "__main__":
    main()
