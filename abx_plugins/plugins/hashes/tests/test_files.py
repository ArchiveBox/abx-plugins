"""Exercise the shared helper on real independent files and the current filesystem."""

import errno
import struct
import sys
from pathlib import Path

from abx_plugins.plugins.hashes.files import deduplicate_files, sha256_file


def test_native_deduplication_preserves_independent_outputs(tmp_path):
    original = Path(__file__).parents[1] / "config.json"
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    first.write_bytes(original.read_bytes())
    second.write_bytes(original.read_bytes())
    digest = sha256_file(first)
    identities = [(path.stat().st_dev, path.stat().st_ino) for path in (first, second)]
    assert identities[0] != identities[1]
    result = deduplicate_files(
        tmp_path,
        [
            {"path": path.name, "hash": digest, "size": path.stat().st_size}
            for path in (first, second)
        ],
    )
    assert result["supported_platform"] == (sys.platform == "linux")
    if sys.platform == "linux":
        assert result["attempted_files"] == 1
        assert result["failed_files"] in (0, 1)
        assert result["extent_bytes_verified"] == (
            0 if result["failed_files"] else first.stat().st_size
        )
    else:
        assert result == {
            "supported_platform": False,
            "attempted_files": 0,
            "extent_bytes_verified": 0,
            "failed_files": 0,
        }
    assert [
        (path.stat().st_dev, path.stat().st_ino) for path in (first, second)
    ] == identities
    assert first.read_bytes() == second.read_bytes() == original.read_bytes()
    second.write_bytes(b"independent later write")
    assert first.read_bytes() == original.read_bytes()


def test_native_deduplication_stops_on_unsupported_device(tmp_path):
    data = (Path(__file__).parents[1] / "config.json").read_bytes()
    paths = [tmp_path / f"{name}.json" for name in ("first", "second", "third")]
    for path in paths:
        path.write_bytes(data)
    digest = sha256_file(paths[0])

    unsupported = False
    if sys.platform == "linux":
        import fcntl

        # No destinations: the kernel checks the source file operation without
        # sharing or changing extents.
        with paths[0].open("rb") as source:
            try:
                fcntl.ioctl(
                    source,
                    0xC0189436,
                    bytearray(struct.pack("=QQHHI", 0, 1, 0, 0, 0)),
                    True,
                )
            except OSError as exc:
                unsupported = exc.errno == errno.EOPNOTSUPP

    result = deduplicate_files(
        tmp_path,
        [{"path": path.name, "hash": digest, "size": len(data)} for path in paths],
    )
    expected_attempts = (1 if unsupported else 2) if sys.platform == "linux" else 0
    assert result["attempted_files"] == expected_attempts
    if unsupported:
        assert result["failed_files"] == 1
        assert result["extent_bytes_verified"] == 0
    assert all(path.read_bytes() == data for path in paths)
