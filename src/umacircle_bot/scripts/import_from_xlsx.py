import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from umacircle_bot.sheets.legacy_room_report import build_legacy_room_dry_run_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate legacy Umacircle XLSX data without writing to the DB")
    parser.add_argument("workbook", type=Path, help="path to the legacy room-match XLSX/XLSM workbook")
    parser.add_argument("--output", type=Path, help="optional UTF-8 JSON report path; defaults to stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_legacy_room_dry_run_report(args.workbook)
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is None:
        print(payload)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{payload}\n", encoding="utf-8")
    return 2 if report.has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
