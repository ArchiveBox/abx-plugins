#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "rich-click",
#   "abx-pkg",
# ]
# ///
#
# Install a binary using apt package manager. Outputs a Binary JSONL record to stdout after installation.
#
# Usage:
#     ./on_Binary__13_apt_install.py [...] > events.jsonl

import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from base.utils import emit_binary_record

import rich_click as click
from abx_pkg import AptProvider, Binary, EnvProvider, SemVer


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
    """Install binary using apt package manager."""

    # Check if apt provider is allowed
    if binproviders != "*" and "apt" not in binproviders.split(","):
        click.echo(f"apt provider not allowed for {name}", err=True)
        sys.exit(0)  # Not an error, just skip

    # Use abx-pkg AptProvider to install binary
    provider = AptProvider()
    if not provider.INSTALLER_BIN:
        click.echo("apt not available on this system", err=True)
        sys.exit(1)

    click.echo(f"Resolving {name} via apt (load or install)...", err=True)

    try:
        # Parse overrides if provided
        overrides_dict = None
        if overrides:
            try:
                overrides_dict = json.loads(overrides)
                # Extract apt-specific overrides
                overrides_dict = overrides_dict.get("apt", {})
                click.echo(f"Using apt install overrides: {overrides_dict}", err=True)
            except json.JSONDecodeError:
                click.echo(
                    f"Warning: Failed to parse overrides JSON: {overrides}", err=True
                )

        # Prefer already-installed binaries found in PATH, then fall back to apt install.
        binary = Binary(
            name=name,
            min_version=SemVer(min_version) if min_version else None,
            binproviders=[EnvProvider(), provider],
            overrides={"apt": overrides_dict} if overrides_dict else {},
        ).load_or_install()
    except Exception as e:
        click.echo(f"apt install failed: {e}", err=True)
        sys.exit(1)

    if not binary.abspath:
        click.echo(f"{name} not found after apt install", err=True)
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
