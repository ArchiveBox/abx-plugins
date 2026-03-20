#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "rich-click",
#   "abx-pkg",
# ]
# ///
#
# Install a binary using Homebrew package manager and output a Binary JSONL record.
#
# Usage:
#     ./on_Binary__12_brew_install.py [...] > events.jsonl
#

import json
import shutil
import sys
from pathlib import Path

import rich_click as click
from abx_pkg import Binary, BrewProvider, EnvProvider


@click.command()
@click.option("--machine-id", required=True, help="Machine UUID")
@click.option("--binary-id", required=True, help="Dependency UUID")
@click.option("--name", required=True, help="Binary name to install")
@click.option("--binproviders", default="*", help="Allowed providers (comma-separated)")
@click.option("--custom-cmd", default=None, help="Custom install command")
@click.option("--overrides", default=None, help="JSON-encoded overrides dict")
def main(
    binary_id: str,
    machine_id: str,
    name: str,
    binproviders: str,
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
        overrides_dict = None
        if overrides:
            try:
                overrides_dict = json.loads(overrides)
                click.echo(
                    f"Using custom install overrides: {overrides_dict}", err=True
                )
            except json.JSONDecodeError:
                click.echo(
                    f"Warning: Failed to parse overrides JSON: {overrides}", err=True
                )

        allowed_providers = set(binproviders.split(",")) if binproviders != "*" else {"env", "brew"}
        providers = [provider]
        if "env" in allowed_providers:
            providers.insert(0, EnvProvider())

        brew_overrides = (overrides_dict or {}).get("brew", {})
        install_args = brew_overrides.get("install_args") or []
        if install_args and "abspath" not in brew_overrides:
            search_paths: list[str] = []
            for package in install_args:
                if not isinstance(package, str) or package.startswith("-"):
                    continue
                for bin_dir in provider.PATH.split(":"):
                    if not bin_dir.endswith("/bin"):
                        continue
                    prefix = Path(bin_dir).parent
                    search_paths.append(str(prefix / "opt" / package / "bin"))
                    search_paths.extend(str(path) for path in (prefix / "Cellar" / package).glob("*/bin"))
            if search_paths:
                abspath = shutil.which(name, path=":".join(search_paths))
                if abspath:
                    brew_overrides = {**brew_overrides, "abspath": abspath}
                    overrides_dict = {**(overrides_dict or {}), "brew": brew_overrides}

        binary = Binary(
            name=name,
            binproviders=providers,
            overrides=overrides_dict or {},
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
    record = {
        "type": "Binary",
        "name": name,
        "abspath": str(binary.abspath),
        "version": str(binary.version) if binary.version else "",
        "sha256": binary.sha256 or "",
        "binprovider": resolved_provider_name,
        "machine_id": machine_id,
        "binary_id": binary_id,
    }
    print(json.dumps(record))

    # Log human-readable info to stderr
    click.echo(f"Installed {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    sys.exit(0)


if __name__ == "__main__":
    main()
