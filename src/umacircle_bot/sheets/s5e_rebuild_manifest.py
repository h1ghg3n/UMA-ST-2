from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path

from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.domain.identity import validate_registration_pid

S5E_REBUILD_MANIFEST_VERSION = 1
S5E_REBUILD_MODE = "s5e_rebuild"

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class S5EWalletDelta:
    wallet_source_key: str
    delta: int


@dataclass(frozen=True, slots=True)
class S5EOwnerDelta:
    owner_business_key: str
    delta: int


@dataclass(frozen=True, slots=True)
class S5EParticipantDelta:
    discord_user_id: str
    uma_pid: str
    delta: int


@dataclass(frozen=True, slots=True)
class S5EPhaseA:
    artifact_file_checksum: str
    source_semantic_checksum: str
    business_key_prefix_checksum: str
    transaction_count: int
    bet_count: int
    judgement_count: int
    expected_delta: int
    expected_cumulative_total: int
    wallet_deltas: tuple[S5EWalletDelta, ...]


@dataclass(frozen=True, slots=True)
class S5EPhaseB:
    manifest_checksum: str
    target_attribution_checksum: str
    reward_policy_checksum: str
    transaction_count: int
    expected_delta: int
    expected_cumulative_total: int
    owner_deltas: tuple[S5EOwnerDelta, ...]


@dataclass(frozen=True, slots=True)
class S5EPhaseC:
    provenance_checksum: str
    transaction_count: int
    expected_delta: int
    expected_cumulative_total: int
    participant_deltas: tuple[S5EParticipantDelta, ...]


@dataclass(frozen=True, slots=True)
class S5EPhaseD:
    win5_manifest_checksum: str
    win5_file_checksum: str
    participant_checksum: str
    score_reward_checksum: str
    transaction_count: int
    expected_delta: int
    expected_cumulative_total: int
    expected_season_score: int
    expected_top1_score: int
    participant_deltas: tuple[S5EParticipantDelta, ...]


@dataclass(frozen=True, slots=True)
class S5ESourceLineage:
    source_identifier: str
    old_workbook_checksum: str
    latest_workbook_checksum: str


@dataclass(frozen=True, slots=True)
class S5EReviewedArtifacts:
    source_seed_basis_checksum: str
    source_seed_decision_checksum: str
    mapping_checksum: str
    mapping_decision_checksum: str
    rating_disposition_checksum: str
    rating_decision_checksum: str
    rating_signature_checksum: str


@dataclass(frozen=True, slots=True)
class S5ERebuildManifest:
    manifest_checksum: str
    file_checksum: str
    source_commit: str
    alembic_head: str
    source_lineage: S5ESourceLineage
    reviewed_artifacts: S5EReviewedArtifacts
    phase_a: S5EPhaseA
    phase_b: S5EPhaseB
    phase_c: S5EPhaseC
    phase_d: S5EPhaseD


def load_s5e_rebuild_manifest(path: Path) -> S5ERebuildManifest:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise LegacyImportError("S5E rebuild manifest cannot be read") from exc
    return parse_s5e_rebuild_manifest(payload)


def parse_s5e_rebuild_manifest(payload: bytes) -> S5ERebuildManifest:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyImportError("S5E rebuild manifest must be UTF-8 JSON") from exc
    root = _mapping(decoded, field="manifest")
    _exact_keys(
        root,
        {
            "manifest_version",
            "mode",
            "source_commit",
            "alembic_head",
            "source_lineage",
            "reviewed_artifacts",
            "phase_a",
            "phase_b",
            "phase_c",
            "phase_d",
            "manifest_checksum",
        },
        field="manifest",
    )
    if _integer(root["manifest_version"], field="manifest version") != S5E_REBUILD_MANIFEST_VERSION:
        raise LegacyImportError("S5E rebuild manifest version is not supported")
    if _text(root["mode"], field="manifest mode", maximum=64) != S5E_REBUILD_MODE:
        raise LegacyImportError("S5E rebuild manifest mode is not supported")

    stored_checksum = _sha256(root["manifest_checksum"], field="manifest checksum")
    checksum_payload = dict(root)
    checksum_payload.pop("manifest_checksum")
    calculated_checksum = canonical_checksum(checksum_payload)
    if stored_checksum != calculated_checksum:
        raise LegacyImportError("S5E rebuild manifest checksum does not match its content")

    source_lineage = _parse_source_lineage(root["source_lineage"])
    reviewed = _parse_reviewed_artifacts(root["reviewed_artifacts"])
    phase_a = _parse_phase_a(root["phase_a"])
    phase_b = _parse_phase_b(root["phase_b"])
    phase_c = _parse_phase_c(root["phase_c"])
    phase_d = _parse_phase_d(root["phase_d"])
    _validate_phase_arithmetic(phase_a, phase_b, phase_c, phase_d)
    return S5ERebuildManifest(
        manifest_checksum=stored_checksum,
        file_checksum=sha256(payload).hexdigest(),
        source_commit=_commit(root["source_commit"]),
        alembic_head=_text(root["alembic_head"], field="Alembic head", maximum=64),
        source_lineage=source_lineage,
        reviewed_artifacts=reviewed,
        phase_a=phase_a,
        phase_b=phase_b,
        phase_c=phase_c,
        phase_d=phase_d,
    )


