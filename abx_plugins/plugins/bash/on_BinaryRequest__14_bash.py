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
# Install a binary using a bash shell command defined in overrides.
# This provider runs arbitrary shell commands to install binaries that don't fit
# into standard package managers, outputting a Binary JSONL record to stdout.
#
# Usage:
#     ./on_BinaryRequest__14_bash.py [...] > events.jsonl

import sys
import json

from abx_plugins.plugins.base.utils import (
    emit_installed_binary_record,
    parse_extra_hook_args,
)

import rich_click as click

from abxpkg import BashProvider, Binary


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
    if binproviders != "*" and "bash" not in allowed_providers:
        click.echo(f"bash provider not allowed for {name}", err=True)
        sys.exit(0)

    context = click.get_current_context(silent=True)
    extra_kwargs = parse_extra_hook_args(context.args if context else [])
    raw_overrides = json.loads(overrides)
    if not isinstance(raw_overrides, dict):
        click.echo(
            "bash provider requires overrides to decode to an object",
            err=True,
        )
        sys.exit(1)

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
            "bash provider requires overrides.bash.install",
            err=True,
        )
        sys.exit(1)

    try:
        binary = binary.install()
    except Exception as e:
        click.echo(f"bash install failed: {e}", err=True)
        sys.exit(1)

    if not binary.abspath:
        click.echo(f"{name} not found after bash install", err=True)
        sys.exit(1)

    # Output Binary JSONL record to stdout
    emit_installed_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version) if binary.version else "",
        sha256=binary.sha256 or "",
        binprovider="bash",
    )

    # Log human-readable info to stderr
    click.echo(f"Installed {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    sys.exit(0)


if __name__ == "__main__":
    main()
