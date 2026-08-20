from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from umacircle_bot.domain.errors import MatchResultConflictError, MatchResultError
from umacircle_bot.domain.races import MatchRaceStatus, normalize_match_race_status


class MatchResultSubmissionStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    REVIEWED = "reviewed"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    CONFIRMED = "confirmed"


class MatchResultPublicationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    DELIVERY_UNKNOWN = "delivery_unknown"


ROOM_MATCH_TYPES = frozenset({"regular_room_match", "irregular_room_match"})


@dataclass(frozen=True, slots=True)
class MatchResultInput:
    entry_number: int
    rank: int
    character_evaluation_rank: str | None = None
    popularity_rank: int | None = None
    finish_time_ms: int | None = None
    finish_margin_text: str | None = None


def normalize_match_type(value: str) -> str:
    if not isinstance(value, str):
        raise MatchResultError("match type must be text")
    normalized = value.strip()
    if normalized not in ROOM_MATCH_TYPES:
        raise MatchResultError("unsupported room-match result type")
    return normalized


def normalize_result_input(values: Sequence[MatchResultInput]) -> tuple[MatchResultInput, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
        raise MatchResultError("result payload must contain at least one entry")
    normalized: list[MatchResultInput] = []
    for value in values:
        if not isinstance(value, MatchResultInput):
            raise MatchResultError("result payload entries are invalid")
        normalized.append(
            MatchResultInput(
                entry_number=_positive_int(value.entry_number, field="entry number"),
                rank=_positive_int(value.rank, field="rank"),
                character_evaluation_rank=_optional_text(
                    value.character_evaluation_rank,
                    field="character evaluation rank",
                    maximum=32,
                ),
                popularity_rank=_optional_positive_int(value.popularity_rank, field="popularity rank"),
                finish_time_ms=_optional_positive_int(
                    value.finish_time_ms,
                    field="finish time milliseconds",
                ),
                finish_margin_text=_optional_text(
                    value.finish_margin_text,
                    field="finish margin",
                    maximum=64,
                ),
            )
        )
    entry_numbers = [item.entry_number for item in normalized]
    ranks = [item.rank for item in normalized]
    if len(entry_numbers) != len(set(entry_numbers)):
        raise MatchResultError("result payload contains duplicate entry numbers")
    if len(ranks) != len(set(ranks)):
        raise MatchResultError("result payload contains duplicate ranks")
    return tuple(sorted(normalized, key=lambda item: item.entry_number))


def validate_complete_result(
    results: Sequence[MatchResultInput],
    *,
    final_entry_numbers: Sequence[int],
) -> None:
    expected = tuple(final_entry_numbers)
    if not expected:
        raise MatchResultConflictError("final room-match entry snapshot is empty")
    if len(expected) != len(set(expected)) or any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in expected
    ):
        raise MatchResultConflictError("final room-match entry snapshot is invalid")
    actual_numbers = tuple(item.entry_number for item in results)
    if set(actual_numbers) != set(expected) or len(actual_numbers) != len(expected):
        raise MatchResultError("result payload must contain every final entry exactly once")
    expected_ranks = set(range(1, len(expected) + 1))
    if {item.rank for item in results} != expected_ranks:
        raise MatchResultError("result ranks must be unique and contiguous from 1 through field size")
    if any(item.popularity_rank is not None and item.popularity_rank > len(expected) for item in results):
        raise MatchResultError("popularity rank must not exceed the final field size")
    popularity_ranks = [item.popularity_rank for item in results if item.popularity_rank is not None]
    if len(popularity_ranks) != len(set(popularity_ranks)):
        raise MatchResultError("result payload contains duplicate popularity ranks")


def validate_initial_submission(
    *,
    race_status: str | MatchRaceStatus,
    has_results: bool,
    has_submission_history: bool,
) -> None:
    status = normalize_match_race_status(race_status)
    if status is not MatchRaceStatus.BETTING_CLOSED:
        raise MatchResultConflictError("initial result submission requires closed betting")
    if has_results:
        raise MatchResultConflictError("race results already exist")
    if has_submission_history:
        raise MatchResultConflictError("result submission history already exists")


def validate_review(
    *,
    race_status: str | MatchRaceStatus,
    submission_status: str | MatchResultSubmissionStatus,
    is_current: bool,
) -> None:
    _require_result_review_race(race_status)
    if _submission_status(submission_status) is not MatchResultSubmissionStatus.PENDING_REVIEW or not is_current:
        raise MatchResultConflictError("only the current pending result revision can be reviewed")


def validate_correction(
    *,
    race_status: str | MatchRaceStatus,
    submission_status: str | MatchResultSubmissionStatus,
    is_latest_revision: bool,
    has_results: bool,
) -> None:
    _require_result_review_race(race_status)
    status = _submission_status(submission_status)
    if status not in {
        MatchResultSubmissionStatus.PENDING_REVIEW,
        MatchResultSubmissionStatus.REVIEWED,
        MatchResultSubmissionStatus.REJECTED,
    }:
        raise MatchResultConflictError("result revision cannot be corrected from its current status")
    if not is_latest_revision:
        raise MatchResultConflictError("only the latest result revision can be corrected")
    if has_results:
        raise MatchResultConflictError("confirmed race results are immutable")


