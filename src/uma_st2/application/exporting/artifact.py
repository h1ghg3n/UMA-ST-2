"""Provider-independent generated export artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from uma_st2.shared import normalize_utc_datetime


@dataclass(frozen=True, slots=True)
class ExportArtifact:
    """Caller-owned immutable bytes plus bounded artifact provenance."""

    filename: str
    media_type: str
    content: bytes
    sha256_hex: str
    generated_at: datetime
    source_cutoff: datetime
    schema_version: str
    projection_version: str
    scope_type: str
    scope_id: str
    scope_name: str
    row_count: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.filename, str)
            or not self.filename
            or self.filename in {".", ".."}
            or any(character in self.filename for character in ("/", "\\"))
            or any(ord(character) < 32 for character in self.filename)
        ):
            raise ValueError("filename must be a safe basename.")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValueError("media_type must be non-empty.")
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("content must be non-empty bytes.")
        expected_hash = sha256(self.content).hexdigest()
        if self.sha256_hex != expected_hash:
            raise ValueError("sha256_hex does not match content.")
        object.__setattr__(
            self,
            "generated_at",
            normalize_utc_datetime(self.generated_at, field_name="generated_at"),
        )
        object.__setattr__(
            self,
            "source_cutoff",
            normalize_utc_datetime(self.source_cutoff, field_name="source_cutoff"),
        )
        for field_name in (
            "schema_version",
            "projection_version",
            "scope_type",
            "scope_id",
            "scope_name",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty.")
        if isinstance(self.row_count, bool) or not isinstance(self.row_count, int) or self.row_count < 0:
            raise ValueError("row_count must be a non-negative integer.")
