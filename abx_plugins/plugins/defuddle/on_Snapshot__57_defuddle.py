#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "click",
# ]
# ///
#
# Extract article content using Defuddle.

import argparse
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.name
SNAP_DIR = Path(os.environ.get("SNAP_DIR", ".")).resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


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
        return default if default is not None else []
    except json.JSONDecodeError:
        return default if default is not None else []


def extract_defuddle(url: str, binary: str) -> tuple[bool, str | None, str]:
    timeout = get_env_int("DEFUDDLE_TIMEOUT") or get_env_int("TIMEOUT", 60)
    defuddle_args = get_env_array("DEFUDDLE_ARGS", [])
    defuddle_args_extra = get_env_array("DEFUDDLE_ARGS_EXTRA", [])
    output_dir = Path(OUTPUT_DIR)

    try:
        cmd = [binary, *defuddle_args, *defuddle_args_extra, url]
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True,
        )

        if result.returncode != 0:
            err = (result.stderr or "").strip()
            if err:
                return False, None, f"defuddle failed (exit={result.returncode}): {err}"
            return False, None, f"defuddle failed (exit={result.returncode})"

        raw_output = result.stdout.strip()
        html_content = ""
        text_content = ""
        metadata: dict[str, object] = {}

        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError:
            parsed = None

        if isinstance(parsed, dict):
            html_content = str(parsed.get("content") or parsed.get("html") or "")
            text_content = str(
                parsed.get("textContent")
                or parsed.get("text")
                or parsed.get("markdown")
                or ""
            )
            metadata = {
                key: value
                for key, value in parsed.items()
                if key not in {"content", "html", "textContent", "text", "markdown"}
            }
        elif raw_output:
            text_content = raw_output

        if text_content and not html_content:
            html_content = f"<pre>{html.escape(text_content)}</pre>"

        if not text_content and html_content:
            text_content = re.sub(r"<[^>]+>", " ", html_content)
            text_content = " ".join(text_content.split())

        if not text_content and not html_content:
            return False, None, "No content extracted"

        (output_dir / "content.html").write_text(html_content, encoding="utf-8")
        (output_dir / "content.txt").write_text(text_content, encoding="utf-8")
        (output_dir / "article.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )

        return True, "content.html", ""
    except subprocess.TimeoutExpired:
        return False, None, f"Timed out after {timeout} seconds"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


def main():
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument("--url", required=True, help="URL to extract article from")
        parser.add_argument("--snapshot-id", required=True, help="Snapshot UUID")
        args = parser.parse_args()

        if not get_env_bool("DEFUDDLE_ENABLED", True):
            print("Skipping defuddle (DEFUDDLE_ENABLED=False)", file=sys.stderr)
            sys.exit(0)

        binary = get_env("DEFUDDLE_BINARY", "defuddle")
        success, output, error = extract_defuddle(args.url, binary)

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
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
