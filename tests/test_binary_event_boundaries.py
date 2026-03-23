from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "abx_plugins" / "plugins"
FORBIDDEN_TOKENS = (
    r"\bBinary\(",
    r"\bBinProvider\(",
    r"\bEnvProvider\(",
    r"\bNpmProvider\(",
    r"\bPipProvider\(",
    r"\bBrewProvider\(",
    r"\bAptProvider\(",
    r"\bfrom abx_pkg\b",
    r"\bimport abx_pkg\b",
    r"\bshutil\.which\(",
    r"\bemit_installed_binary_record\([^)]*abspath=",
    r"\bemit_installed_binary_record\([^)]*binprovider=",
)


def _iter_non_test_hook_files() -> list[Path]:
    files: list[Path] = []
    for path in PLUGINS_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".py", ".js", ".sh"}:
            continue
        rel_parts = path.relative_to(PLUGINS_ROOT).parts
        if "tests" in rel_parts:
            continue
        if path.name.startswith("on_BinaryRequest__"):
            continue
        if not path.name.startswith("on_"):
            continue
        files.append(path)
    return files


def _iter_non_test_plugin_source_files() -> list[Path]:
    files: list[Path] = []
    for path in PLUGINS_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".py", ".js", ".sh"}:
            continue
        rel_parts = path.relative_to(PLUGINS_ROOT).parts
        if "tests" in rel_parts:
            continue
        files.append(path)
    return files


def test_non_binary_hooks_only_emit_binary_events() -> None:
    failures: list[str] = []

    for path in _iter_non_test_hook_files():
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(PLUGINS_ROOT)
        for pattern in FORBIDDEN_TOKENS:
            match = re.search(pattern, source)
            if match:
                failures.append(f"{rel}:{match.start()} matched /{pattern}/")

    assert not failures, (
        "Non-on_BinaryRequest hooks must not instantiate abx_pkg Binary/provider objects "
        "or import abx_pkg directly. They should emit BinaryRequest events and let the "
        "on_BinaryRequest hooks resolve/install them:\n" + "\n".join(failures)
    )


def test_no_shutil_which_outside_tests() -> None:
    failures: list[str] = []

    for path in _iter_non_test_plugin_source_files():
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(PLUGINS_ROOT)
        match = re.search(r"\bshutil\.which\(", source)
        if match:
            failures.append(f"{rel}:{match.start()} matched /\\bshutil\\.which\\(/")

    assert not failures, (
        "shutil.which() is banned in non-test abx_plugins code. "
        "Use abx-pkg-backed resolution helpers instead:\n" + "\n".join(failures)
    )
