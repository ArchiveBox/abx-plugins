#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""Timestamp a completed sibling hashes manifest using the standalone ots CLI."""

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import rich_click as click

from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    load_config,
    write_text_atomic,
)


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
        # ots refuses an existing proof; work outside the snapshot until success.
        stage = Path(tempfile.mkdtemp(prefix="abx-opentimestamps-"))
        (stage / "hashes.json").symlink_to(snap_dir / "hashes/hashes.json")
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
        submitted_at = datetime.now(UTC).isoformat()
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

        # Preserve the real client's proof path and submission metadata. The pinned
        # CLI stamps one file by appending a 16-byte nonce to its binary digest.
        info = subprocess.run(
            [config.OPENTIMESTAMPS_BINARY, "info", str(proof)],
            capture_output=True,
            text=True,
            check=True,
            timeout=max(0.1, deadline - time.monotonic()),
        ).stdout
        manifest_hash = hashlib.sha256(manifest).hexdigest()
        initial_ops = re.match(
            rf"File sha256 hash: {manifest_hash}\nTimestamp:\nappend ([0-9a-f]{{32}})\nsha256\n",
            info,
        )
        if not initial_ops:
            raise ValueError("Unexpected OpenTimestamps single-file commitment path")
        nonce = initial_ops.group(1)
        write_text_atomic(output / "proof-info.txt", info)
        write_text_atomic(
            output / "submission.json",
            json.dumps(
                {
                    "submitted_at": submitted_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "configured_calendars": calendars,
                    "required_calendar_replies": config.OPENTIMESTAMPS_REQUIRED_CALENDARS,
                    "manifest_sha256": manifest_hash,
                    "nonce_hex": nonce,
                    "submitted_digest": hashlib.sha256(
                        bytes.fromhex(manifest_hash + nonce),
                    ).hexdigest(),
                    "pending_attestations": re.findall(
                        r"PendingAttestation\('([^']+)'\)",
                        info,
                    ),
                    "proof_bytes": proof.stat().st_size,
                },
                indent=2,
            ),
        )

        pending_manifest = output / ".hashes.json.tmp"
        pending_manifest.unlink(missing_ok=True)
        pending_manifest.symlink_to("../hashes/hashes.json")
        pending_manifest.replace(output / "hashes.json")
        pending_proof = output / ".hashes.json.ots.tmp"
        pending_proof.write_bytes(proof.read_bytes())
        pending_proof.replace(output / "hashes.json.ots")
        emit_archive_result_record("succeeded", "opentimestamps/hashes.json.ots")
    except Exception as error:
        print(f"[opentimestamps] {type(error).__name__}: {error}", file=sys.stderr)
        emit_archive_result_record("failed", str(error))
        sys.exit(1)
    finally:
        if stage is not None:
            shutil.rmtree(stage)


if __name__ == "__main__":
    main()
