#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "rich-click",
#   "abx-pkg",
#   "abx-plugins",
# ]
# [tool.uv.sources]
# abx-plugins = { path = "../../..", editable = true }
# ///
#
# Install a binary using Homebrew package manager and output a Binary JSONL record.
#
# Usage:
#     ./on_Binary__12_brew_install.py [...] > events.jsonl
#

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import rich_click as click
from abx_pkg import Binary, BrewProvider, EnvProvider, SemVer

from abx_plugins.plugins.base.utils import emit_binary_record

if TYPE_CHECKING:
    from abx_pkg.binprovider import BinProvider, HandlerDict


def get_brew_prefix(provider: BrewProvider) -> Path | None:
    brew_bin = getattr(provider, "INSTALLER_BIN_ABSPATH", None) or shutil.which(
        provider.INSTALLER_BIN
    )
    if not brew_bin:
        return None
    return Path(brew_bin).resolve().parent.parent


def get_package_bin_dirs(
    provider: BrewProvider, install_args: list[object]
) -> tuple[list[str], list[Path]]:
    brew_prefix = get_brew_prefix(provider)
    if not brew_prefix:
        return [], []

    package_names: list[str] = []
    bin_dirs: list[Path] = []
    for package in install_args:
        if not isinstance(package, str) or package.startswith("-"):
            continue
        package_names.append(package)
        bin_dirs.append(brew_prefix / "opt" / package / "bin")
        bin_dirs.extend((brew_prefix / "Cellar" / package).glob("*/bin"))

    return package_names, bin_dirs


@click.command()
@click.option("--machine-id", required=True, help="Machine UUID")
@click.option("--binary-id", required=True, help="Dependency UUID")
@click.option("--plugin-name", required=True, help="Requesting plugin name")
@click.option("--hook-name", required=True, help="Requesting hook name")
@click.option("--name", required=True, help="Binary name to install")
@click.option("--binproviders", default="*", help="Allowed providers (comma-separated)")
@click.option("--min-version", default="", help="Minimum acceptable version")
@click.option("--custom-cmd", default=None, help="Custom install command")
@click.option("--overrides", default=None, help="JSON-encoded overrides dict")
def main(
    binary_id: str,
    machine_id: str,
    plugin_name: str,
    hook_name: str,
    name: str,
    binproviders: str,
    min_version: str,
    custom_cmd: str | None,
    overrides: str | None,
):
    """Install binary using Homebrew."""

    if binproviders != "*" and "brew" not in binproviders.split(","):
        click.echo(f"brew provider not allowed for {name}", err=True)
        sys.exit(0)

    # Use abx-pkg BrewProvider to install binary
    provider = BrewProvider()
    if not provider.INSTALLER_BIN:
        click.echo("brew not available on this system", err=True)
        sys.exit(1)

    click.echo(f"Resolving {name} via brew (load or install)...", err=True)

    try:
        # Parse overrides if provided
        overrides_dict: dict[str, HandlerDict] = {}
        if overrides:
            try:
                parsed_overrides = json.loads(overrides)
                if not isinstance(parsed_overrides, dict):
                    raise json.JSONDecodeError(
                        "overrides must be an object",
                        overrides,
                        0,
                    )
                overrides_dict = parsed_overrides
                click.echo(
                    f"Using custom install overrides: {overrides_dict}",
                    err=True,
                )
            except json.JSONDecodeError:
                click.echo(
                    f"Warning: Failed to parse overrides JSON: {overrides}",
                    err=True,
                )

        allowed_providers = (
            set(binproviders.split(",")) if binproviders != "*" else {"env", "brew"}
        )
        providers: list[BinProvider] = [provider]
        if "env" in allowed_providers:
            providers.insert(0, EnvProvider())

        brew_overrides: HandlerDict = {}
        if "brew" in overrides_dict:
            brew_overrides = overrides_dict["brew"]
        install_args = brew_overrides.get("install_args")
        if (
            isinstance(install_args, list)
            and install_args
            and "abspath" not in brew_overrides
        ):
            package_names, bin_dirs = get_package_bin_dirs(provider, install_args)
            if bin_dirs:
                search_paths = [str(path) for path in bin_dirs]
                abspath = shutil.which(name, path=":".join(search_paths))
                if abspath:
                    brew_overrides["abspath"] = abspath
                elif len(package_names) == 1:
                    # For keg-only formulae like openjdk, the binary may only exist
                    # at /opt/<formula>/bin/<name> after the install completes.
                    brew_overrides["abspath"] = str(bin_dirs[0] / name)

                if "abspath" in brew_overrides:
                    overrides_dict["brew"] = brew_overrides

        binary = Binary(
            name=name,
            min_version=SemVer(min_version) if min_version else None,
            binproviders=providers,
            overrides=overrides_dict,
        ).load_or_install()
    except Exception as e:
        click.echo(f"brew install failed: {e}", err=True)
        sys.exit(1)

    if not binary.abspath:
        click.echo(f"{name} not found after brew install", err=True)
        sys.exit(1)

    resolved_provider = getattr(binary, "binprovider", None)
    if isinstance(resolved_provider, str):
        resolved_provider_name = resolved_provider
    else:
        resolved_provider_name = getattr(resolved_provider, "name", "") or ""

    # Output Binary JSONL record to stdout
    emit_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version) if binary.version else "",
        sha256=binary.sha256 or "",
        binprovider=resolved_provider_name,
        machine_id=machine_id,
        binary_id=binary_id,
        plugin_name=plugin_name,
        hook_name=hook_name,
    )

    # Log human-readable info to stderr
    click.echo(f"Installed {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    sys.exit(0)


if __name__ == "__main__":
    main()
