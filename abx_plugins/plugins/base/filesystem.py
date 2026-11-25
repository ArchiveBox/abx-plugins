"""Contained reads of saved plugin artifacts."""

import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path


@contextmanager
def resolve_contained_file(
    root: Path,
    relative: str | Path,
    *,
    resolved_root: Path | None = None,
):
    """Yield a contained regular file path and stat, valid within this context."""
    root, relative = Path(root), Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Output must be inside its snapshot directory")

    with ExitStack() as handles:
        if hasattr(os, "O_PATH") and Path("/proc/self/fd").is_dir():
            # Path.resolve lstats every ancestor for every artifact. On FUSE
            # that repeats remote metadata requests for the same archive root.
            # O_PATH lets the kernel resolve once without opening a device or
            # FIFO. Inspect the pinned inode before opening it for any I/O.
            flags = os.O_PATH | os.O_CLOEXEC
            root_fd = os.open(root, flags | os.O_DIRECTORY)
            handles.callback(os.close, root_fd)
            source_fd = os.open(relative, flags, dir_fd=root_fd)
            handles.callback(os.close, source_fd)
            resolved_root = Path(os.readlink(f"/proc/self/fd/{root_fd}"))
            source = Path(f"/proc/self/fd/{source_fd}")
            if not Path(os.readlink(source)).is_relative_to(resolved_root):
                raise ValueError("Output must be inside its snapshot directory")
            info = os.fstat(source_fd)
        else:
            resolved_root = resolved_root or root.resolve()
            source = (resolved_root / relative).resolve()
            if not source.is_relative_to(resolved_root):
                raise ValueError("Output must be inside its snapshot directory")
            info = source.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Output must be a regular file")
        # On Linux opening this path reads the checked inode, not a path that could have
        # been redirected by a concurrent symlink replacement after validation.
        yield source, info
