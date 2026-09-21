#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""Timestamp a completed sibling hashes manifest using the standalone ots CLI."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import rich_click as click

from abx_plugins.plugins.base.utils import emit_archive_result_record, load_config


def read_manifest(snap_dir: Path) -> bytes:
    """A JSON file alone may be stale or half-written: require hashes' final marker."""
    hashes_dir = snap_dir / "hashes"
    completion = (hashes_dir / "hashes.sha256").read_text().strip()
    manifest = (hashes_dir / "hashes.json").read_bytes()
    if hashlib.sha256(manifest).hexdigest() != completion:
        raise ValueError(
            "Hash manifest does not match its completion checksum; rerun hashes",
        )
    if (hashes_dir / "hashes.sha256").read_text().strip() != completion:
        raise ValueError("Hashes changed while reading the manifest")
    return manifest


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="URL being archived")
def main(url: str) -> None:
    """Run after hashes finishes; no runner APIs or database access are needed."""
    stage = None
    pending = None
    try:
        config = load_config()
        if not config.OPENTIMESTAMPS_ENABLED:
            emit_archive_result_record("skipped", "OPENTIMESTAMPS_ENABLED=False")
            return
        if not config.HASHES_ENABLED:
            raise ValueError("OpenTimestamps requires HASHES_ENABLED=True")
        deadline = time.monotonic() + config.OPENTIMESTAMPS_TIMEOUT - 2
        calendars = list(dict.fromkeys(config.OPENTIMESTAMPS_CALENDARS))
        if not 1 <= config.OPENTIMESTAMPS_REQUIRED_CALENDARS <= len(calendars):
            raise ValueError(
                "Required calendar count exceeds the configured distinct calendars",
            )
        for calendar in calendars:
            endpoint = urlsplit(calendar)
            if endpoint.scheme not in {"https", "http"} or not endpoint.hostname:
                raise ValueError("Calendar URLs must use HTTP or HTTPS")

        snap_dir = Path(config.SNAP_DIR).resolve()
        manifest = read_manifest(snap_dir)
        data = json.loads(manifest)
        root_hash = data["root_hash"]
        if (
            not isinstance(root_hash, str)
            or len(root_hash) != 64
            or any(char not in "0123456789abcdef" for char in root_hash)
        ):
            raise ValueError("Invalid Merkle root in hashes.json")
        output = snap_dir / "opentimestamps"
        output.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".stamp-", dir=output))
        (stage / "hashes.json").write_bytes(manifest)
        command = [
            config.OPENTIMESTAMPS_BINARY,
            "--no-cache",
            # The client otherwise hides individual calendar errors at DEBUG level.
            "--verbose",
            "stamp",
            "--timeout",
            str(max(1, int(deadline - time.monotonic()))),
            "-m",
            str(config.OPENTIMESTAMPS_REQUIRED_CALENDARS),
        ]
        for calendar in calendars:
            command.extend(["-c", calendar])
        command.append("hashes.json")
        result = subprocess.run(
            command,
            cwd=stage,
            capture_output=True,
            text=True,
            timeout=max(0.1, deadline - time.monotonic()),
        )
        if result.stdout:
            print(result.stdout, file=sys.stderr, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        if result.returncode:
            raise RuntimeError(f"ots stamp exited {result.returncode}; see hook log")
        proof = stage / "hashes.json.ots"
        if not proof.is_file() or not proof.stat().st_size:
            raise ValueError("ots stamp produced no detached proof")
        if read_manifest(snap_dir) != manifest:
            raise ValueError("Hashes changed during submission; rerun OpenTimestamps")

        # Publish matching manifest/proof together, retaining earlier evidence.
        generation = output / stage.name.removeprefix(".")
        stage.rename(generation)
        stage = None
        pending = output / f".current-{os.getpid()}"
        pending.symlink_to(generation.name, target_is_directory=True)
        pending.replace(output / "current")
        emit_archive_result_record(
            "succeeded",
            "opentimestamps/current/hashes.json.ots",
        )
    except Exception as error:
        print(f"[opentimestamps] {type(error).__name__}: {error}", file=sys.stderr)
        emit_archive_result_record("failed", str(error))
        sys.exit(1)
    finally:
        if stage is not None:
            shutil.rmtree(stage)
        if pending is not None:
            pending.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
