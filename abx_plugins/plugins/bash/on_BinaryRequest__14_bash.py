#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "rich-click",
#   "abx-pkg>=1.9.27",
#   "abx-plugins>=1.10.27",
# ]
# ///
#
# Install a binary using a custom shell command defined in overrides.
# This provider runs arbitrary shell commands to install binaries that don't fit
# into standard package managers, outputting a Binary JSONL record to stdout.
#
# Usage:
#     ./on_BinaryRequest__14_custom.py [...] > events.jsonl

import sys
import json

from abx_plugins.plugins.base.utils import (
    emit_installed_binary_record,
    parse_extra_hook_args,
)

import rich_click as click

from abx_pkg import BashProvider, Binary


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--name", required=True, help="Binary name to install")
@click.option("--binproviders", default="*", help="Allowed providers (comma-separated)")
@click.option("--min-version", default="", help="Minimum acceptable version")
@click.option("--overrides", required=True, help="JSON-encoded overrides dict")
def main(
    name: str,
    binproviders: str,
    min_version: str,
    overrides: str,
):
    """Install binary using bash shell commands defined in overrides."""

    allowed_providers = {provider.strip() for provider in binproviders.split(",")}
    if (
        binproviders != "*"
        and "custom" not in allowed_providers
        and "bash" not in allowed_providers
    ):
        click.echo(f"custom provider not allowed for {name}", err=True)
        sys.exit(0)

    context = click.get_current_context(silent=True)
    extra_kwargs = parse_extra_hook_args(context.args if context else [])
    raw_overrides = json.loads(overrides)
    if not isinstance(raw_overrides, dict):
        click.echo(
            "custom provider requires overrides to decode to an object",
            err=True,
        )
        sys.exit(1)
    if "bash" not in raw_overrides and "custom" in raw_overrides:
        raw_overrides = {**raw_overrides, "bash": raw_overrides["custom"]}

    provider = BashProvider()
    binary = Binary.model_validate(
        {
            **extra_kwargs,
            "name": name,
            "binproviders": [provider],
            "min_version": min_version or extra_kwargs.get("min_version") or None,
            "overrides": raw_overrides,
        },
    )
    bash_overrides = binary.overrides.get("bash", {})
    if "install" not in bash_overrides:
        click.echo(
            "custom provider requires overrides.custom.install or overrides.bash.install",
            err=True,
        )
        sys.exit(1)

    try:
        binary = binary.load_or_install()
    except Exception as e:
        click.echo(f"custom install failed: {e}", err=True)
        sys.exit(1)

    if not binary.abspath:
        click.echo(f"{name} not found after custom install", err=True)
        sys.exit(1)

    # Output Binary JSONL record to stdout
    emit_installed_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version) if binary.version else "",
        sha256=binary.sha256 or "",
        binprovider="custom",
    )

    # Log human-readable info to stderr
    click.echo(f"Installed {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    sys.exit(0)


if __name__ == "__main__":
    main()
