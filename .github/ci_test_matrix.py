from __future__ import annotations

import json
import os
from collections import Counter
from itertools import cycle
from pathlib import Path
from typing import TypedDict

SUPPORTED_CELLS = (
    ("ubuntu-24.04", "3.12"),
    ("ubuntu-24.04", "3.13"),
    ("ubuntu-24.04", "3.14"),
    ("macos-15", "3.12"),
    ("macos-15", "3.13"),
    ("macos-15", "3.14"),
)
REPO_ROOT = Path(__file__).resolve().parent.parent


class TestMatrixItem(TypedDict):
    path: str
    paths: list[str]
    os: str
    python: str
    ugnas: bool


def discover_tests() -> list[Path]:
    tests = {
        *(REPO_ROOT / "abx_plugins/plugins").rglob("test_*.py"),
        *(REPO_ROOT / "tests").rglob("test_*.py"),
    }
    if not tests:
        raise SystemExit("No test files were discovered")
    return sorted(path.relative_to(REPO_ROOT) for path in tests)


if __name__ == "__main__":
    all_tests = discover_tests()
    targets = cycle(SUPPORTED_CELLS)
    test_matrix: list[TestMatrixItem] = []
    cells_used: set[tuple[str, str]] = set()
    durations = json.loads((REPO_ROOT / ".github/test-durations.json").read_text())[
        "seconds"
    ]
    # Ordinary Linux tests can use either runner. Genuine exceptions declare
    # a # ci-runner: hosted header in the first five lines of the test file.
    hosted = {
        str(path)
        for path in all_tests
        if "# ci-runner: hosted" in (REPO_ROOT / path).read_text().splitlines()[:5]
    }
    short_tests: dict[tuple[str, str, bool], list[str]] = {}

    for test_path in all_tests:
        os_name, python_version = next(targets)
        cells_used.add((os_name, python_version))
        path = str(test_path)
        if durations.get(path, 60) < 60:
            short_tests.setdefault(
                (os_name, python_version, path in hosted),
                [],
            ).append(path)
            continue
        test_matrix.append(
            {
                "path": path,
                "paths": [path],
                "os": os_name,
                "python": python_version,
                "ugnas": False,
            },
        )

    # Keep the original OS/Python assignment and a separate pytest process per
    # file. Only measured short files share checkout and dependency setup.
    for (os_name, python_version, _), paths in short_tests.items():
        batch: list[str] = []
        batch_seconds = 0
        for path in paths:
            if batch and (len(batch) == 8 or batch_seconds + durations[path] > 180):
                test_matrix.append(
                    {
                        "path": f"batch/{batch[0]}",
                        "paths": batch,
                        "os": os_name,
                        "python": python_version,
                        "ugnas": False,
                    },
                )
                batch = []
                batch_seconds = 0
            batch.append(path)
            batch_seconds += durations[path]
        if batch:
            test_matrix.append(
                {
                    "path": f"batch/{batch[0]}",
                    "paths": batch,
                    "os": os_name,
                    "python": python_version,
                    "ugnas": False,
                },
            )

    eligible = [
        item
        for item in test_matrix
        if item["os"] == "ubuntu-24.04"
        and not any(path in hosted for path in item["paths"])
    ]
    capacity = int(os.environ.get("UGNAS_CI_MAX_JOBS", "3"))
    for item in sorted(
        eligible,
        key=lambda item: sum(durations.get(path, 60) for path in item["paths"]),
        reverse=True,
    )[:capacity]:
        item["ugnas"] = True

    assigned = Counter(Path(path) for item in test_matrix for path in item["paths"])
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
