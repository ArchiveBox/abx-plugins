"""Exercise the shared helper on real independent files and the current filesystem."""

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
