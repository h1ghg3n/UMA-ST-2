"""Guard Match staff View replacement against discord.py callback eviction."""

from __future__ import annotations

import ast
from pathlib import Path

DISCORD_ADAPTER_ROOT = Path("src/uma_st2/adapters/discord")


def _is_non_terminal_view_keyword(keyword: ast.keyword) -> bool:
    return keyword.arg == "view" and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None)


def _is_source_stop(call: ast.Call) -> bool:
    if not isinstance(call.func, ast.Attribute) or call.func.attr != "stop":
        return False
    owner = call.func.value
    return (isinstance(owner, ast.Name) and owner.id == "source_view") or (
        isinstance(owner, ast.Name) and owner.id == "self"
    )


def _contains_source_stop(node: ast.AST) -> bool:
    return any(isinstance(child, ast.Call) and _is_source_stop(child) for child in ast.walk(node))


def _has_preceding_source_stop(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    child: ast.AST = call
    while parent := parents.get(child):
        for _, value in ast.iter_fields(parent):
            if isinstance(value, list) and child in value:
                position = value.index(child)
                if any(_contains_source_stop(statement) for statement in value[:position]):
                    return True
        child = parent
    return False


def test_match_staff_view_replacements_stop_the_source_before_registration() -> None:
    """The source stop must dominate each replacement in its concrete statement block."""

    failures: list[str] = []
    for path in sorted(DISCORD_ADAPTER_ROOT.glob("match_staff*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for function in (node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)):
            parameter_names = {
                argument.arg
                for argument in (
                    *function.args.posonlyargs,
                    *function.args.args,
                    *function.args.kwonlyargs,
                )
            }
            replacement_calls = sorted(
                (
                    call
                    for call in ast.walk(function)
                    if isinstance(call, ast.Call)
                    and any(_is_non_terminal_view_keyword(keyword) for keyword in call.keywords)
                ),
                key=lambda call: call.lineno,
            )
            owns_source = "source_view" in parameter_names or any(
                _is_source_stop(call) for call in ast.walk(function) if isinstance(call, ast.Call)
            )
            if not owns_source or not replacement_calls:
                continue
            for replacement_call in replacement_calls:
                if not _has_preceding_source_stop(replacement_call, parents):
                    failures.append(f"{path}:{function.name}:{replacement_call.lineno}")

    assert failures == [], "source View must stop before replacement registration: " + ", ".join(failures)
