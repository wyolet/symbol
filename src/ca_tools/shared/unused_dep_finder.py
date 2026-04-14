"""Unused dependency detection — find deps declared but never imported."""

import ast
from pathlib import Path

from ca_tools.shared.ast_cache import ASTCache
from ca_tools.shared.files import collect_py_files
from ca_tools.shared.spec import Spec

from ca_tools.shared.pkg_registry import normalize_package_name


def _collect_all_imports(
    project_root: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    cache: ASTCache | None = None,
) -> set[str]:
    all_imports: set[str] = set()

    if cache:
        files = cache.files
    else:
        files = collect_py_files(project_root, include, exclude)

    for py_file in files:
        if cache:
            tree = cache.get_ast(py_file)
        else:
            try:
                source = py_file.read_text()
                tree = ast.parse(source, filename=str(py_file))
            except (SyntaxError, UnicodeDecodeError, OSError):
                tree = None

        if tree is None:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    all_imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    all_imports.add(node.module.split(".")[0])

    return all_imports


def _dep_to_import_name(dep: str, spec: Spec) -> str:
    normalized = normalize_package_name(dep)
    pkg_info = spec.packages.get(normalized)
    if pkg_info and pkg_info.import_name:
        return pkg_info.import_name.split(".")[0]
    return normalized.replace("-", "_")


def detect_unused_deps(
    project_root: Path,
    deps: list[str],
    spec: Spec,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    cache: ASTCache | None = None,
) -> list[str]:
    all_imports = _collect_all_imports(project_root, include, exclude, cache)
    unused: list[str] = []

    for dep in deps:
        import_name = _dep_to_import_name(dep, spec)
        if import_name not in all_imports:
            unused.append(dep)

    return unused
