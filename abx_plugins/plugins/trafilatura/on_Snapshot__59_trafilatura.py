#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Extract article content using trafilatura from local HTML snapshots."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

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
OUTPUT_ENV_TO_FORMAT = {
    "TRAFILATURA_OUTPUT_TXT": "txt",
    "TRAFILATURA_OUTPUT_MARKDOWN": "markdown",
    "TRAFILATURA_OUTPUT_HTML": "html",
    "TRAFILATURA_OUTPUT_CSV": "csv",
    "TRAFILATURA_OUTPUT_JSON": "json",
    "TRAFILATURA_OUTPUT_XML": "xml",
    "TRAFILATURA_OUTPUT_XMLTEI": "xmltei",
}

TRAFILATURA_EXTRACT_SCRIPT = """
import sys
from pathlib import Path
import trafilatura

html = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
url = sys.argv[2]
fmt = sys.argv[3]
result = trafilatura.extract(
    html,
    output_format=fmt,
    with_metadata=True,
    url=url,
) or ""
sys.stdout.write(result)
"""


def emit_archive_result(status: str, output_str: str) -> None:
    print(
        json.dumps(
            {
                "type": "ArchiveResult",
                "status": status,
                "output_str": output_str,
            }
        )
    )


def write_text_atomic(path: Path, text: str) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


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
        fmt for env_name, fmt in OUTPUT_ENV_TO_FORMAT.items()
        if get_env_bool(env_name, fmt in {"txt", "markdown", "html"})
    ]


def run_trafilatura(
    binary: str, html_source: str, url: str, fmt: str, timeout: int
) -> tuple[bool, str]:
    resolved_binary = shutil.which(binary) or binary
    binary_path = Path(resolved_binary)

    python_candidates = (
        binary_path.with_name("python"),
        binary_path.with_name("python3"),
    )
    python_bin = next((candidate for candidate in python_candidates if candidate.exists()), Path(sys.executable))

    cmd = [
        str(python_bin),
        "-c",
        TRAFILATURA_EXTRACT_SCRIPT,
        html_source,
        url,
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

    write_text_atomic(OUTPUT_DIR / FORMAT_TO_FILE[fmt], result.stdout or "")
    return True, ""


def extract_trafilatura(url: str, binary: str) -> tuple[str, str]:
    timeout = get_env_int("TRAFILATURA_TIMEOUT") or get_env_int("TIMEOUT", 60)
    html_source = find_html_source()
    if not html_source:
        return "noresults", "No HTML source found"

    formats = get_enabled_formats()
    if not formats:
        return "noresults", "No output formats enabled"

    for fmt in formats:
        success, error = run_trafilatura(binary, html_source, url, fmt, timeout)
        if not success:
            return "failed", error

    output_file = FORMAT_TO_FILE[formats[0]]
    return "succeeded", output_file


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="URL to extract article from")
    parser.add_argument("--snapshot-id", required=True, help="Snapshot UUID")
    args = parser.parse_args()

    try:
        if not get_env_bool("TRAFILATURA_ENABLED", True):
            emit_archive_result("skipped", "TRAFILATURA_ENABLED=False")
            sys.exit(0)

        status, output = extract_trafilatura(
            args.url,
            get_env("TRAFILATURA_BINARY", "trafilatura")
        )

        if status == "failed":
            print(f"ERROR: {output}", file=sys.stderr)
        emit_archive_result(status, output)
        sys.exit(0 if status != "failed" else 1)

    except subprocess.TimeoutExpired as err:
        error = f"Timed out after {err.timeout} seconds"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result("failed", error)
        sys.exit(1)
    except Exception as err:
        error = f"{type(err).__name__}: {err}"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result("failed", error)
        sys.exit(1)


if __name__ == "__main__":
    main()
