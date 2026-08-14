"""Fail-closed validation for one private player-history source row.

The public API deliberately returns only a validity bit and a stable exclusion
code.  Parsed identity material is confined to the validator call stack; this
module performs no I/O and makes no authenticity claim about its input.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dataclass_field

from .player_history_security import NORMALIZER_ID, normalize_riot_id_v1


__all__ = (
    "EXCLUSION_CODES_V1",
    "PlayerHistoryRowValidationConfigurationError",
    "PlayerHistoryRowValidationV1",
    "validate_player_history_source_row_v1",
)


EXCLUSION_CODES_V1 = (
    "invalid_source_schema",
    "invalid_row_scalar",
    "out_of_scope",
    "invalid_participants_json",
    "invalid_private_json",
    "invalid_cardinality",
    "invalid_team",
    "invalid_champion",
    "invalid_identity",
    "invalid_riot_id",
    "invalid_participant_alignment",
)

_MAX_JSON_LENGTH = 262_144
_MAX_INT32 = (1 << 31) - 1
_MAX_INT63 = (1 << 63) - 1
_REQUIRED_FIELDS = (
    "game_id",
    "queue_id",
    "patch",
    "blue_wins",
    "duration_sec",
    "created_ms",
    "participants_json",
    "participants_private_json",
)
_TWO_COMPONENT_PATCH_RE = re.compile(r"([0-9]+)\.([0-9]+)\Z", re.ASCII)
_THREE_COMPONENT_PATCH_RE = re.compile(
    r"([0-9]+)\.([0-9]+)\.([0-9]+)\Z", re.ASCII
)
_GAME_ID_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z", re.ASCII)
_LOWERCASE_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.ASCII,
)


class PlayerHistoryRowValidationConfigurationError(ValueError):
    """Stable configuration failure with no caller-controlled diagnostics."""

    def __init__(self, _ignored: object = None) -> None:
        super().__init__("invalid_configuration")


@dataclass(frozen=True, slots=True)
class PlayerHistoryRowValidationV1:
    is_valid: bool
    exclusion_code: str | None


@dataclass(frozen=True, slots=True, repr=False)
class _ValidatedPrivateRowV1:
    participant_pairs: frozenset[tuple[int, int]]
    normalized_riot_ids: tuple[bytes, ...]
    participants: tuple[_ProjectedParticipantV1, ...]


@dataclass(frozen=True, slots=True)
class _ProjectedParticipantV1:
    normalized_riot_id: bytes = dataclass_field(repr=False)
    player_local_id: str = dataclass_field(repr=False)
    team_id: int
    champion_id: int


@dataclass(frozen=True, slots=True)
class _PlayerHistorySourceProjectionV1:
    game_id: int
    patch: str
    blue_wins: int
    duration_sec: int
    created_ms: int
    participants: tuple[_ProjectedParticipantV1, ...] = dataclass_field(repr=False)


def _invalid(code: str) -> PlayerHistoryRowValidationV1:
    return PlayerHistoryRowValidationV1(False, code)


def _is_canonical_decimal(component: str) -> bool:
    return bool(component) and (component == "0" or component[0] != "0")


def _validate_configuration(
    queue_id: int, patches: tuple[str, ...], expected_normalizer_id: str
) -> None:
    try:
        if type(queue_id) is not int or queue_id != 2400:
            raise ValueError
        if type(patches) is not tuple or not 1 <= len(patches) <= 3:
            raise ValueError

        parsed: list[tuple[int, int]] = []
        for patch in patches:
            if type(patch) is not str or len(patch) > 32:
                raise ValueError
            encoded = patch.encode("ascii", "strict")
            if len(encoded) > 32:
                raise ValueError
            match = _TWO_COMPONENT_PATCH_RE.fullmatch(patch)
            if match is None:
                raise ValueError
            components = match.groups()
            if any(not _is_canonical_decimal(component) for component in components):
                raise ValueError
            numeric = tuple(int(component) for component in components)
            if any(component > _MAX_INT32 for component in numeric):
                raise ValueError
            parsed.append((numeric[0], numeric[1]))

        if len(set(patches)) != len(patches):
            raise ValueError
        if any(current <= following for current, following in zip(parsed, parsed[1:])):
            raise ValueError
        if type(expected_normalizer_id) is not str or expected_normalizer_id != NORMALIZER_ID:
            raise ValueError
    except Exception:
        raise PlayerHistoryRowValidationConfigurationError() from None


def _validate_decimal_game_id(value: object) -> bool:
    if type(value) is not str or not 1 <= len(value) <= 20:
        return False
    try:
        encoded = value.encode("ascii", "strict")
    except UnicodeError:
        return False
    return (
        1 <= len(encoded) <= 20
        and _GAME_ID_RE.fullmatch(value) is not None
        and value != "0"
        and int(value) <= _MAX_INT63
    )


def _parse_source_patch(value: object) -> str | None:
    if type(value) is not str or not 1 <= len(value) <= 32:
        return None
    try:
        encoded = value.encode("ascii", "strict")
    except UnicodeError:
        return None
    if len(encoded) > 32:
        return None
    match = _THREE_COMPONENT_PATCH_RE.fullmatch(value)
    if match is None:
        return None
    components = match.groups()
    if any(not _is_canonical_decimal(component) for component in components):
        return None
    numeric = tuple(int(component) for component in components)
    if any(component > _MAX_INT32 for component in numeric):
        return None
    return f"{components[0]}.{components[1]}"


def _validate_json_scalar(value: object) -> str | None:
    if type(value) is not str or len(value) > _MAX_JSON_LENGTH:
        return None
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        return None
    if len(encoded) > _MAX_JSON_LENGTH:
        return None
    return value


def _reject_json_constant(_value: str) -> object:
    raise ValueError


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _parse_participant_array(value: str) -> list[dict[str, object]] | None:
    try:
        decoded = json.loads(
            value,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (
        json.JSONDecodeError,
        UnicodeError,
        ValueError,
        TypeError,
        RecursionError,
        MemoryError,
        OverflowError,
    ):
        return None
    if type(decoded) is not list or any(type(participant) is not dict for participant in decoded):
        return None
    return decoded


def _teams_are_valid(participants: list[dict[str, object]]) -> bool:
    teams: list[int] = []
    for participant in participants:
        team_id = participant.get("teamId")
        if type(team_id) is not int or team_id not in (100, 200):
            return False
        teams.append(team_id)
    return teams.count(100) == 5 and teams.count(200) == 5


def _champions_are_valid(participants: list[dict[str, object]]) -> bool:
    return all(
        type(participant.get("championId")) is int
        and 1 <= participant["championId"] <= _MAX_INT32
        for participant in participants
    )


def _private_identities_are_valid(
    participants: list[dict[str, object]],
) -> bool:
    participant_ids: set[int] = set()
    puuids: set[str] = set()
    for participant in participants:
        participant_id = participant.get("participantId")
        puuid = participant.get("puuid")
        if type(participant_id) is not int or not 1 <= participant_id <= 10:
            return False
        if participant_id in participant_ids:
            return False
        participant_ids.add(participant_id)
        if type(puuid) is not str:
            return False
        try:
            puuid.encode("ascii", "strict")
        except UnicodeError:
            return False
        if _LOWERCASE_UUID_RE.fullmatch(puuid) is None or puuid in puuids:
            return False
        puuids.add(puuid)
    return True


def _parse_private_riot_ids(
    participants: list[dict[str, object]],
) -> tuple[bytes, ...] | None:
    normalized: list[bytes] = []
    for participant in participants:
        game_name = participant.get("gameName")
        tag_line = participant.get("tagLine")
        if type(game_name) is not str or type(tag_line) is not str:
            return None
        if len(game_name) > 128 or len(tag_line) > 128:
            return None
        try:
            game_name_bytes = game_name.encode("utf-8", "strict")
            tag_line_bytes = tag_line.encode("utf-8", "strict")
        except UnicodeError:
            return None
        if len(game_name_bytes) > 128 or len(tag_line_bytes) > 128:
            return None
        joined = game_name + "#" + tag_line
        try:
            joined_bytes = joined.encode("utf-8", "strict")
        except UnicodeError:
            return None
        if len(joined_bytes) > 128:
            return None
        try:
            normalized.append(normalize_riot_id_v1(joined))
        except Exception:
            return None
    return tuple(normalized)


def _participant_pairs(
    participants: list[dict[str, object]],
) -> frozenset[tuple[int, int]]:
    return frozenset(
        (participant["teamId"], participant["championId"])
        for participant in participants
    )


def _parse_and_validate_source_row_v1(
    row: dict[str, object], *, queue_id: int, patches: tuple[str, ...]
) -> tuple[_ValidatedPrivateRowV1 | None, str | None]:
    if type(row) is not dict or any(field not in row for field in _REQUIRED_FIELDS):
        return None, "invalid_source_schema"

    game_id = row["game_id"]
    source_queue_id = row["queue_id"]
    source_patch = row["patch"]
    blue_wins = row["blue_wins"]
    duration_sec = row["duration_sec"]
    created_ms = row["created_ms"]
    participants_json = row["participants_json"]
    private_json = row["participants_private_json"]

    normalized_patch = _parse_source_patch(source_patch)
    public_json_text = _validate_json_scalar(participants_json)
    private_json_text = _validate_json_scalar(private_json)
    if (
        not _validate_decimal_game_id(game_id)
        or type(source_queue_id) is not int
        or not 0 <= source_queue_id <= _MAX_INT32
        or normalized_patch is None
        or type(blue_wins) is not int
        or blue_wins not in (0, 1)
        or type(duration_sec) is not int
        or not 1 <= duration_sec <= 86_400
        or type(created_ms) is not int
        or not 0 <= created_ms <= _MAX_INT63
        or public_json_text is None
        or private_json_text is None
    ):
        return None, "invalid_row_scalar"

    if source_queue_id != queue_id or normalized_patch not in patches:
        return None, "out_of_scope"

    public_participants = _parse_participant_array(public_json_text)
    if public_participants is None:
        return None, "invalid_participants_json"
    private_participants = _parse_participant_array(private_json_text)
    if private_participants is None:
        return None, "invalid_private_json"

    if len(public_participants) != 10 or len(private_participants) != 10:
        return None, "invalid_cardinality"
    if not _teams_are_valid(public_participants) or not _teams_are_valid(private_participants):
        return None, "invalid_team"
    if not _champions_are_valid(public_participants) or not _champions_are_valid(
        private_participants
    ):
        return None, "invalid_champion"
    if not _private_identities_are_valid(private_participants):
        return None, "invalid_identity"

    normalized_riot_ids = _parse_private_riot_ids(private_participants)
    if normalized_riot_ids is None:
        return None, "invalid_riot_id"
    if len(set(normalized_riot_ids)) != 10:
        return None, "invalid_identity"

    public_pairs = _participant_pairs(public_participants)
    private_pairs = _participant_pairs(private_participants)
    if len(public_pairs) != 10 or len(private_pairs) != 10 or public_pairs != private_pairs:
        return None, "invalid_participant_alignment"

    projected_participants = tuple(
        _ProjectedParticipantV1(
            normalized_riot_id=normalized_riot_ids[index],
            player_local_id=participant["puuid"],
            team_id=participant["teamId"],
            champion_id=participant["championId"],
        )
        for index, participant in sorted(
            enumerate(private_participants),
            key=lambda item: item[1]["participantId"],
        )
    )
    return _ValidatedPrivateRowV1(
        private_pairs, normalized_riot_ids, projected_participants
    ), None


def _project_player_history_source_row_v1(
    row: dict[str, object],
    *,
    queue_id: int,
    patches: tuple[str, ...],
    expected_normalizer_id: str,
) -> tuple[_PlayerHistorySourceProjectionV1 | None, str | None]:
    """Return a frozen internal projection or one stable exclusion code."""

    _validate_configuration(queue_id, patches, expected_normalizer_id)
    try:
        validated, exclusion_code = _parse_and_validate_source_row_v1(
            row, queue_id=queue_id, patches=patches
        )
        if validated is None:
            return None, exclusion_code or "invalid_source_schema"
        return (
            _PlayerHistorySourceProjectionV1(
                game_id=int(row["game_id"]),
                patch=_parse_source_patch(row["patch"]) or "",
                blue_wins=row["blue_wins"],
                duration_sec=row["duration_sec"],
                created_ms=row["created_ms"],
                participants=validated.participants,
            ),
            None,
        )
    except Exception:
        return None, "invalid_source_schema"


def validate_player_history_source_row_v1(
    row: dict[str, object],
    *,
    queue_id: int,
    patches: tuple[str, ...],
    expected_normalizer_id: str,
) -> PlayerHistoryRowValidationV1:
    """Validate one exact source projection without exposing private material."""

    _validate_configuration(queue_id, patches, expected_normalizer_id)
    validated, exclusion_code = _project_player_history_source_row_v1(
        row,
        queue_id=queue_id,
        patches=patches,
        expected_normalizer_id=expected_normalizer_id,
    )
    if validated is None:
        return _invalid(exclusion_code or "invalid_source_schema")
    return PlayerHistoryRowValidationV1(True, None)
