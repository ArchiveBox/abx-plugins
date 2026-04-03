#!/usr/bin/env -S uv run --active --script
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
"""Install Chrome Web Store extensions as binary-like artifacts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import rich_click as click

from abx_pkg import Binary, ChromeWebstoreProvider

from abx_plugins.plugins.base.utils import (
    emit_installed_binary_record,
    load_config,
    parse_extra_hook_args,
)


def _extensions_dir() -> Path:
    config = load_config()
    if config.CHROME_EXTENSIONS_DIR:
        return Path(config.CHROME_EXTENSIONS_DIR).expanduser().resolve()
    return (
        Path(config.PERSONAS_DIR).expanduser()
        / config.ACTIVE_PERSONA
        / "chrome_extensions"
    ).resolve()


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--name", required=True, help="Chrome Web Store extension ID")
@click.option("--binproviders", default="*", help="Allowed providers (comma-separated)")
@click.option("--min-version", default="", help="Minimum acceptable version")
@click.option("--overrides", default=None, help="JSON-encoded overrides dict")
def main(
    name: str,
    binproviders: str,
    min_version: str,
    overrides: str | None,
) -> None:
    if binproviders != "*" and "chromewebstore" not in [
        provider.strip() for provider in binproviders.split(",")
    ]:
        sys.exit(0)

    provider = ChromeWebstoreProvider(extensions_dir=_extensions_dir())
    if not provider.is_valid:
        click.echo("chromewebstore provider is not available on this host", err=True)
        sys.exit(0)

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
    ).load_or_install()

    if not binary.abspath:
        click.echo(f"{name} not resolved as Chrome Web Store extension", err=True)
        sys.exit(1)

    emit_installed_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version or ""),
        sha256=str(binary.sha256 or ""),
        binprovider="chromewebstore",
    )

    click.echo(f"Resolved extension {name} -> {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
