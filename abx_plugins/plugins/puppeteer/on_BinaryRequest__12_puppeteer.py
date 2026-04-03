#!/usr/bin/env -S uv run --active --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic-settings",
#     "jambo",
#     "rich-click",
#     "abx-pkg>=1.9.27",
#     "abx-plugins>=1.10.27",
# ]
# ///
"""
Install Chromium via the Puppeteer CLI.

Usage: on_BinaryRequest__12_puppeteer.py --name=<name>
Output: Binary JSONL record to stdout after installation
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import rich_click as click
from abx_pkg import Binary, EnvProvider, PuppeteerProvider

from abx_plugins.plugins.base.utils import (
    emit_installed_binary_record,
    load_config,
    parse_extra_hook_args,
)

CLAUDE_SANDBOX_NO_PROXY = (
    "localhost,127.0.0.1,169.254.169.254,metadata.google.internal,"
    ".svc.cluster.local,.local"
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

    if name not in ("chromium", "chrome"):
        sys.exit(0)

    existing_chrome_binary = (config.CHROME_BINARY or "").strip()
    if existing_chrome_binary:
        existing_binary = _load_binary_from_path(existing_chrome_binary, name=name)
        if existing_binary and existing_binary.abspath:
            _emit_browser_binary_record(binary=existing_binary, name=name)
            sys.exit(0)

    lib_dir = (config.LIB_DIR or "").strip()
    if not lib_dir:
        lib_dir = str(Path.home() / ".config" / "abx" / "lib")

    configured_cache_dir = (config.PUPPETEER_CACHE_DIR or "").strip()
    if configured_cache_dir:
        browser_cache_dir = Path(configured_cache_dir).expanduser().resolve()
        browser_cache_dir.mkdir(parents=True, exist_ok=True)
        provider = PuppeteerProvider(
            browser_cache_dir=browser_cache_dir,
            browser_bin_dir=browser_cache_dir.parent / "bin",
        )
    else:
        puppeteer_root = (Path(lib_dir) / "puppeteer").resolve()
        puppeteer_root.mkdir(parents=True, exist_ok=True)
        provider = PuppeteerProvider(puppeteer_root=puppeteer_root)

    raw_overrides = json.loads(overrides) if overrides else {}
    if not isinstance(raw_overrides, dict):
        click.echo("puppeteer overrides must decode to an object", err=True)
        sys.exit(1)

    provider_overrides = raw_overrides.get("puppeteer")
    default_install_args = [f"{name}@latest", "--install-deps"]
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
        ).load_or_install()
    except Exception as e:
        error_output = str(e)
        hint = _get_install_failure_hint(error_output)
        if hint:
            click.echo(hint, err=True)
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


def _get_install_failure_hint(install_output: str) -> str | None:
    output = install_output or ""
    lowered = output.lower()
    if (
        "storage.googleapis.com" in lowered
        and "getaddrinfo" in lowered
        and "eai_again" in lowered
    ):
        return (
            "HINT: Puppeteer failed to download Chromium from storage.googleapis.com.\n"
            "HINT: In Claude sandboxes, NO_PROXY often includes *.googleapis.com "
            "and *.google.com. @puppeteer/browsers respects NO_PROXY, bypasses the "
            "egress proxy for storage.googleapis.com, and the direct connection can "
            "time out or fail DNS resolution.\n"
            "HINT: Override NO_PROXY, no_proxy, and any tool-specific no-proxy env "
            "vars to remove .googleapis.com and .google.com before retrying.\n"
            f'HINT: NO_PROXY="{CLAUDE_SANDBOX_NO_PROXY}"\n'
            'HINT: no_proxy="$NO_PROXY"'
        )
    return None


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
    if not raw_path:
        return None
    path_obj = Path(raw_path).expanduser()
    overrides = (
        {"env": {"abspath": str(path_obj)}}
        if raw_path.startswith(("~", ".", "/")) or "/" in raw_path or "\\" in raw_path
        else {}
    )
    try:
        binary = Binary.model_validate(
            {
                "name": path_obj.name if overrides else (name or raw_path),
                "binproviders": [EnvProvider()],
                "overrides": overrides,
            },
        ).load()
    except Exception:
        return None
    if binary and binary.abspath:
        return binary
    return None


if __name__ == "__main__":
    main()
