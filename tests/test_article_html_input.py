# ci-runner: hosted-linux

"""Verify archive input behavior using files and real non-mappable OS pipes."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import errno
import mmap
import os

import pytest

from abx_plugins.plugins.base.utils import (
    ARTICLE_HTML_BASE64_STRIP_SIZE,
    _article_html_buffer,
    read_article_html,
)


@pytest.mark.parametrize("large", [False, True])
def test_article_reader_falls_back_when_kernel_mmap_returns_enodev(large):
    article = b"<article>Saved page.</article>"
    capture = article * (
        ARTICLE_HTML_BASE64_STRIP_SIZE // len(article) + 1 if large else 1
    )
    read_fd, write_fd = os.pipe()
    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        os.fdopen(write_fd, "wb", buffering=0) as writer,
        os.fdopen(read_fd, "rb") as source_file,
        ExitStack() as stack,
    ):
        # A pipe has the same syscall contract as the reported FUSE file:
        # bytes are readable, but the kernel rejects mapping the descriptor.
        with pytest.raises(OSError) as error:
            mmap.mmap(read_fd, len(capture), access=mmap.ACCESS_READ)
        assert error.value.errno == errno.ENODEV
        pending = executor.submit(writer.write, capture)
        source = _article_html_buffer(source_file, len(capture), stack)
        assert isinstance(source, mmap.mmap if large else bytes)
        assert source[:] == capture
        assert pending.result() == len(capture)


def test_article_reader_keeps_unrelated_mmap_failure(tmp_path):
    path = tmp_path / "write-only.html"
    with path.open("wb") as writer, ExitStack() as stack:
        writer.write(b"sample")
        writer.flush()
        with pytest.raises(OSError) as error:
            _article_html_buffer(writer, 6, stack)
        assert error.value.errno == errno.EACCES


def test_article_reader_keeps_regular_file_cleaning(tmp_path):
    path = tmp_path / "saved.html"
    path.write_bytes(
        b'<img src="data:image/png;base64,'
        + b"A" * 1024**2
        + b'">\r\n<article>saved\xff</article>',
    )
    contents, stripped = read_article_html(str(path))
    assert stripped
    assert "inline-images-stripped-by-archivebox" in contents
    assert "</article>" in contents and "saved\ufffd" in contents
    assert "\r" not in contents


@pytest.mark.parametrize("size", [32, ARTICLE_HTML_BASE64_STRIP_SIZE + 1])
def test_article_reader_rejects_short_fallback_input(size):
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "wb") as writer:
        writer.write(b"<p>short</p>")
    with os.fdopen(read_fd, "rb") as source_file, ExitStack() as stack:
        with pytest.raises(OSError) as error:
            _article_html_buffer(source_file, size, stack)
        assert error.value.errno == errno.EIO
