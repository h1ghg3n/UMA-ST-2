from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src"
PACKAGE_ROOT = SOURCE_ROOT / "uma_st2"


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    return imported


def _matches_prefix(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(f"{prefix}.")


@pytest.mark.parametrize(
    "path",
    _python_files(PACKAGE_ROOT / "domain"),
    ids=lambda path: path.relative_to(PROJECT_ROOT).as_posix(),
)
def test_domain_does_not_import_outer_layers(path: Path) -> None:
    forbidden = (
        "uma_st2.application",
        "uma_st2.adapters",
        "uma_st2.infrastructure",
        "sqlalchemy",
        "discord",
    )

    violations = sorted(
        module for module in _imported_modules(path) if any(_matches_prefix(module, prefix) for prefix in forbidden)
    )

    assert violations == []


@pytest.mark.parametrize(
    "path",
    _python_files(PACKAGE_ROOT / "application"),
    ids=lambda path: path.relative_to(PROJECT_ROOT).as_posix(),
)
def test_application_does_not_import_adapters_or_concrete_infrastructure(path: Path) -> None:
    forbidden = (
        "uma_st2.adapters",
        "uma_st2.infrastructure",
        "sqlalchemy",
        "discord",
    )

    violations = sorted(
        module for module in _imported_modules(path) if any(_matches_prefix(module, prefix) for prefix in forbidden)
    )

    assert violations == []


@pytest.mark.parametrize(
    "path",
    _python_files(PACKAGE_ROOT / "adapters"),
    ids=lambda path: path.relative_to(PROJECT_ROOT).as_posix(),
)
def test_adapters_do_not_import_concrete_persistence(path: Path) -> None:
    forbidden = (
        "uma_st2.infrastructure",
        "sqlalchemy",
    )

    violations = sorted(
        module for module in _imported_modules(path) if any(_matches_prefix(module, prefix) for prefix in forbidden)
    )

    assert violations == []


@pytest.mark.parametrize(
    "path",
    _python_files(PACKAGE_ROOT / "infrastructure"),
    ids=lambda path: path.relative_to(PROJECT_ROOT).as_posix(),
)
def test_infrastructure_does_not_import_inbound_adapters(path: Path) -> None:
    violations = sorted(module for module in _imported_modules(path) if _matches_prefix(module, "uma_st2.adapters"))

    assert violations == []


def test_v1_package_is_not_an_active_v2_source_package() -> None:
    assert not (SOURCE_ROOT / "umacircle_bot").exists()
