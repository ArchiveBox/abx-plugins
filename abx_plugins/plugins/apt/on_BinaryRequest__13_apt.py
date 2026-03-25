#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "rich-click",
#   "abx-pkg",
#   "abx-plugins",
# ]
# ///
#
# Install a binary using apt package manager. Outputs a Binary JSONL record to stdout after installation.
#
# Usage:
#     ./on_BinaryRequest__13_apt.py [...] > events.jsonl

import sys

from abx_plugins.plugins.base.utils import emit_installed_binary_record

import rich_click as click
from pydantic import ConfigDict, TypeAdapter

from abx_pkg import AptProvider, Binary, BinaryOverrides, HandlerDict, SemVer


OverridesDict = TypeAdapter(
    BinaryOverrides,
    config=ConfigDict(arbitrary_types_allowed=True),
)


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
    """Install binary using apt package manager."""

    # Check if apt provider is allowed
    if binproviders != "*" and "apt" not in binproviders.split(","):
        click.echo(f"apt provider not allowed for {name}", err=True)
        sys.exit(0)  # Not an error, just skip

    # Use abx-pkg AptProvider to install binary
    provider = AptProvider()
    if not provider.INSTALLER_BIN:
        click.echo("apt not available on this system", err=True)
        sys.exit(0)

    click.echo(f"Resolving {name} via apt (load or install)...", err=True)

    try:
        # Parse overrides if provided
        provider_overrides: HandlerDict = {}
        if overrides:
            parsed_overrides = OverridesDict.validate_json(overrides)
            if "apt" in parsed_overrides:
                provider_overrides = parsed_overrides["apt"]
            click.echo(
                f"Using apt install overrides: {provider_overrides}",
                err=True,
            )

        binary = Binary(
            name=name,
            min_version=SemVer(min_version) if min_version else None,
            binproviders=[provider],
            overrides={"apt": provider_overrides} if provider_overrides else {},
        ).load_or_install()
    except Exception as e:
        click.echo(f"apt install failed: {e}", err=True)
        sys.exit(1)

    if not binary.abspath:
        click.echo(f"{name} not found after apt install", err=True)
        sys.exit(1)

    resolved_provider = binary.loaded_binprovider
    resolved_provider_name = resolved_provider.name if resolved_provider else ""

    # Output Binary JSONL record to stdout
    emit_installed_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version) if binary.version else "",
        sha256=binary.sha256 or "",
        binprovider=resolved_provider_name,
    )

    # Log human-readable info to stderr
    click.echo(f"Installed {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    sys.exit(0)


if __name__ == "__main__":
    main()
