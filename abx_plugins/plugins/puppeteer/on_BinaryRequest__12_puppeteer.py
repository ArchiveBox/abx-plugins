#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic-settings",
#     "rich-click",
#     "abx-pkg",
#     "abx-plugins",
# ]
# [tool.uv.sources]
# abx-plugins = { path = "../../..", editable = true }
# ///
"""
Install Chromium via the Puppeteer CLI.

Usage: on_BinaryRequest__12_puppeteer.py --name=<name>
Output: Binary JSONL record to stdout after installation
"""

import json
import os
import re
import shutil
import sys
from pathlib import Path

import rich_click as click
from abx_pkg.semver import bin_version
from abx_pkg import Binary, EnvProvider, NpmProvider

from abx_plugins.plugins.base.utils import (
    emit_installed_binary_record,
    emit_machine_record,
    load_config,
    resolve_binary_path,
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

    lib_dir = (config.LIB_DIR or "").strip()
    if not lib_dir:
        lib_dir = str(Path.home() / ".config" / "abx" / "lib")

    npm_prefix = Path(lib_dir) / "npm"
    npm_prefix.mkdir(parents=True, exist_ok=True)
    npm_provider = NpmProvider(npm_prefix=npm_prefix)
    cache_dir = Path(lib_dir) / "puppeteer" / "chrome"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PUPPETEER_CACHE_DIR", str(cache_dir))

    # Fast-path: if CHROME_BINARY is already available in env, reuse it and avoid
    # a full `puppeteer browsers install` call for this invocation.
    existing_chrome_binary = (config.CHROME_BINARY or "").strip()
    if existing_chrome_binary:
        existing_binary = _load_binary_from_path(existing_chrome_binary, name=name)
        if existing_binary and existing_binary.abspath:
            _emit_browser_binary_record(
                binary=existing_binary,
                name=name,
            )
            _emit_browser_machine_config(existing_binary)
            sys.exit(0)

    puppeteer_binary = Binary(
        name="puppeteer",
        binproviders=[npm_provider],
        overrides={"npm": {"install_args": ["puppeteer"]}},
    ).load_or_install()

    if not puppeteer_binary.abspath:
        click.echo(
            "ERROR: puppeteer binary not found (install puppeteer first)",
            err=True,
        )
        sys.exit(1)

    install_args = _parse_override_install_args(
        overrides,
        default=[f"{name}@latest", "--install-deps"],
    )
    proc = _run_puppeteer_install(
        binary=puppeteer_binary,
        install_args=install_args,
        cache_dir=cache_dir,
    )
    if proc.returncode != 0 and _should_repair_puppeteer_install(
        proc.stdout + "\n" + proc.stderr,
    ):
        click.echo("Detected broken puppeteer CLI, reinstalling package...", err=True)
        npm_provider.install("puppeteer")
        puppeteer_binary = Binary(
            name="puppeteer",
            binproviders=[npm_provider],
            overrides={"npm": {"install_args": ["puppeteer"]}},
        ).load()
        proc = _run_puppeteer_install(
            binary=puppeteer_binary,
            install_args=install_args,
            cache_dir=cache_dir,
        )
    if proc.returncode != 0:
        click.echo(proc.stdout.strip(), err=True)
        click.echo(proc.stderr.strip(), err=True)
        install_hint = _get_install_failure_hint(proc.stdout + "\n" + proc.stderr)
        if install_hint:
            click.echo(install_hint, err=True)
        click.echo(f"ERROR: puppeteer install failed ({proc.returncode})", err=True)
        sys.exit(1)

    chromium_binary = _load_browser_binary(proc.stdout + "\n" + proc.stderr, name=name)
    if not chromium_binary or not chromium_binary.abspath:
        click.echo("ERROR: failed to locate Chromium after install", err=True)
        sys.exit(1)

    _emit_browser_binary_record(
        binary=chromium_binary,
        name=name,
    )

    _emit_browser_machine_config(chromium_binary)

    sys.exit(0)


def _parse_override_install_args(
    overrides: str | None,
    default: list[str],
) -> list[str]:
    if not overrides:
        return default
    try:
        overrides_dict = json.loads(overrides)
    except json.JSONDecodeError:
        return default

    if isinstance(overrides_dict, dict):
        provider_overrides = overrides_dict.get("puppeteer")
        if isinstance(provider_overrides, dict):
            install_args = provider_overrides.get("install_args")
            if isinstance(install_args, list) and install_args:
                return [str(arg) for arg in install_args]
        if isinstance(provider_overrides, list) and provider_overrides:
            return [str(arg) for arg in provider_overrides]
    if isinstance(overrides_dict, list) and overrides_dict:
        return [str(arg) for arg in overrides_dict]

    return default


def _run_puppeteer_install(binary: Binary, install_args: list[str], cache_dir: Path):
    cmd = ["browsers", "install", *install_args]
    proc = binary.exec(cmd=cmd, timeout=300)
    if proc.returncode == 0:
        return proc

    install_output = f"{proc.stdout}\n{proc.stderr}"

    # If --install-deps failed because we're not root, retry with sudo
    if (
        "--install-deps" in install_args
        and "requires root privileges" in install_output
        and os.geteuid() != 0
        and resolve_binary_path("sudo")
    ):
        sudo_proc = _run_puppeteer_install_with_sudo(binary, install_args, cache_dir)
        if sudo_proc is not None and sudo_proc.returncode == 0:
            return sudo_proc
        if sudo_proc is not None:
            install_output = f"{sudo_proc.stdout}\n{sudo_proc.stderr}"
            proc = sudo_proc

    if not _cleanup_partial_chromium_cache(install_output, cache_dir):
        return proc

    return binary.exec(cmd=cmd, timeout=300)


def _run_puppeteer_install_with_sudo(
    binary: Binary,
    install_args: list[str],
    cache_dir: Path,
):
    """Re-run puppeteer install via sudo so --install-deps can install system libs."""
    import subprocess as _subprocess

    abspath = str(binary.abspath or resolve_binary_path("puppeteer") or "")
    if not abspath:
        return None

    sudo_cmd = [
        "sudo",
        "-E",
        abspath,
        "browsers",
        "install",
        *install_args,
    ]
    env = os.environ.copy()
    env.setdefault("PUPPETEER_CACHE_DIR", str(cache_dir))
    proc = _subprocess.run(
        sudo_cmd,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )

    # Fix ownership: sudo may have written root-owned files into the
    # normal user's cache dir, which would break later non-root operations.
    if proc.returncode == 0 and cache_dir.exists():
        uid = os.getuid()
        gid = os.getgid()
        _subprocess.run(
            ["sudo", "chown", "-R", f"{uid}:{gid}", str(cache_dir)],
            timeout=30,
        )

    return proc


def _cleanup_partial_chromium_cache(install_output: str, cache_dir: Path) -> bool:
    targets: set[Path] = set()
    chromium_cache_dir = cache_dir / "chromium"

    missing_dir_match = re.search(
        r"browser folder \(([^)]+)\) exists but the executable",
        install_output,
    )
    if missing_dir_match:
        targets.add(Path(missing_dir_match.group(1)))

    missing_zip_match = re.search(r"open '([^']+\.zip)'", install_output)
    if missing_zip_match:
        targets.add(Path(missing_zip_match.group(1)))

    build_id_match = re.search(
        r"All providers failed for chromium (\d+)",
        install_output,
    )
    if build_id_match and chromium_cache_dir.exists():
        build_id = build_id_match.group(1)
        targets.update(chromium_cache_dir.glob(f"*{build_id}*"))

    removed_any = False
    for target in targets:
        resolved_target = target.resolve(strict=False)
        resolved_cache = cache_dir.resolve(strict=False)
        if not (
            resolved_target == resolved_cache
            or resolved_cache in resolved_target.parents
        ):
            continue
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed_any = True
            continue
        if target.exists():
            target.unlink(missing_ok=True)
            removed_any = True

    return removed_any


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


def _should_repair_puppeteer_install(output: str) -> bool:
    lowered = (output or "").lower()
    return (
        "this.shim.parser.camelcase is not a function" in lowered
        or "yargs/build/lib/command.js" in lowered
    )


def _emit_browser_binary_record(
    binary: Binary,
    name: str,
) -> None:
    version = str(binary.version) if binary.version else ""
    if not version and binary.abspath:
        try:
            detected_version = bin_version(binary.abspath)
        except Exception:
            detected_version = None
        if detected_version:
            version = str(detected_version)
    emit_installed_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=version,
        sha256=binary.sha256 or "",
        binprovider="puppeteer",
    )


