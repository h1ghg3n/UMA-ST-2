import json
from collections.abc import Sequence
from hashlib import sha256
from typing import TypeAlias

SOURCE_IDENTIFIER_MAX_LENGTH = 200
SHEET_NAME_MAX_LENGTH = 100
SHA256_HEX_LENGTH = 64
ImportFingerprintValue: TypeAlias = str | int | bool | None


def build_sheet_import_source_key(
    *,
    source_identifier: str,
    sheet_name: str,
    row_number: int,
) -> str:
    normalized_source = normalize_import_source_identifier(source_identifier)
    normalized_sheet = normalize_import_sheet_name(sheet_name)
    normalized_row = normalize_source_row_number(row_number)
    canonical = json.dumps(
        [normalized_source, normalized_sheet, normalized_row],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def build_import_row_fingerprint(values: Sequence[ImportFingerprintValue]) -> str:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("fingerprint values must be a sequence")
    if any(not isinstance(value, (str, int, bool, type(None))) for value in values):
        raise ValueError("fingerprint contains an unsupported value")
    canonical = json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def normalize_import_source_identifier(value: str) -> str:
    return _normalize_printable_text(
        value,
        field_name="source_identifier",
        max_length=SOURCE_IDENTIFIER_MAX_LENGTH,
    )


def normalize_import_sheet_name(value: str) -> str:
    return _normalize_printable_text(
        value,
        field_name="sheet_name",
        max_length=SHEET_NAME_MAX_LENGTH,
    )


def normalize_source_row_number(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("source row number must be a positive integer")
    return value


def normalize_sha256_hex(value: str, *, field_name: str = "SHA-256 value") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    normalized = value.strip().lower()
    if (
        len(normalized) != SHA256_HEX_LENGTH
        or not normalized.isascii()
        or any(character not in "0123456789abcdef" for character in normalized)
    ):
        raise ValueError(f"{field_name} must be 64 hexadecimal characters")
    return normalized


def _normalize_printable_text(value: str, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length or any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{field_name} must contain 1 to {max_length} printable characters")
    return normalized