def validate_rejection(
    *,
    race_status: str | MatchRaceStatus,
    submission_status: str | MatchResultSubmissionStatus,
    is_current: bool,
) -> None:
    _require_result_review_race(race_status)
    if (
        _submission_status(submission_status)
        not in {
            MatchResultSubmissionStatus.PENDING_REVIEW,
            MatchResultSubmissionStatus.REVIEWED,
        }
        or not is_current
    ):
        raise MatchResultConflictError("only the current pending or reviewed result revision can be rejected")


def validate_confirmation(
    *,
    race_status: str | MatchRaceStatus,
    submission_status: str | MatchResultSubmissionStatus,
    is_current: bool,
    has_results: bool,
) -> None:
    _require_result_review_race(race_status)
    if _submission_status(submission_status) is not MatchResultSubmissionStatus.REVIEWED or not is_current:
        raise MatchResultConflictError("only the current reviewed result revision can be confirmed")
    if has_results:
        raise MatchResultConflictError("race results already exist")


def validate_submission_state(
    *,
    submission_status: str | MatchResultSubmissionStatus,
    is_current: bool,
    has_review_metadata: bool,
    has_rejection_metadata: bool,
    has_confirmation_metadata: bool,
) -> None:
    status = _submission_status(submission_status)
    valid = {
        MatchResultSubmissionStatus.PENDING_REVIEW: (
            is_current and not has_review_metadata and not has_rejection_metadata and not has_confirmation_metadata
        ),
        MatchResultSubmissionStatus.REVIEWED: (
            is_current and has_review_metadata and not has_rejection_metadata and not has_confirmation_metadata
        ),
        MatchResultSubmissionStatus.SUPERSEDED: (
            not is_current and not has_rejection_metadata and not has_confirmation_metadata
        ),
        MatchResultSubmissionStatus.REJECTED: (
            not is_current and has_rejection_metadata and not has_confirmation_metadata
        ),
        MatchResultSubmissionStatus.CONFIRMED: (
            is_current and has_review_metadata and not has_rejection_metadata and has_confirmation_metadata
        ),
    }[status]
    if not valid:
        raise MatchResultConflictError("persisted result revision state is inconsistent")


def validate_publication_intent(
    *,
    race_status: str | MatchRaceStatus,
    submission_status: str | MatchResultSubmissionStatus,
    publication_status: str | MatchResultPublicationStatus | None,
    authorize_delivery_unknown_retry: bool,
) -> None:
    if normalize_match_race_status(race_status) is not MatchRaceStatus.SETTLED:
        raise MatchResultConflictError("only a settled room-match result can be published")
    if _submission_status(submission_status) is not MatchResultSubmissionStatus.CONFIRMED:
        raise MatchResultConflictError("publication requires the confirmed result revision")
    if publication_status is None:
        return
    status = _publication_status(publication_status)
    if status is MatchResultPublicationStatus.FAILED:
        return
    if status is MatchResultPublicationStatus.DELIVERY_UNKNOWN and authorize_delivery_unknown_retry:
        return
    if status is MatchResultPublicationStatus.DELIVERY_UNKNOWN:
        raise MatchResultConflictError("delivery-unknown publication requires explicit retry authorization")
    if status is MatchResultPublicationStatus.PENDING:
        raise MatchResultConflictError("publication attempt is already pending")
    raise MatchResultConflictError("confirmed result publication was already sent")


def validate_publication_outcome(
    *,
    current_status: str | MatchResultPublicationStatus,
    outcome: str | MatchResultPublicationStatus,
) -> None:
    current = _publication_status(current_status)
    target = _publication_status(outcome)
    if target is MatchResultPublicationStatus.SENT:
        if current not in {
            MatchResultPublicationStatus.PENDING,
            MatchResultPublicationStatus.DELIVERY_UNKNOWN,
        }:
            raise MatchResultConflictError("publication cannot be marked sent from its current state")
        return
    if target in {
        MatchResultPublicationStatus.FAILED,
        MatchResultPublicationStatus.DELIVERY_UNKNOWN,
    }:
        if current is not MatchResultPublicationStatus.PENDING:
            raise MatchResultConflictError("only a pending publication attempt can record this outcome")
        return
    raise MatchResultError("unsupported publication outcome")


def _require_result_review_race(value: str | MatchRaceStatus) -> None:
    if normalize_match_race_status(value) is not MatchRaceStatus.RESULT_REVIEW:
        raise MatchResultConflictError("result revision mutation requires result-review race state")


def _submission_status(value: str | MatchResultSubmissionStatus) -> MatchResultSubmissionStatus:
    if isinstance(value, MatchResultSubmissionStatus):
        return value
    if not isinstance(value, str):
        raise MatchResultConflictError("persisted result revision status is invalid")
    try:
        return MatchResultSubmissionStatus(value)
    except ValueError as exc:
        raise MatchResultConflictError("persisted result revision status is invalid") from exc


def _publication_status(value: str | MatchResultPublicationStatus) -> MatchResultPublicationStatus:
    if isinstance(value, MatchResultPublicationStatus):
        return value
    if not isinstance(value, str):
        raise MatchResultConflictError("persisted publication status is invalid")
    try:
        return MatchResultPublicationStatus(value)
    except ValueError as exc:
        raise MatchResultConflictError("persisted publication status is invalid") from exc


def _positive_int(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MatchResultError(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: int | None, *, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field=field)


def _optional_text(value: str | None, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MatchResultError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or not normalized.isprintable():
        raise MatchResultError(f"{field} must contain 1 to {maximum} printable characters")
    return normalized
