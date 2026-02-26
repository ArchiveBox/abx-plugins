from __future__ import annotations

import ast
from pathlib import Path


PLUGINS_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_IMPORT_ROOTS = ("archivebox", "django")


def _is_forbidden_import(module_name: str) -> bool:
    return any(
        module_name == forbidden or module_name.startswith(f"{forbidden}.")
        for forbidden in FORBIDDEN_IMPORT_ROOTS
    )


def _is_allowlisted_path(path: Path) -> bool:
    rel_parts = path.relative_to(PLUGINS_ROOT).parts
    top_level_dir = rel_parts[0] if rel_parts else ""
    if top_level_dir.startswith("search_backend_"):
        return True
    return any("ldap" in part.lower() for part in rel_parts)


def _iter_non_test_plugin_python_files() -> list[Path]:
    files: list[Path] = []
    for path in PLUGINS_ROOT.rglob("*.py"):
        rel_parts = path.relative_to(PLUGINS_ROOT).parts
        if "tests" in rel_parts:
            continue
        files.append(path)
    return files


def _collect_forbidden_imports(path: Path) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_import(alias.name):
                    violations.append((node.lineno, alias.name))

        elif isinstance(node, ast.ImportFrom):
            if node.module and _is_forbidden_import(node.module):
                violations.append((node.lineno, node.module))

        elif isinstance(node, ast.Call):
            if not node.args:
                continue
            first_arg = node.args[0]
            if not isinstance(first_arg, ast.Constant) or not isinstance(first_arg.value, str):
                continue

            if isinstance(node.func, ast.Name) and node.func.id == "__import__":
                if _is_forbidden_import(first_arg.value):
                    violations.append((node.lineno, first_arg.value))

            if isinstance(node.func, ast.Attribute) and node.func.attr == "import_module":
                if _is_forbidden_import(first_arg.value):
                    violations.append((node.lineno, first_arg.value))

    return violations


def test_plugin_dependency_boundaries() -> None:
    """Guard plugin boundaries by banning archivebox/django imports outside explicit allowlist paths."""
    failures: list[str] = []

    for path in _iter_non_test_plugin_python_files():
        if _is_allowlisted_path(path):
            continue
        for lineno, module_name in _collect_forbidden_imports(path):
            rel = path.relative_to(PLUGINS_ROOT)
            failures.append(f"{rel}:{lineno} imports {module_name!r}")

    assert not failures, (
        "Forbidden dependency imports detected. "
        "Only search backends and ldap-related paths may import archivebox/django:\n"
        + "\n".join(failures)
    )
