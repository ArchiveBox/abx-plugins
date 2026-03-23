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
# Install a binary using Cargo and output a Binary JSONL record.
#
# Usage:
#     ./on_BinaryRequest__12_cargo.py [...] > events.jsonl
#

from __future__ import annotations

import json
import sys

import rich_click as click
from abx_pkg import Binary, CargoProvider, SemVer
from abx_pkg.binprovider import HandlerDict

from abx_plugins.plugins.base.utils import emit_installed_binary_record


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
    """Install binary using Cargo."""

    if binproviders != "*" and "cargo" not in binproviders.split(","):
        click.echo(f"cargo provider not allowed for {name}", err=True)
        sys.exit(0)

    provider = CargoProvider()
    if not provider.INSTALLER_BIN_ABSPATH:
        click.echo("cargo not available on this system", err=True)
        sys.exit(1)

    click.echo(f"Resolving {name} via cargo (load or install)...", err=True)

    try:
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

        binary = Binary(
            name=name,
            min_version=SemVer(min_version) if min_version else None,
            binproviders=[provider],
            overrides=overrides_dict,
        ).load_or_install()
    except Exception as e:
        click.echo(f"cargo install failed: {e}", err=True)
        sys.exit(1)

    if not binary.abspath:
        click.echo(f"{name} not found after cargo install", err=True)
        sys.exit(1)

    resolved_provider = getattr(binary, "binprovider", None)
    if isinstance(resolved_provider, str):
        resolved_provider_name = resolved_provider
    else:
        resolved_provider_name = getattr(resolved_provider, "name", "") or ""

    emit_installed_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version) if binary.version else "",
        sha256=binary.sha256 or "",
        binprovider=resolved_provider_name,
    )

    click.echo(f"Installed {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    sys.exit(0)


if __name__ == "__main__":
    main()