def canonical_checksum(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def build_s5e_rebuild_manifest_document(
    *,
    source_commit: str,
    alembic_head: str,
    source_lineage: S5ESourceLineage,
    reviewed_artifacts: S5EReviewedArtifacts,
    phase_a: S5EPhaseA,
    phase_b: S5EPhaseB,
    phase_c: S5EPhaseC,
    phase_d: S5EPhaseD,
) -> dict[str, object]:
    """Build the one-shot V1 envelope and validate it through the strict parser."""

    document: dict[str, object] = {
        "manifest_version": S5E_REBUILD_MANIFEST_VERSION,
        "mode": S5E_REBUILD_MODE,
        "source_commit": source_commit,
        "alembic_head": alembic_head,
        "source_lineage": asdict(source_lineage),
        "reviewed_artifacts": asdict(reviewed_artifacts),
        "phase_a": asdict(phase_a),
        "phase_b": asdict(phase_b),
        "phase_c": asdict(phase_c),
        "phase_d": asdict(phase_d),
    }
    document["manifest_checksum"] = canonical_checksum(document)
    parse_s5e_rebuild_manifest(render_s5e_rebuild_manifest(document))
    return document


def render_s5e_rebuild_manifest(document: dict[str, object]) -> bytes:
    """Render one deterministic protected-file representation."""

    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _parse_source_lineage(value: object) -> S5ESourceLineage:
    row = _mapping(value, field="source lineage")
    _exact_keys(
        row,
        {"source_identifier", "old_workbook_checksum", "latest_workbook_checksum"},
        field="source lineage",
    )
    return S5ESourceLineage(
        source_identifier=_text(row["source_identifier"], field="source identifier", maximum=200),
        old_workbook_checksum=_sha256(row["old_workbook_checksum"], field="old workbook checksum"),
        latest_workbook_checksum=_sha256(row["latest_workbook_checksum"], field="latest workbook checksum"),
    )


def _parse_reviewed_artifacts(value: object) -> S5EReviewedArtifacts:
    row = _mapping(value, field="reviewed artifacts")
    fields = {
        "source_seed_basis_checksum",
        "source_seed_decision_checksum",
        "mapping_checksum",
        "mapping_decision_checksum",
        "rating_disposition_checksum",
        "rating_decision_checksum",
        "rating_signature_checksum",
    }
    _exact_keys(row, fields, field="reviewed artifacts")
    return S5EReviewedArtifacts(
        source_seed_basis_checksum=_sha256(row["source_seed_basis_checksum"], field="source-seed basis checksum"),
        source_seed_decision_checksum=_sha256(
            row["source_seed_decision_checksum"], field="source-seed decision checksum"
        ),
        mapping_checksum=_sha256(row["mapping_checksum"], field="mapping checksum"),
        mapping_decision_checksum=_sha256(row["mapping_decision_checksum"], field="mapping decision checksum"),
        rating_disposition_checksum=_sha256(row["rating_disposition_checksum"], field="Rating disposition checksum"),
        rating_decision_checksum=_sha256(row["rating_decision_checksum"], field="Rating decision checksum"),
        rating_signature_checksum=_sha256(row["rating_signature_checksum"], field="Rating signature checksum"),
    )


def _parse_phase_a(value: object) -> S5EPhaseA:
    row = _mapping(value, field="phase A")
    fields = {
        "artifact_file_checksum",
        "source_semantic_checksum",
        "business_key_prefix_checksum",
        "transaction_count",
        "bet_count",
        "judgement_count",
        "expected_delta",
        "expected_cumulative_total",
        "wallet_deltas",
    }
    _exact_keys(row, fields, field="phase A")
    deltas = tuple(
        _parse_wallet_delta(item, index=index) for index, item in enumerate(_sequence(row["wallet_deltas"]), 1)
    )
    _unique((item.wallet_source_key for item in deltas), field="phase A wallet source key")
    return S5EPhaseA(
        artifact_file_checksum=_sha256(row["artifact_file_checksum"], field="E2 artifact file checksum"),
        source_semantic_checksum=_sha256(row["source_semantic_checksum"], field="E2 semantic checksum"),
        business_key_prefix_checksum=_sha256(
            row["business_key_prefix_checksum"], field="E2 business-key prefix checksum"
        ),
        transaction_count=_nonnegative_integer(row["transaction_count"], field="phase A transaction count"),
        bet_count=_nonnegative_integer(row["bet_count"], field="phase A Bet count"),
        judgement_count=_nonnegative_integer(row["judgement_count"], field="phase A judgement count"),
        expected_delta=_integer(row["expected_delta"], field="phase A expected delta"),
        expected_cumulative_total=_integer(row["expected_cumulative_total"], field="phase A expected cumulative total"),
        wallet_deltas=deltas,
    )


def _parse_phase_b(value: object) -> S5EPhaseB:
    row = _mapping(value, field="phase B")
    fields = {
        "manifest_checksum",
        "target_attribution_checksum",
        "reward_policy_checksum",
        "transaction_count",
        "expected_delta",
        "expected_cumulative_total",
        "owner_deltas",
    }
    _exact_keys(row, fields, field="phase B")
    deltas = tuple(
        _parse_owner_delta(item, index=index) for index, item in enumerate(_sequence(row["owner_deltas"]), 1)
    )
    _unique((item.owner_business_key for item in deltas), field="phase B owner business key")
    return S5EPhaseB(
        manifest_checksum=_sha256(row["manifest_checksum"], field="E3 manifest checksum"),
        target_attribution_checksum=_sha256(row["target_attribution_checksum"], field="E3 target attribution checksum"),
        reward_policy_checksum=_sha256(row["reward_policy_checksum"], field="E3 reward-policy checksum"),
        transaction_count=_nonnegative_integer(row["transaction_count"], field="phase B transaction count"),
        expected_delta=_integer(row["expected_delta"], field="phase B expected delta"),
        expected_cumulative_total=_integer(row["expected_cumulative_total"], field="phase B expected cumulative total"),
        owner_deltas=deltas,
    )


def _parse_phase_c(value: object) -> S5EPhaseC:
    row = _mapping(value, field="phase C")
    fields = {
        "provenance_checksum",
        "transaction_count",
        "expected_delta",
        "expected_cumulative_total",
        "participant_deltas",
    }
    _exact_keys(row, fields, field="phase C")
    deltas = _parse_participant_deltas(row["participant_deltas"], field="phase C")
    return S5EPhaseC(
        provenance_checksum=_sha256(row["provenance_checksum"], field="registration provenance checksum"),
        transaction_count=_nonnegative_integer(row["transaction_count"], field="phase C transaction count"),
        expected_delta=_integer(row["expected_delta"], field="phase C expected delta"),
        expected_cumulative_total=_integer(row["expected_cumulative_total"], field="phase C expected cumulative total"),
        participant_deltas=deltas,
    )


def _parse_phase_d(value: object) -> S5EPhaseD:
    row = _mapping(value, field="phase D")
    fields = {
        "win5_manifest_checksum",
        "win5_file_checksum",
        "participant_checksum",
        "score_reward_checksum",
        "transaction_count",
        "expected_delta",
        "expected_cumulative_total",
        "expected_season_score",
        "expected_top1_score",
        "participant_deltas",
    }
    _exact_keys(row, fields, field="phase D")
    deltas = _parse_participant_deltas(row["participant_deltas"], field="phase D")
    return S5EPhaseD(
        win5_manifest_checksum=_sha256(row["win5_manifest_checksum"], field="WIN5 manifest checksum"),
        win5_file_checksum=_sha256(row["win5_file_checksum"], field="WIN5 file checksum"),
        participant_checksum=_sha256(row["participant_checksum"], field="WIN5 participant checksum"),
        score_reward_checksum=_sha256(row["score_reward_checksum"], field="WIN5 score/reward checksum"),
        transaction_count=_nonnegative_integer(row["transaction_count"], field="phase D transaction count"),
        expected_delta=_integer(row["expected_delta"], field="phase D expected delta"),
        expected_cumulative_total=_integer(row["expected_cumulative_total"], field="phase D expected cumulative total"),
        expected_season_score=_nonnegative_integer(row["expected_season_score"], field="expected WIN5 Season score"),
        expected_top1_score=_nonnegative_integer(row["expected_top1_score"], field="expected WIN5 TOP1 score"),
        participant_deltas=deltas,
    )


def _parse_wallet_delta(value: object, *, index: int) -> S5EWalletDelta:
    row = _mapping(value, field=f"phase A wallet delta {index}")
    _exact_keys(row, {"wallet_source_key", "delta"}, field=f"phase A wallet delta {index}")
    return S5EWalletDelta(
        wallet_source_key=_sha256(row["wallet_source_key"], field=f"phase A wallet source key {index}"),
        delta=_integer(row["delta"], field=f"phase A wallet delta {index}"),
    )


def _parse_owner_delta(value: object, *, index: int) -> S5EOwnerDelta:
    row = _mapping(value, field=f"phase B owner delta {index}")
    _exact_keys(row, {"owner_business_key", "delta"}, field=f"phase B owner delta {index}")
    return S5EOwnerDelta(
        owner_business_key=_sha256(row["owner_business_key"], field=f"phase B owner business key {index}"),
        delta=_integer(row["delta"], field=f"phase B owner delta {index}"),
    )


def _parse_participant_deltas(value: object, *, field: str) -> tuple[S5EParticipantDelta, ...]:
    output: list[S5EParticipantDelta] = []
    for index, item in enumerate(_sequence(value), 1):
        row = _mapping(item, field=f"{field} participant delta {index}")
        _exact_keys(
            row,
            {"discord_user_id", "uma_pid", "delta"},
            field=f"{field} participant delta {index}",
        )
        output.append(
            S5EParticipantDelta(
                discord_user_id=_discord_user_id(row["discord_user_id"], field=f"{field} Discord user ID"),
                uma_pid=validate_registration_pid(_text(row["uma_pid"], field=f"{field} Uma PID", maximum=32)),
                delta=_integer(row["delta"], field=f"{field} participant delta {index}"),
            )
        )
    _unique((item.discord_user_id for item in output), field=f"{field} Discord user ID")
    _unique((item.uma_pid for item in output), field=f"{field} Uma PID")
    return tuple(output)


def _validate_phase_arithmetic(
    phase_a: S5EPhaseA,
    phase_b: S5EPhaseB,
    phase_c: S5EPhaseC,
    phase_d: S5EPhaseD,
) -> None:
    phases = (phase_a, phase_b, phase_c, phase_d)
    vectors = (
        phase_a.wallet_deltas,
        phase_b.owner_deltas,
        phase_c.participant_deltas,
        phase_d.participant_deltas,
    )
    running = 0
    for name, phase, vector in zip(("A", "B", "C", "D"), phases, vectors, strict=True):
        if sum(item.delta for item in vector) != phase.expected_delta:
            raise LegacyImportError(f"S5E phase {name} delta vector does not match its expected delta")
        running += phase.expected_delta
        if phase.expected_cumulative_total != running:
            raise LegacyImportError(f"S5E phase {name} cumulative total does not reconcile")
    if phase_a.expected_delta != 18530:
        raise LegacyImportError("S5E phase A must preserve the reviewed 18530 historical prefix")
    if phase_c.expected_delta != 500 or phase_c.transaction_count != 1:
        raise LegacyImportError("S5E phase C must contain the one reviewed 500 registration grant")
    if phase_d.expected_delta != 10 or phase_d.transaction_count != 1:
        raise LegacyImportError("S5E phase D must contain the one reviewed 10 WIN5 reward")
    if (phase_d.expected_season_score, phase_d.expected_top1_score) != (9, 0):
        raise LegacyImportError("S5E phase D must preserve the reviewed WIN5 9/0 score signature")


def _mapping(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LegacyImportError(f"{field} must be an object")
    return value


def _sequence(value: object) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise LegacyImportError("S5E rebuild manifest vector must be a list")
    return tuple(value)


def _exact_keys(value: dict[str, object], expected: set[str], *, field: str) -> None:
    if set(value) != expected:
        raise LegacyImportError(f"{field} fields do not match the S5E V1 contract")


def _unique(values, *, field: str) -> None:
    items = tuple(values)
    if len(items) != len(set(items)):
        raise LegacyImportError(f"{field} is duplicated")


def _text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise LegacyImportError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise LegacyImportError(f"{field} is invalid")
    return normalized


def _integer(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise LegacyImportError(f"{field} must be an integer")
    return value


def _nonnegative_integer(value: object, *, field: str) -> int:
    normalized = _integer(value, field=field)
    if normalized < 0:
        raise LegacyImportError(f"{field} must be nonnegative")
    return normalized


def _sha256(value: object, *, field: str) -> str:
    normalized = _text(value, field=field, maximum=64).lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise LegacyImportError(f"{field} must be a SHA-256 checksum")
    return normalized


def _commit(value: object) -> str:
    normalized = _text(value, field="source commit", maximum=40).lower()
    if _COMMIT_RE.fullmatch(normalized) is None:
        raise LegacyImportError("source commit must be a full 40-character revision")
    return normalized


def _discord_user_id(value: object, *, field: str) -> str:
    normalized = _text(value, field=field, maximum=32)
    if not normalized.isascii() or not normalized.isdigit():
        raise LegacyImportError(f"{field} must contain ASCII digits")
    return normalized
