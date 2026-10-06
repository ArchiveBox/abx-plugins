from __future__ import annotations

import ast
import pytest
from importlib.util import resolve_name
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "abx_plugins" / "plugins"


def _is_forbidden_import(module_name: str, forbidden_roots: tuple[str, ...]) -> bool:
    return any(
        module_name == forbidden or module_name.startswith(f"{forbidden}.")
        for forbidden in forbidden_roots
    )


def _iter_non_test_plugin_python_files() -> list[Path]:
    files: list[Path] = []
    for path in PLUGINS_ROOT.rglob("*.py"):
        rel_parts = path.relative_to(PLUGINS_ROOT).parts
        if "tests" in rel_parts:
            continue
        files.append(path)
    return files


def _collect_forbidden_imports(
    path: Path,
    forbidden_roots: tuple[str, ...],
) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_import(alias.name, forbidden_roots):
                    violations.append((node.lineno, alias.name))

        elif isinstance(node, ast.ImportFrom):
            package = ".".join(path.relative_to(REPO_ROOT).parts[:-1])
            module = (
                resolve_name("." * node.level + (node.module or ""), package)
                if node.level
                else node.module or ""
            )
            for imported in (
                module,
                *(f"{module}.{alias.name}" for alias in node.names),
            ):
                if _is_forbidden_import(imported, forbidden_roots):
                    violations.append((node.lineno, imported))

        elif isinstance(node, ast.Call):
            if not node.args:
                continue
            first_arg = node.args[0]
            if not isinstance(first_arg, ast.Constant) or not isinstance(
                first_arg.value,
                str,
            ):
                continue

            if isinstance(node.func, ast.Name) and node.func.id == "__import__":
                if _is_forbidden_import(first_arg.value, forbidden_roots):
                    violations.append((node.lineno, first_arg.value))

            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
            ):
                if _is_forbidden_import(first_arg.value, forbidden_roots):
                    violations.append((node.lineno, first_arg.value))

    return violations


@pytest.fixture
def plugin_dependency_violations():
    integrations = tuple(
        f"abx_plugins.plugins.{path.parent.name}.archivebox"
        for path in PLUGINS_ROOT.glob("*/archivebox")
        if path.is_dir()
    )
    failures: list[str] = []
    for path in _iter_non_test_plugin_python_files():
        rel = path.relative_to(PLUGINS_ROOT)
        is_host_adapter = len(rel.parts) > 2 and rel.parts[1] == "archivebox"
        forbidden = (
            ("abx_dl",)
            if is_host_adapter
            else ("archivebox", "abx_dl", "django", *integrations)
        )
        for lineno, module_name in _collect_forbidden_imports(path, forbidden):
            failures.append(f"{rel}:{lineno} imports {module_name!r}")
    return failures
