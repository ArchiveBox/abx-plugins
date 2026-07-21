from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import TypedDict


CHROMIUM_SHARD_COUNT = 4
STANDARD_SHARD_COUNT = 4


class TestShard(TypedDict):
    name: str
    paths: list[str]
    needs_chromium: bool


def discover_tests() -> list[Path]:
    tests = {
        *Path("abx_plugins/plugins").glob("*/tests/test_*.py"),
        *Path("abx_plugins/plugins").glob("*/test_*.py"),
        *Path("tests").glob("**/test_*.py"),
    }
    if not tests:
        raise SystemExit("No test files were discovered")
    return sorted(tests)


def needs_chromium(test_path: Path) -> bool:
    parts = test_path.parts
    if parts[:2] == ("abx_plugins", "plugins") and "tests" in parts:
        plugin_dir = Path(*parts[:3])
        config = json.loads((plugin_dir / "config.json").read_text(encoding="utf-8"))
        return plugin_dir.name == "chrome" or "chrome" in config.get(
            "required_plugins",
            [],
        )
    return "chrom" in test_path.read_text(encoding="utf-8").lower()


def test_weight(test_path: Path) -> int:
    return len(test_path.read_text(encoding="utf-8").splitlines())


def build_shards(
    test_paths: list[Path],
    *,
    count: int,
    prefix: str,
    chromium: bool,
) -> list[TestShard]:
    shard_count = min(count, len(test_paths))
    shards: list[list[Path]] = [[] for _ in range(shard_count)]
    shard_weights = [0] * shard_count

    for test_path in sorted(
        test_paths,
        key=lambda path: (-test_weight(path), str(path)),
    ):
        shard_index = min(
            range(shard_count),
            key=lambda index: (shard_weights[index], index),
        )
        shards[shard_index].append(test_path)
        shard_weights[shard_index] += test_weight(test_path)

    return [
        {
            "name": f"{prefix}-{index + 1}",
            "paths": [str(path) for path in sorted(shard)],
            "needs_chromium": chromium,
        }
        for index, shard in enumerate(shards)
    ]


if __name__ == "__main__":
    all_tests = discover_tests()
    chromium_tests = [test_path for test_path in all_tests if needs_chromium(test_path)]
    standard_tests = [
        test_path for test_path in all_tests if test_path not in chromium_tests
    ]
    test_shards = [
        *build_shards(
            chromium_tests,
            count=CHROMIUM_SHARD_COUNT,
            prefix="chromium",
            chromium=True,
        ),
        *build_shards(
            standard_tests,
            count=STANDARD_SHARD_COUNT,
            prefix="standard",
            chromium=False,
        ),
    ]

    assigned = Counter(
        Path(test_path) for shard in test_shards for test_path in shard["paths"]
    )
    if assigned != Counter(all_tests):
        raise SystemExit(
            "Test shards must contain every discovered test file exactly once",
        )

    print(f"test-shards={json.dumps(test_shards)}")
