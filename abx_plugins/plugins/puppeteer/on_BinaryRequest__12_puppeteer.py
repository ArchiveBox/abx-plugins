#!/usr/bin/env -S uv run --active --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic-settings",
#     "jambo",
#     "rich-click",
#     "abxpkg>=1.10.4",
#     "abx-plugins>=1.10.27",
# ]
# ///
"""
Install Chrome for Testing via the Puppeteer CLI.

Usage: on_BinaryRequest__12_puppeteer.py --name=<name>
Output: Binary JSONL record to stdout after installation
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import rich_click as click
from abxpkg import Binary, EnvProvider, PuppeteerProvider

from abx_plugins.plugins.base.utils import (
    emit_installed_binary_record,
    load_config,
    parse_extra_hook_args,
)


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--name", required=True, help="Binary name to install")
@click.option("--binproviders", default="*", help="Allowed providers (comma-separated)")
@click.option("--overrides", default=None, help="JSON-encoded overrides dict")
def main(
    name: str,
    binproviders: str,
    overrides: str | None,
) -> None:
    config = load_config()

    if binproviders != "*" and "puppeteer" not in binproviders.split(","):
        sys.exit(0)

    if name not in ("chrome",):
        sys.exit(0)

    existing_chrome_binary = (config.CHROME_BINARY or "").strip()
    if existing_chrome_binary and _is_explicit_path(existing_chrome_binary):
        existing_binary = _load_binary_from_path(existing_chrome_binary, name=name)
        if existing_binary and existing_binary.abspath:
            _emit_browser_binary_record(binary=existing_binary, name=name)
            sys.exit(0)

    lib_dir = (config.LIB_DIR or "").strip()
    if not lib_dir:
        lib_dir = str(Path.home() / ".config" / "abx" / "lib")
    os.environ.setdefault("ABXPKG_LIB_DIR", str(Path(lib_dir).expanduser().resolve()))
    provider = PuppeteerProvider()

    raw_overrides = json.loads(overrides) if overrides else {}
    if not isinstance(raw_overrides, dict):
        click.echo("puppeteer overrides must decode to an object", err=True)
        sys.exit(1)

    provider_overrides = raw_overrides.get("puppeteer")
    default_install_args = ["chrome@stable", "--install-deps"]
    if provider_overrides is None:
        raw_overrides = {
            **raw_overrides,
            "puppeteer": {"install_args": default_install_args},
        }
    elif (
        isinstance(provider_overrides, dict)
        and "install_args" not in provider_overrides
    ):
        raw_overrides = {
            **raw_overrides,
            "puppeteer": {
                **provider_overrides,
                "install_args": default_install_args,
            },
        }

    context = click.get_current_context(silent=True)
    extra_kwargs = parse_extra_hook_args(context.args if context else [])

    try:
        binary = Binary.model_validate(
            {
                **extra_kwargs,
                "name": name,
                "binproviders": [provider],
                "overrides": raw_overrides,
            },
        ).install()
    except Exception as e:
        error_output = str(e)
        click.echo(f"puppeteer install failed: {error_output}", err=True)
        sys.exit(1)

    if not binary.abspath:
        click.echo("ERROR: failed to locate browser after install", err=True)
        sys.exit(1)

    _emit_browser_binary_record(
        binary=binary,
        name=name,
    )

    sys.exit(0)


def _emit_browser_binary_record(
    binary: Binary,
    name: str,
) -> None:
    emit_installed_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version) if binary.version else "",
        sha256=binary.sha256 or "",
        binprovider="puppeteer",
    )


def _load_binary_from_path(path: str, name: str) -> Binary | None:
    raw_path = str(path or "").strip()
    if not raw_path or not _is_explicit_path(raw_path):
        return None
    path_obj = Path(raw_path).expanduser()
    overrides = {"env": {"abspath": str(path_obj)}}
    try:
        binary = Binary.model_validate(
            {
                "name": path_obj.name,
                "binproviders": [EnvProvider()],
                "overrides": overrides,
            },
        ).load()
    except Exception:
        return None
    if binary and binary.abspath and _is_supported_chromium_binary(binary.abspath):
        return binary
    return None


def _is_supported_chromium_binary(path: str | Path) -> bool:
    try:
        proc = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    version = f"{proc.stdout}\n{proc.stderr}".strip()
    if not version:
        return False
    if (
        version.startswith("Google Chrome ")
        and "Chrome for Testing" not in version
        and "Chrome Canary" not in version
    ):
        return False
    return any(
        name in version
        for name in (
            "Chromium",
            "Chrome for Testing",
            "Chrome Canary",
            "HeadlessChrome",
            "Chrome Headless Shell",
        )
    )


def _is_explicit_path(value: str) -> bool:
    raw_value = str(value or "").strip()
    return (
        raw_value.startswith(("~", ".", "/")) or "/" in raw_value or "\\" in raw_value
    )


if __name__ == "__main__":
    main()
