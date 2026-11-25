"""Exercise containment against real filesystem objects, including symlinks."""

import os
from pathlib import Path

import pytest

from abx_plugins.plugins.base.filesystem import resolve_contained_file


@pytest.mark.parametrize(
    "target",
    ["file", "inside", "outside", "directory", "missing", "loop", "fifo"],
)
def test_resolve_contained_file(tmp_path, target, request):
    root = tmp_path / "snapshot"
    root.mkdir()
    saved = root / "saved.txt"
    saved.write_text("Saved article text")
    source = root / "output.txt"
    if target == "file":
        source.write_bytes(saved.read_bytes())
    elif target == "inside":
        source.symlink_to(saved)
    elif target == "outside":
        outside = tmp_path / "private.txt"
        outside.write_text("Private data")
        source.symlink_to(outside)
    elif target == "directory":
        source.mkdir()
    elif target == "loop":
        source.symlink_to(source)
        request.addfinalizer(source.unlink)
    elif target == "fifo":
        os.mkfifo(source)

    if target in {"file", "inside"}:
        with resolve_contained_file(root, "output.txt") as (source, info):
            assert source.read_bytes() == saved.read_bytes()
            assert info.st_size == saved.stat().st_size
    else:
        with pytest.raises((OSError, ValueError, RuntimeError)):
            with resolve_contained_file(root, "output.txt"):
                pytest.fail("An unreadable or uncontained output was opened")


def test_contained_read_pins_file_and_supports_symlinked_root(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    source = root / "output.txt"
    source.write_text("Saved text")
    outside = tmp_path / "private.txt"
    outside.write_text("Private data")
    with (
        resolve_contained_file(alias, "output.txt") as (checked, info),
        checked.open("rb") as stream,
    ):
        # Replacing the pathname after opening must never redirect its stream.
        source.unlink()
        source.symlink_to(outside)
        assert stream.read() == b"Saved text"
        assert info.st_size == len(b"Saved text")
    with pytest.raises(ValueError):
        with resolve_contained_file(alias, "output.txt"):
            pytest.fail("Replacement symlink escaped containment")


@pytest.mark.parametrize("relative", ["../private.txt", "/etc/passwd"])
def test_contained_read_rejects_nonrelative_paths(tmp_path, relative):
    with pytest.raises(ValueError):
        with resolve_contained_file(tmp_path, Path(relative)):
            pytest.fail("Nonrelative output was accepted")
