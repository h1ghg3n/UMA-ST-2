from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict

from sqlalchemy.exc import SQLAlchemyError

from umacircle_bot.db.session import SessionLocal, configure_session, dispose_session_engine
from umacircle_bot.domain.errors import PersonaLinkConsoleError
from umacircle_bot.services.persona_link_console import (
    PersonaLinkMutationCommand,
    apply_persona_link_mutation,
    inspect_persona_link,
    preview_persona_link_mutation,
)

_ACCOUNT_ACTIONS = {
    "discord_attach",
    "discord_detach",
    "discord_transfer",
    "game_attach",
    "game_detach",
    "game_transfer",
}
_MUTATION_ACTIONS = sorted(_ACCOUNT_ACTIONS | {"set_display_name", "restore_display_name_source"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect or safely correct current Persona account links")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="Inspect one Persona without writes")
    inspect_parser.add_argument("--persona-id", required=True)

    mutate_parser = subparsers.add_parser(
        "mutate", help="Preview by default; use --apply to commit one audited correction"
    )
    mutate_parser.add_argument("action", choices=_MUTATION_ACTIONS)
    mutate_parser.add_argument("--persona-id", required=True, help="Target Persona UUID")
    mutate_parser.add_argument("--actor", required=True, help="Operator identity recorded in the audit")
    mutate_parser.add_argument("--operation-id", required=True, help="Stable idempotency ID for this correction")
    mutate_parser.add_argument("--reason", required=True, help="Non-empty audit reason")
    mutate_parser.add_argument(
        "--expected-current-persona-id",
        help="Current owner UUID, or 'none' when the account is expected to be unlinked",
    )
    mutate_parser.add_argument("--discord-account-id", type=int)
    mutate_parser.add_argument("--game-account-id", type=int)
    mutate_parser.add_argument("--display-name")
    mutate_parser.add_argument("--apply", action="store_true", help="Commit the audited correction; default is dry-run")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        configure_session()
        with SessionLocal() as session:
            if args.command == "inspect":
                result = inspect_persona_link(session, persona_id=args.persona_id)
                print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
                return 0
            command = _mutation_command(args)
            if args.apply:
                result = apply_persona_link_mutation(session, command=command)
                session.commit()
                mode = "apply"
            else:
                result = preview_persona_link_mutation(session, command=command)
                mode = "dry-run"
            print(json.dumps({"mode": mode, **asdict(result)}, ensure_ascii=False, sort_keys=True))
            return 0
    except (PersonaLinkConsoleError, ValueError) as exc:
        return _write_error(str(exc))
    except SQLAlchemyError:
        return _write_error("database operation failed")
    finally:
        dispose_session_engine()


def _mutation_command(args: argparse.Namespace) -> PersonaLinkMutationCommand:
    if args.action in _ACCOUNT_ACTIONS and args.expected_current_persona_id is None:
        raise PersonaLinkConsoleError("--expected-current-persona-id is required for account link mutations")
    return PersonaLinkMutationCommand(
        action=args.action,
        target_persona_id=args.persona_id,
        actor=args.actor,
        operation_id=args.operation_id,
        reason=args.reason,
        expected_current_persona_id=_persona_or_none(args.expected_current_persona_id),
        discord_account_id=args.discord_account_id,
        game_account_id=args.game_account_id,
        display_name=args.display_name,
    )


def _persona_or_none(value: str | None) -> str | None:
    if value is None or value.strip().lower() == "none":
        return None
    return value


def _write_error(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
