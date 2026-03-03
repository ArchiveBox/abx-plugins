#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "click",
# ]
# ///
"""Extract article content using trafilatura from local HTML snapshots."""

import json
import os
import subprocess
import sys
from pathlib import Path

import click

PLUGIN_DIR = Path(__file__).resolve().parent.name
SNAP_DIR = Path(os.environ.get("SNAP_DIR", ".")).resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)

FORMAT_TO_FILE = {
    "txt": "content.txt",
    "markdown": "content.md",
    "html": "content.html",
    "csv": "content.csv",
    "json": "content.json",
    "xml": "content.xml",
    "xmltei": "content.xmltei",
}


def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def get_env_bool(name: str, default: bool = False) -> bool:
    val = get_env(name, "").lower()
    if val in ("true", "1", "yes", "on"):
        return True
    if val in ("false", "0", "no", "off"):
        return False
    return default


def get_env_int(name: str, default: int = 0) -> int:
    try:
        return int(get_env(name, str(default)))
    except ValueError:
        return default


def get_env_array(name: str, default: list[str] | None = None) -> list[str]:
    val = get_env(name, "")
    if not val:
        return default if default is not None else []
    try:
        result = json.loads(val)
        if isinstance(result, list):
            return [str(item) for item in result]
    except json.JSONDecodeError:
        pass
    return default if default is not None else []


def find_html_source() -> str | None:
    search_patterns = [
        "singlefile/singlefile.html",
        "*_singlefile/singlefile.html",
        "singlefile/*.html",
        "*_singlefile/*.html",
        "dom/output.html",
        "*_dom/output.html",
        "dom/*.html",
        "*_dom/*.html",
        "wget/**/*.html",
        "*_wget/**/*.html",
        "wget/**/*.htm",
        "*_wget/**/*.htm",
    ]

    cwd = Path.cwd()
    for base in (cwd, cwd.parent):
        for pattern in search_patterns:
            for match in base.glob(pattern):
                if match.is_file() and match.stat().st_size > 0:
                    return str(match)
    return None


def get_enabled_formats() -> list[str]:
    return [
        fmt
        for fmt in FORMAT_TO_FILE
        if get_env_bool(f"TRAFILATURA_OUTPUT_{fmt.upper()}", fmt in {"txt", "markdown", "html"})
    ]


def run_trafilatura(binary: str, html_source: str, fmt: str, timeout: int) -> tuple[bool, str]:
    args = get_env_array("TRAFILATURA_ARGS", [])
    args_extra = get_env_array("TRAFILATURA_ARGS_EXTRA", [])
    cmd = [
        binary,
        *args,
        *args_extra,
        "--input-file",
        html_source,
        "--output-format",
        fmt,
    ]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    if result.stderr:
        sys.stderr.write(result.stderr)
        sys.stderr.flush()
    if result.returncode != 0:
        return False, f"trafilatura failed for format={fmt} (exit={result.returncode})"

    (OUTPUT_DIR / FORMAT_TO_FILE[fmt]).write_text(result.stdout or "", encoding="utf-8")
    return True, ""


def extract_trafilatura(binary: str) -> tuple[bool, str | None, str]:
    timeout = get_env_int("TRAFILATURA_TIMEOUT") or get_env_int("TIMEOUT", 60)
    html_source = find_html_source()
    if not html_source:
        return False, None, "No HTML source found (run singlefile, dom, or wget first)"

    formats = get_enabled_formats()
    if not formats:
        return False, None, "No Trafilatura output formats enabled"

    for fmt in formats:
        success, error = run_trafilatura(binary, html_source, fmt, timeout)
        if not success:
            return False, None, error

    output_file = FORMAT_TO_FILE[formats[0]]
    return True, output_file, ""


@click.command()
@click.option("--url", required=True, help="URL to extract article from")
@click.option("--snapshot-id", required=True, help="Snapshot UUID")
def main(url: str, snapshot_id: str):
    try:
        if not get_env_bool("TRAFILATURA_ENABLED", True):
            sys.exit(0)

        success, output, error = extract_trafilatura(
            get_env("TRAFILATURA_BINARY", "trafilatura")
        )

        if success:
            print(
                json.dumps(
                    {
                        "type": "ArchiveResult",
                        "status": "succeeded",
                        "output_str": output or "",
                    }
                )
            )
            sys.exit(0)

        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)

    except subprocess.TimeoutExpired as err:
        print(f"ERROR: Timed out after {err.timeout} seconds", file=sys.stderr)
        sys.exit(1)
    except Exception as err:
        print(f"ERROR: {type(err).__name__}: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
