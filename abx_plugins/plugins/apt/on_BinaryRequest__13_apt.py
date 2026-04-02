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
import json

from abx_plugins.plugins.base.utils import (
    emit_installed_binary_record,
)

import rich_click as click

from abx_pkg import AptProvider, Binary


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
    """Install binary using apt package manager."""

    # Check if apt provider is allowed
    if binproviders != "*" and "apt" not in binproviders.split(","):
        click.echo(f"apt provider not allowed for {name}", err=True)
        sys.exit(0)  # Not an error, just skip

    # Use abx-pkg AptProvider to install binary
    provider = AptProvider(postinstall_scripts=True, min_release_age=0)
    if not provider.INSTALLER_BIN_ABSPATH:
        click.echo(
            "AptProvider.INSTALLER_BIN is not available on this host",
            err=True,
        )
        sys.exit(0)

    click.echo(f"Resolving {name} via apt (load or install)...", err=True)

    try:
        ctx = click.get_current_context(silent=True)
        extra_kwargs = _parse_extra_hook_args(ctx.args if ctx else [])
        overrides_dict = json.loads(overrides) if overrides else {}
        provider_overrides = overrides_dict.get("apt", {})
        if provider_overrides:
            click.echo(
                f"Using apt install overrides: {provider_overrides}",
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
            **{**request_kwargs, "binproviders": [provider]},  # ty:ignore[invalid-argument-type]
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