def _emit_browser_machine_config(binary: Binary) -> None:
    # Persist stable runtime config only. Browser version metadata already
    # lives on the Binary record and should not be promoted into Machine.config.
    emit_machine_record(
        {
            "CHROME_BINARY": str(binary.abspath),
        },
    )


def _resolve_binary_reference(binary_ref: str) -> str | None:
    resolved_ref = resolve_binary_path(binary_ref)
    if resolved_ref:
        return resolved_ref

    path_obj = Path(binary_ref).expanduser()
    if path_obj.is_absolute() or path_obj.parent != Path("."):
        return str(path_obj.resolve(strict=False))

    return None


def _load_binary_from_path(path: str, name: str) -> Binary | None:
    resolved_path = _resolve_binary_reference(path)
    if not resolved_path:
        return None
    try:
        binary = Binary(
            name=name,
            binproviders=[EnvProvider()],
            overrides={"env": {"abspath": resolved_path}},
        ).load()
    except Exception:
        return None
    if binary and binary.abspath:
        return binary
    return None


def _load_browser_binary(output: str, name: str) -> Binary | None:
    candidates: list[Path] = []
    match = re.search(r"(?:chromium|chrome)@[^\s]+\s+(\S+)", output)
    if match:
        candidates.append(Path(match.group(1)))

    cache_dirs: list[Path] = []
    cache_env = load_config().PUPPETEER_CACHE_DIR
    if cache_env:
        cache_dirs.append(Path(cache_env))

    home = Path.home()
    cache_dirs.extend(
        [
            home / ".cache" / "puppeteer",
            home / "Library" / "Caches" / "puppeteer",
        ],
    )

    for base in cache_dirs:
        for root in (base, base / name):
            try:
                candidates.extend(root.rglob("Chromium.app/Contents/MacOS/Chromium"))
            except Exception:
                pass
            try:
                candidates.extend(
                    root.rglob(
                        "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                    ),
                )
            except Exception:
                pass
            try:
                candidates.extend(root.rglob("chrome"))
            except Exception:
                pass

    for candidate in candidates:
        try:
            binary = Binary(
                name=name,
                binproviders=[EnvProvider()],
                overrides={"env": {"abspath": str(candidate)}},
            ).load()
        except Exception:
            continue
        if binary.abspath:
            return binary

    return None


if __name__ == "__main__":
    main()
