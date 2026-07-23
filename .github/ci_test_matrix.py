from __future__ import annotations

import json
from collections import Counter
from itertools import cycle
from pathlib import Path
from typing import TypedDict


SUPPORTED_CELLS = (
    ("ubuntu-24.04", "3.12.10"),
    ("ubuntu-24.04", "3.13.13"),
    ("ubuntu-24.04", "3.14.5"),
    ("macos-15", "3.12.10"),
    ("macos-15", "3.13.13"),
    ("macos-15", "3.14.5"),
)


class TestMatrixItem(TypedDict):
    path: str
    os: str
    python: str


def discover_tests() -> list[Path]:
    tests = {
        *Path("abx_plugins/plugins").rglob("test_*.py"),
        *Path("tests").rglob("test_*.py"),
    }
    if not tests:
        raise SystemExit("No test files were discovered")
    return sorted(tests)


if __name__ == "__main__":
    all_tests = discover_tests()
    targets = cycle(SUPPORTED_CELLS)
    test_matrix: list[TestMatrixItem] = []
    cells_used: set[tuple[str, str]] = set()

    for test_path in all_tests:
        os_name, python_version = next(targets)
        cells_used.add((os_name, python_version))
        test_matrix.append(
            {
                "path": str(test_path),
                "os": os_name,
                "python": python_version,
            },
        )

    assigned = Counter(Path(item["path"]) for item in test_matrix)
    if assigned != Counter(all_tests):
        raise SystemExit(
            "Test matrix must contain every discovered test file exactly once",
        )
    if cells_used != set(SUPPORTED_CELLS):
        raise SystemExit("Tests must cover every supported OS/Python cell")
    if any(
        item["os"] not in {"ubuntu-24.04", "macos-15"}
        or (item["os"], item["python"]) not in SUPPORTED_CELLS
        for item in test_matrix
    ):
        raise SystemExit("Every test must use one supported OS/Python cell")

    print(f"test-matrix={json.dumps(test_matrix)}")
