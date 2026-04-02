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
# Install a binary using Cargo and output a Binary JSONL record.
#
# Usage:
#     ./on_BinaryRequest__12_cargo.py [...] > events.jsonl
#

from __future__ import annotations

import json
import sys

import rich_click as click
from abx_pkg import Binary, CargoProvider

from abx_plugins.plugins.base.utils import (
    emit_installed_binary_record,
)


def _parse_extra_hook_args(args: list[str]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for arg in args:
        if not arg.startswith("--") or "=" not in arg:
            continue
        key, raw_value = arg[2:].split("=", 1)
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        parsed[key.replace("-", "_")] = value
    return parsed


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

    provider = CargoProvider(postinstall_scripts=True, min_release_age=0)
    if not provider.INSTALLER_BIN_ABSPATH:
        click.echo("cargo not available on this system", err=True)
        sys.exit(0)

    click.echo(f"Resolving {name} via cargo (load or install)...", err=True)

    try:
        extra_kwargs = _parse_extra_hook_args(click.get_current_context().args)
        overrides_dict = json.loads(overrides) if overrides else {}
        if overrides_dict:
            click.echo(
                f"Using custom install overrides: {overrides_dict}",
                err=True,
            )

        request_kwargs = {
            **extra_kwargs,
            "name": name,
            "binproviders": binproviders,
            "min_version": min_version or None,
            "overrides": overrides_dict,
        }
        binary = Binary(
            **{**request_kwargs, "binproviders": [provider]},
        ).load_or_install()
    except Exception as e:
        click.echo(f"cargo install failed: {e}", err=True)
        sys.exit(1)

    if not binary.abspath:
        click.echo(f"{name} not found after cargo install", err=True)
        sys.exit(1)

    resolved_provider = binary.loaded_binprovider
    resolved_provider_name = resolved_provider.name if resolved_provider else ""

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
