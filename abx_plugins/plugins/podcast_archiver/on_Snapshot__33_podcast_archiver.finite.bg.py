#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""Archive episodes from a podcast feed using podcast-archiver."""

import os
import signal
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import rich_click as click

from abx_plugins.plugins.base.utils import emit_archive_result_record, load_config

CONFIG_PATH = Path(__file__).resolve().with_name("config.json")
MEDIA_SUFFIXES = {".mp3", ".m4a", ".mp4", ".ogg", ".opus", ".wav", ".flac", ".aac", ".webm"}


@click.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@click.option("--url", required=True)
def main(url: str) -> None:
    config = load_config(CONFIG_PATH, hydrate_binaries=False)
    if not config.PODCAST_ARCHIVER_ENABLED:
        print("Podcast Archiver disabled", flush=True)
        emit_archive_result_record("skipped", "PODCAST_ARCHIVER_ENABLED=False")
        return

    # Snapshot cleanup sends SIGTERM to background process groups. Let finite
    # downloads finish during the runner's grace period, including the child.
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    print("Podcast Archiver ready", flush=True)
    output_dir = Path(config.SNAP_DIR or ".").resolve() / "podcast_archiver"
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": str(config.USER_AGENT)})
        with urllib.request.urlopen(request, timeout=config.PODCAST_ARCHIVER_TIMEOUT) as response:
            parser = ET.XMLPullParser(events=("start", "end"))
            is_feed = False
            has_enclosure = False
            first = True
            try:
                while chunk := response.read(65536):
                    parser.feed(chunk)
                    for event, element in parser.read_events():
                        if first and event == "start":
                            first = False
                            is_feed = element.tag.lower() in {"rss", "{http://www.w3.org/2005/atom}feed", "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}rdf"}
                            if not is_feed:
                                break
                        if event == "end":
                            tag = element.tag.lower()
                            if (tag.endswith("enclosure") and element.get("url")) or (tag.endswith("link") and element.get("rel") == "enclosure" and element.get("href")):
                                has_enclosure = True
                                break
                            element.clear()
                    if not is_feed or has_enclosure:
                        break
            except ET.ParseError:
                pass
        if not is_feed:
            emit_archive_result_record("noresults", "Not a podcast feed")
            return
        if not has_enclosure:
            emit_archive_result_record("noresults", "Feed has no podcast enclosures")
            return

        with tempfile.TemporaryDirectory(prefix=".podcast-", dir=output_dir) as temp:
            staging = Path(temp)
            (staging / "episodes").mkdir()
            (staging / "config.yaml").write_text("{}\n")
            command = [str(config.PODCAST_ARCHIVER_BINARY), "--config", str(staging / "config.yaml"), "--feed", url, "--dir", str(staging / "episodes"), "--database", str(staging / "podcast.db"), "--ignore-database", "--write-info-json"]
            if config.PODCAST_ARCHIVER_MAX_EPISODES:
                command += ["--max-episodes", str(config.PODCAST_ARCHIVER_MAX_EPISODES)]
            child_env = {key: value for key, value in os.environ.items() if not key.startswith("PODCAST_ARCHIVER_")}
            completed = subprocess.run(command, stdout=sys.stderr, stderr=sys.stderr, timeout=config.PODCAST_ARCHIVER_TIMEOUT, check=False, env=child_env)
            if completed.returncode:
                raise RuntimeError(f"podcast-archiver exited {completed.returncode}")
            files = sorted(path for path in (staging / "episodes").rglob("*") if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES and path.stat().st_size)
            if not files:
                raise RuntimeError("podcast-archiver produced no episode files")
            primary = files[0].relative_to(staging / "episodes")
            target = output_dir / "episodes"
            backup = staging / "previous"
            if target.exists():
                target.rename(backup)
            try:
                (staging / "episodes").rename(target)
            except OSError:
                if backup.exists():
                    backup.rename(target)
                raise
        emit_archive_result_record("succeeded", str(Path("podcast_archiver") / "episodes" / primary))
    except Exception as exc:
        print(f"Podcast Archiver: {exc}", file=sys.stderr)
        emit_archive_result_record("failed", f"{type(exc).__name__}: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
