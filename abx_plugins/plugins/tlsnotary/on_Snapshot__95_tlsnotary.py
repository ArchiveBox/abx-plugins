#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["abx-plugins", "rich-click"]
# ///
"""Produce an offline-verifiable artifact for the response captured by TLSNotary itself."""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import rich_click as click

from abx_plugins.plugins.base.utils import emit_archive_result_record, load_config


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True)
def main(url: str) -> None:
    config = load_config()
    if not config.TLSNOTARY_ENABLED:
        emit_archive_result_record("skipped", "TLSNOTARY_ENABLED=False")
        return
    if urlsplit(url).scheme != "https":
        emit_archive_result_record(
            "noresults",
            "TLS notarization requires an HTTPS URL",
        )
        return
    plugin = Path(__file__).resolve().parent
    output = Path(config.SNAP_DIR).resolve() / "tlsnotary"
    output.mkdir(parents=True, exist_ok=True)
    binary = config.TLSNOTARY_BINARY or str(
        Path(config.ABXPKG_LIB_DIR)
        / "tlsnotary"
        / "target"
        / "release"
        / "abx-tlsnotary",
    )
    try:
        trusted_key = (
            config.TLSNOTARY_TRUSTED_KEY
            or json.loads((plugin / "web" / "trust.json").read_text())["public_key"]
        )
        with tempfile.TemporaryDirectory(prefix=".capture-", dir=output) as staging:
            result = subprocess.run(
                [
                    binary,
                    "capture",
                    "--url",
                    url,
                    "--notary",
                    config.TLSNOTARY_NOTARY_URL,
                    "--trusted-key",
                    trusted_key,
                    "--output",
                    staging,
                    "--max-recv",
                    str(config.TLSNOTARY_MAX_RECV_BYTES),
                    "--timeout",
                    str(config.TLSNOTARY_TIMEOUT),
                ],
                capture_output=True,
                text=True,
                timeout=config.TLSNOTARY_TIMEOUT + 5,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "notarization failed")
            metadata = json.loads((Path(staging) / "verified.json").read_text())
            if not 200 <= metadata["status"] < 300:
                raise RuntimeError(
                    f"Authenticated HTTP {metadata['status']}; not a successful page capture",
                )
            # The native runtime verifies the artifact before writing it. Do not associate
            # its signature with independently downloaded DOM, WARC, screenshot or media.
            for name in ("response.body", "verified.json", "capture.tlsn"):
                os.replace(Path(staging) / name, output / name)
        emit_archive_result_record("succeeded", "capture.tlsn")
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        emit_archive_result_record("failed", str(error))
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
