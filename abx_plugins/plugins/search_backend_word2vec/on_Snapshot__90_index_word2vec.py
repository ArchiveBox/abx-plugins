#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""Index the first available extractor text without crawling the output tree."""

import argparse
import os
import sys
from pathlib import Path

from abx_plugins.plugins.base.search_text import clean_index_text
from abx_plugins.plugins.base.utils import emit_archive_result_record
from abx_plugins.plugins.search_backend_word2vec.search import (
    RuntimeUnavailable,
    enabled,
    index_text,
)


def main() -> int:
    if not enabled():
        emit_archive_result_record("skipped", "")
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--url", required=True)
    args, _ = parser.parse_known_args()
    directory = Path(os.environ.get("SNAP_DIR") or ".").resolve()
    texts = []
    for name in (
        "title/title.txt",
        "readability/content.txt",
        "defuddle/content.txt",
        "mercury/content.txt",
        "trafilatura/content.txt",
    ):
        path = directory / name
        if not path.is_file() or not path.resolve().is_relative_to(directory):
            continue
        with path.open("rb") as source:
            text = clean_index_text(source.read(16 * 1024).decode("utf-8", "ignore"))[
                :4096
            ]
        if text.strip():
            texts.append(text)
            if name != "title/title.txt":
                break
    if not texts:
        emit_archive_result_record("noresults", "")
        return 0
    try:
        index_text(args.snapshot_id, "\n\n".join(texts))
    except RuntimeUnavailable as error:
        print(str(error) or "Word2Vec Search runtime unavailable", file=sys.stderr)
        return 1
    emit_archive_result_record("succeeded", "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
