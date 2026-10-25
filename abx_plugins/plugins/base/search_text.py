"""Bound search input after removing inline data and encoded binary payloads."""

import codecs
import io
import re
from collections.abc import Iterable, Iterator
from pathlib import Path


_DATA_START = re.compile(r"\bdata:", re.IGNORECASE)
_DATA_PREFIX = re.compile(r"d(?:a(?:t(?:a)?)?)?\Z", re.IGNORECASE)
_DATA_END = re.compile(r"[\s<>\"'`)\]}\\]")
_ENCODED_RUN = re.compile(r"[A-Za-z0-9+/_=-]+")
_ENCODED_TAIL = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_=-]+\Z")
_ENCODED_BLOB = re.compile(r"[A-Za-z0-9+/_=-]{256,}")


def _without_data_urls(chunks: Iterable[str]) -> Iterator[str]:
    carry = ""
    dropping = False
    for chunk in chunks:
        text = carry + chunk
        carry = ""
        if dropping:
            end = _DATA_END.search(text)
            if end is None:
                continue
            text = text[end.start() :]
            dropping = False
        position = 0
        while match := _DATA_START.search(text, position):
            yield text[position : match.start()]
            end = _DATA_END.search(text, match.end())
            if end is None:
                dropping = True
                position = len(text)
                break
            position = end.start()
        if not dropping:
            remaining = text[position:]
            # Keep only an incomplete scheme and its preceding character. A
            # fixed tail can cut "metadata:x" into a false "data:x" URL.
            partial = _DATA_PREFIX.search(remaining)
            boundary = max(0, (partial.start() if partial else len(remaining)) - 1)
            carry = remaining[boundary:]
            yield remaining[:boundary]
    if carry:
        yield carry


def _without_encoded_blobs(chunks: Iterable[str]) -> Iterator[str]:
    carry = ""
    dropping = False
    for chunk in chunks:
        text = carry + chunk
        carry = ""
        if dropping:
            run = _ENCODED_RUN.match(text)
            if run:
                text = text[run.end() :]
            if not text:
                continue
            dropping = False
        tail = _ENCODED_TAIL.search(text)
        if tail:
            trailing = tail.group()
            text = text[: tail.start()]
            if len(trailing) >= 256:
                dropping = True
            else:
                carry = trailing
        yield _ENCODED_BLOB.sub("", text)
    if carry:
        yield carry


def clean_index_text(text: str) -> str:
    """Remove data URLs and long ASCII payloads without changing other text."""
    return "".join(_without_encoded_blobs(_without_data_urls((text,))))


def read_indexable_text(path: Path, max_bytes: int) -> str:
    """Read cleaned UTF-8 text, retaining one whole character past the limit.

    An over-limit result has at most ``max_bytes + 4`` UTF-8 bytes so callers
    can reject it without confusing a split multibyte character with EOF.
    Raw embedded data never counts toward this cleaned-input limit.
    """
    if max_bytes < 0:
        raise ValueError("max_bytes must be nonnegative")
    result = io.StringIO()
    total = 0
    with path.open("rb") as source:
        raw_chunks = iter(lambda: source.read(64 * 1024), b"")
        decoded = codecs.iterdecode(raw_chunks, "utf-8", errors="ignore")
        for piece in _without_encoded_blobs(_without_data_urls(decoded)):
            if not piece:
                continue
            encoded = piece.encode("utf-8")
            if total + len(encoded) > max_bytes:
                end = max_bytes + 1 - total
                # Advance only over the at-most-three UTF-8 continuation bytes.
                while end < len(encoded) and encoded[end] & 0xC0 == 0x80:
                    end += 1
                result.write(encoded[:end].decode("utf-8"))
                return result.getvalue()
            result.write(piece)
            total += len(encoded)
    return result.getvalue()
