"""Bounded offline main-document input from indexed WACZ captures.

No archive scan or network fallback: unsupported captures remain unavailable.
"""

import gzip
import hashlib
import io
import json
from pathlib import Path
from urllib.parse import urljoin
import zipfile
import zlib
from warcio.archiveiterator import ArchiveIterator
from warcio.exceptions import ArchiveLoadFailed
import time

MAX_ARCHIVE = 256 * 1024 * 1024
MAX_INDEX = 8 * 1024 * 1024
MAX_COMPRESSED = 16 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024


class Unsupported(ValueError):
    pass


class BoundedZipReader(io.BufferedReader):
    """Bound ZIP metadata reads before ZipFile allocates its central directory."""

    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            position = self.tell()
            size = self.seek(0, io.SEEK_END) - position
            self.seek(position)
        if size > MAX_COMPRESSED:
            raise Unsupported("ZIP metadata/read cap")
        return super().read(size)


def extract(path: Path, requested: str) -> bytes:
    # Bound both metadata allocation and public ZipExtFile seek's CRC work.
    if path.stat().st_size > MAX_ARCHIVE:
        raise Unsupported("archive cap")
    deadline = time.monotonic() + 10

    def check_deadline():
        if time.monotonic() > deadline:
            raise Unsupported("WACZ input deadline")

    with (
        BoundedZipReader(path.open("rb", buffering=0)) as source,
        zipfile.ZipFile(source) as archive,
    ):
        if len(archive.infolist()) > 1024:
            raise Unsupported("entry cap")
        if len(set(archive.namelist())) != len(archive.infolist()):
            raise Unsupported("ambiguous ZIP members")
        index_name = "indexes/index.cdx"
        if index_name not in archive.namelist():
            index_name = "indexes/index.cdx.gz"
        info = archive.getinfo(index_name)
        if info.file_size > MAX_INDEX or info.compress_size > MAX_INDEX:
            raise Unsupported("index cap")
        with archive.open(info) as stream:
            raw = stream.read(MAX_INDEX + 1)
        if index_name.endswith(".gz"):
            # ArchiveWeb.page switches to concatenated 1024-row gzip blocks
            # at 2048 resources. GzipFile validates every consumed block's CRC;
            # a bounded full index read avoids implementing a second IDX parser.
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as index_stream:
                raw = index_stream.read(MAX_INDEX + 1)
        if len(raw) > MAX_INDEX:
            raise Unsupported("index cap")
        indexed = {}
        for line in io.BytesIO(raw):
            check_deadline()
            if len(line) > 16384:
                raise Unsupported("index line cap")
            _, timestamp, payload = line.split(b" ", 2)
            row = json.loads(payload)
            if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                raise Unsupported("invalid index metadata")
            url = row["url"]
            if row.get("method", "GET") != "GET":
                continue
            if url not in indexed or timestamp > indexed[url][0]:
                indexed[url] = (timestamp, row)
        url = requested
        seen = set()
        for _ in range(5):
            check_deadline()
            if url in seen or url not in indexed:
                raise Unsupported("missing main URL or redirect cycle")
            seen.add(url)
            row = indexed[url][1]
            filename = row["filename"]
            if not isinstance(filename, str):
                raise Unsupported("invalid archive filename")
            if "/" in filename or "\\" in filename or filename in {".", ".."}:
                raise Unsupported("unsupported archive filename")
            member = archive.getinfo("archive/" + filename)
            if member.compress_type != zipfile.ZIP_STORED:
                raise Unsupported(
                    "ZIP-compressed WARC requires sequential decompression",
                )
            offset, length = int(row["offset"]), int(row["length"])
            if (
                offset < 0
                or not 0 < length <= MAX_COMPRESSED
                or offset + length > member.file_size
            ):
                raise Unsupported("record bounds")
            # Public ZIP seek is bounded by MAX_ARCHIVE; only stored members
            # are accepted so it never inflates unrelated WARC data. The CDX
            # record digest verifies the selected range independently of ZIP CRC.
            with archive.open(member) as opened:
                opened.seek(offset)
                compressed = opened.read(length)
            check_deadline()
            if (
                len(compressed) != length
                or row.get("recordDigest")
                != "sha256:" + hashlib.sha256(compressed).hexdigest()
            ):
                raise Unsupported("missing/mismatched record digest")
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as inflated:
                expanded = inflated.read(MAX_EXPANDED + 1)
            if len(expanded) > MAX_EXPANDED:
                raise Unsupported("expanded record cap")
            records = list(ArchiveIterator(io.BytesIO(expanded), check_digests=True))
            check_deadline()
            # ArchiveIterator skips consumed body data when advanced, so reopen
            # the single bounded record for payload consumption below.
            if len(records) != 1:
                raise Unsupported("not one WARC record")
            checker = records[0].digest_checker
            if checker is None or checker.problems:
                raise Unsupported("WARC digest mismatch")
            record = next(ArchiveIterator(io.BytesIO(expanded), check_digests=True))
            if record.rec_type != "response" or record.http_headers is None:
                raise Unsupported("revisit/nonresponse requires explicit resolver")
            if record.rec_headers.get_header("WARC-Target-URI") != url:
                raise Unsupported("target mismatch")
            status = int(record.http_headers.get_statuscode())
            if status != int(row["status"]):
                raise Unsupported("index status mismatch")
            if status in {301, 302, 303, 307, 308}:
                location = record.http_headers.get_header("Location")
                if not location:
                    raise Unsupported("redirect without location")
                url = urljoin(url, location)
                continue
            mime = (
                (record.http_headers.get_header("Content-Type") or "")
                .split(";")[0]
                .strip()
                .lower()
            )
            if (
                status != 200
                or mime not in {"text/html", "application/xhtml+xml"}
                or row.get("mime") != mime
            ):
                raise Unsupported("not successful HTML")
            # Browser WACZ exports normally store decoded response bodies.
            # Do not hand encoded bodies to an unbounded optional decompressor.
            encoding = (
                record.http_headers.get_header("Content-Encoding") or "identity"
            ).lower()
            if encoding != "identity":
                raise Unsupported("encoded HTTP body")
            body = record.content_stream().read(MAX_EXPANDED + 1)
            if len(body) > MAX_EXPANDED:
                raise Unsupported("decoded body cap")
            check_deadline()
            return body
    raise Unsupported("redirect bound")


def find_wacz_html_source(url: str) -> str | None:
    """Materialize a verified HTML input beside the consumer's own outputs."""
    if not url:
        return None
    from abx_plugins.plugins.base.utils import write_text_atomic
    import sys

    for base in (Path.cwd(), Path.cwd().parent):
        path = base / "archivewebpage/archivewebpage.wacz"
        if not path.is_file():
            continue
        try:
            body = extract(path, url)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            EOFError,
            zipfile.BadZipFile,
            zlib.error,
            ArchiveLoadFailed,
        ) as error:
            print(
                f"[article input] WACZ unavailable ({type(error).__name__})",
                file=sys.stderr,
            )
            return None
        # A separate file per consumer avoids races and preserves the WACZ.
        destination = Path.cwd() / ".wacz-article-input.html"
        write_text_atomic(destination, body.decode("utf-8", errors="replace"))
        return str(destination)
    return None
