from __future__ import annotations

import ast
import configparser
import shlex
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = PROJECT_ROOT / "tests"
INTEGRATION_ROOT = TEST_ROOT / "integration"
PYTEST_CONFIG = PROJECT_ROOT / "pytest.ini"

BOUNDARY_MARKERS = {"historical", "integration", "jetson"}
COMMON_MARIADB_BOOTSTRAP_NAMES = {
    "_database_url",
    "_test_database_url",
    "_upgrade",
    "migrated_engine",
}
WIN5_DISCORD_TEST_OWNERS = {
    "test_win5_member_history_discord.py",
    "test_win5_member_reads_discord.py",
    "test_win5_member_submission_discord.py",
    "test_win5_staff_result_discord.py",
    "test_win5_staff_round_creation_discord.py",
    "test_win5_staff_round_lifecycle_discord.py",
    "test_win5_staff_season_discord.py",
}
WIN5_DISCORD_SUPPORT_OWNERS = {
    "win5_member_discord_test_support.py",
    "win5_staff_discord_test_support.py",
}
LEGACY_WIN5_DISCORD_MONOLITHS = {
    "test_win5_member_discord.py",
    "test_win5_staff_discord.py",
}
WIN5_MEMBER_QUERY_TEST_OWNERS = {
    "test_win5_member_read_queries.py",
    "test_win5_member_read_query_database.py",
    "test_win5_member_submission_queries.py",
    "test_win5_member_submission_query_database.py",
}
WIN5_MEMBER_QUERY_SUPPORT_OWNER = "win5_member_query_test_support.py"
LEGACY_WIN5_MEMBER_QUERY_MONOLITH = "test_win5_member_queries.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _top_level_definition_names(path: Path) -> set[str]:
    return {
        node.name
        for node in _tree(path).body
        if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef))
    }


def _pytest_marker_name(node: ast.AST) -> str | None:
    candidate = node.func if isinstance(node, ast.Call) else node
    if not isinstance(candidate, ast.Attribute):
        return None
    mark = candidate.value
    if not isinstance(mark, ast.Attribute) or mark.attr != "mark":
        return None
    if not isinstance(mark.value, ast.Name) or mark.value.id != "pytest":
        return None
    return candidate.attr


def _module_markers(path: Path) -> set[str]:
    markers: set[str] = set()
    for node in _tree(path).body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets):
            continue
        values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else (node.value,)
        markers.update(marker for value in values if (marker := _pytest_marker_name(value)) is not None)
    return markers


def _all_markers(path: Path) -> set[str]:
    return {marker for node in ast.walk(_tree(path)) if (marker := _pytest_marker_name(node)) is not None}


def test_pytest_configuration_fixes_collection_and_marker_governance() -> None:
    parser = configparser.ConfigParser()
    parser.read(PYTEST_CONFIG, encoding="utf-8")
    pytest_config = parser["pytest"]

    assert pytest_config.get("minversion") == "8.0"
    assert pytest_config.get("testpaths", "").split() == ["tests"]
    assert pytest_config.get("pythonpath", "").split() == ["src"]
    assert set(shlex.split(pytest_config.get("addopts", ""))) >= {
        "-p",
        "no:cacheprovider",
        "--strict-config",
        "--strict-markers",
    }
    assert pytest_config.get("tmp_path_retention_count") == "0"
    assert pytest_config.get("tmp_path_retention_policy") == "none"

    registered_markers = {
        line.strip().split(":", 1)[0] for line in pytest_config.get("markers", "").splitlines() if line.strip()
    }
    assert registered_markers == BOUNDARY_MARKERS


def test_integration_modules_own_the_integration_marker() -> None:
    missing = [
        path.name for path in sorted(INTEGRATION_ROOT.glob("test_*.py")) if "integration" not in _module_markers(path)
    ]
    assert missing == []


def test_fast_and_architecture_modules_do_not_claim_external_lane_markers() -> None:
    violations = {
        path.relative_to(PROJECT_ROOT).as_posix(): sorted(_all_markers(path) & BOUNDARY_MARKERS)
        for tier in ("architecture", "unit")
        for path in sorted((TEST_ROOT / tier).glob("test_*.py"))
        if _all_markers(path) & BOUNDARY_MARKERS
    }
    assert violations == {}


def test_mariadb_bootstrap_has_one_physical_owner() -> None:
    duplicates = {
        path.name: sorted(_top_level_definition_names(path) & COMMON_MARIADB_BOOTSTRAP_NAMES)
        for path in sorted(INTEGRATION_ROOT.glob("test_*.py"))
        if _top_level_definition_names(path) & COMMON_MARIADB_BOOTSTRAP_NAMES
    }
    assert duplicates == {}

    assert "migrated_engine" in _top_level_definition_names(INTEGRATION_ROOT / "conftest.py")
    assert {"require_test_database_url", "upgrade_to_head"} <= _top_level_definition_names(
        INTEGRATION_ROOT / "mariadb_test_support.py"
    )


def test_win5_discord_tests_keep_capability_owners_and_support_separate() -> None:
    unit_root = TEST_ROOT / "unit"

    assert {path.name for path in unit_root.glob("test_win5_*_discord.py")} >= WIN5_DISCORD_TEST_OWNERS
    assert not {name for name in LEGACY_WIN5_DISCORD_MONOLITHS if (unit_root / name).exists()}

    for filename in WIN5_DISCORD_SUPPORT_OWNERS:
        path = unit_root / filename
        assert path.is_file()
        hidden_tests = sorted(name for name in _top_level_definition_names(path) if name.startswith("test_"))
        assert hidden_tests == []


def test_win5_member_query_tests_keep_capability_and_layer_owners_separate() -> None:
    unit_root = TEST_ROOT / "unit"

    assert {path.name for path in unit_root.glob("test_win5_member_*quer*.py")} >= WIN5_MEMBER_QUERY_TEST_OWNERS
    assert not (unit_root / LEGACY_WIN5_MEMBER_QUERY_MONOLITH).exists()

    support = unit_root / WIN5_MEMBER_QUERY_SUPPORT_OWNER
    assert support.is_file()
    hidden_tests = sorted(name for name in _top_level_definition_names(support) if name.startswith("test_"))
    assert hidden_tests == []
