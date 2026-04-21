#!/usr/bin/env -S uv run --active --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "rich-click",
#   "abxpkg>=1.10.4",
#   "abx-plugins>=1.10.27",
# ]
# ///
#
# Install a binary using Homebrew package manager and output a Binary JSONL record.
#
# Usage:
#     ./on_BinaryRequest__12_brew.py [...] > events.jsonl
#

from __future__ import annotations

import json
import sys
import rich_click as click
from abxpkg import Binary, BrewProvider

from abx_plugins.plugins.base.utils import (
    emit_installed_binary_record,
    parse_extra_hook_args,
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
    """Install binary using Homebrew."""

    if binproviders != "*" and "brew" not in binproviders.split(","):
        click.echo(f"brew provider not allowed for {name}", err=True)
        sys.exit(0)

    # Use abxpkg BrewProvider to install binary
    provider = BrewProvider()
    try:
        provider.INSTALLER_BINARY()
    except Exception:
        click.echo("brew not available on this system", err=True)
        sys.exit(0)

    click.echo(f"Resolving {name} via brew (load or install)...", err=True)

    try:
        context = click.get_current_context(silent=True)
        extra_kwargs = parse_extra_hook_args(context.args if context else [])
        binary = Binary.model_validate(
            {
                **extra_kwargs,
                "name": name,
                "binproviders": [provider],
                "min_version": min_version or extra_kwargs.get("min_version") or None,
                "overrides": json.loads(overrides) if overrides else {},
            },
        )
        if binary.overrides:
            click.echo(
                f"Using custom install overrides: {binary.overrides}",
                err=True,
            )

        binary = binary.install()
    except Exception as e:
        click.echo(f"brew install failed: {e}", err=True)
        sys.exit(1)

    if not binary.abspath:
        click.echo(f"{name} not found after brew install", err=True)
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
