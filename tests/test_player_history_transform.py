from __future__ import annotations

import dataclasses
import inspect
import json
import sqlite3
import unittest
from copy import deepcopy
from pathlib import Path

from aram_nn.site.player_history_security import NORMALIZER_ID
from aram_nn.site.player_history_transform import (
    EXCLUSION_CODES_V1,
    PlayerHistoryRowValidationConfigurationError,
    PlayerHistoryRowValidationV1,
    validate_player_history_source_row_v1,
)
from aram_nn.site.player_history_transform import _project_player_history_source_row_v1


EXPECTED_CODES = (
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


def _participants() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    public: list[dict[str, object]] = []
    private: list[dict[str, object]] = []
    for index in range(10):
        team_id = 100 if index < 5 else 200
        champion_id = index + 1
        public.append(
            {"teamId": team_id, "championId": champion_id, "stats": {"kills": index}}
        )
        private.append(
            {
                "teamId": team_id,
                "championId": champion_id,
                "participantId": index + 1,
                "puuid": f"00000000-0000-0000-0000-{index + 1:012x}",
                "gameName": f"Player{index + 1}",
                "tagLine": f"TW{index + 1:02d}",
                "riotId": "ignored-carrier-canary",
            }
        )
    return public, private


def _row() -> dict[str, object]:
    public, private = _participants()
    return {
        "game_id": "9007199254740991",
        "queue_id": 2400,
        "patch": "16.10.1",
        "blue_wins": 1,
        "duration_sec": 1234,
        "created_ms": 1_797_000_000_000,
        "participants_json": json.dumps(public, separators=(",", ":")),
        "participants_private_json": json.dumps(
            private, ensure_ascii=False, separators=(",", ":")
        ),
    }


def _validate(row: dict[str, object]) -> PlayerHistoryRowValidationV1:
    return validate_player_history_source_row_v1(
        row,
        queue_id=2400,
        patches=("16.10", "16.9"),
        expected_normalizer_id=NORMALIZER_ID,
    )


def _with_json_mutation(
    row: dict[str, object], field: str, mutation
) -> dict[str, object]:
    changed = deepcopy(row)
    decoded = json.loads(changed[field])
    mutation(decoded)
    changed[field] = json.dumps(decoded, ensure_ascii=False, separators=(",", ":"))
    return changed


class IntSubclass(int):
    pass


class StrSubclass(str):
    pass


class DictSubclass(dict):
    pass


class PlayerHistoryTransformContractTests(unittest.TestCase):
    def assert_code(self, row: object, code: str) -> None:
        result = validate_player_history_source_row_v1(
            row,  # type: ignore[arg-type]
            queue_id=2400,
            patches=("16.10", "16.9"),
            expected_normalizer_id=NORMALIZER_ID,
        )
        self.assertEqual(result, PlayerHistoryRowValidationV1(False, code))

    def test_exact_public_api_signature_and_frozen_slots_result(self) -> None:
        import aram_nn.site.player_history_transform as transform

        self.assertEqual(transform.__all__, (
            "EXCLUSION_CODES_V1",
            "PlayerHistoryRowValidationConfigurationError",
            "PlayerHistoryRowValidationV1",
            "validate_player_history_source_row_v1",
        ))
        self.assertEqual(EXCLUSION_CODES_V1, EXPECTED_CODES)
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(PlayerHistoryRowValidationV1)),
            ("is_valid", "exclusion_code"),
        )
        result = _validate(_row())
        self.assertTrue(result.is_valid)
        self.assertIsNone(result.exclusion_code)
        self.assertFalse(hasattr(result, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.is_valid = False  # type: ignore[misc]
        signature = inspect.signature(validate_player_history_source_row_v1)
        self.assertEqual(tuple(signature.parameters), (
            "row", "queue_id", "patches", "expected_normalizer_id"
        ))
        self.assertEqual(signature.parameters["queue_id"].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_configuration_is_validated_first_and_always_has_fixed_error(self) -> None:
        invalid_configs = (
            {"queue_id": True, "patches": ("16.10",), "expected_normalizer_id": NORMALIZER_ID},
            {"queue_id": 2399, "patches": ("16.10",), "expected_normalizer_id": NORMALIZER_ID},
            {"queue_id": 2400, "patches": ["16.10"], "expected_normalizer_id": NORMALIZER_ID},
            {"queue_id": 2400, "patches": (), "expected_normalizer_id": NORMALIZER_ID},
            {"queue_id": 2400, "patches": ("16.10",) * 4, "expected_normalizer_id": NORMALIZER_ID},
            {"queue_id": 2400, "patches": (StrSubclass("16.10"),), "expected_normalizer_id": NORMALIZER_ID},
            {"queue_id": 2400, "patches": ("016.10",), "expected_normalizer_id": NORMALIZER_ID},
            {"queue_id": 2400, "patches": ("16.010",), "expected_normalizer_id": NORMALIZER_ID},
            {"queue_id": 2400, "patches": ("16.10.1",), "expected_normalizer_id": NORMALIZER_ID},
            {"queue_id": 2400, "patches": ("16.9", "16.10"), "expected_normalizer_id": NORMALIZER_ID},
            {"queue_id": 2400, "patches": ("16.10", "16.10"), "expected_normalizer_id": NORMALIZER_ID},
            {"queue_id": 2400, "patches": ("2147483648.1",), "expected_normalizer_id": NORMALIZER_ID},
            {"queue_id": 2400, "patches": ("1." + "1" * 31,), "expected_normalizer_id": NORMALIZER_ID},
            {"queue_id": 2400, "patches": ("１６.１０",), "expected_normalizer_id": NORMALIZER_ID},
            {"queue_id": 2400, "patches": ("16.10",), "expected_normalizer_id": StrSubclass(NORMALIZER_ID)},
            {"queue_id": 2400, "patches": ("16.10",), "expected_normalizer_id": "wrong"},
        )
        for config in invalid_configs:
            with self.subTest(config=config):
                with self.assertRaises(PlayerHistoryRowValidationConfigurationError) as caught:
                    validate_player_history_source_row_v1([], **config)  # type: ignore[arg-type]
                self.assertEqual(str(caught.exception), "invalid_configuration")
                self.assertEqual(caught.exception.args, ("invalid_configuration",))

    def test_sqlite_projection_shape_is_valid_without_using_committing_poller_save(self) -> None:
        # Deliberate acceptance deviation: reproduce the exact SELECT projection
        # in memory instead of invoking poller._save, which commits by design.
        source = _row()
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                "CREATE TABLE games (game_id TEXT, queue_id INTEGER, patch TEXT, "
                "blue_wins INTEGER, duration_sec INTEGER, created_ms INTEGER, "
                "participants_json TEXT, participants_private_json TEXT) STRICT"
            )
            fields = tuple(source)
            connection.execute(
                f"INSERT INTO games ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                tuple(source[field] for field in fields),
            )
            cursor = connection.execute(
                "SELECT game_id,queue_id,patch,blue_wins,duration_sec,created_ms,"
                "participants_json,participants_private_json FROM games"
            )
            projected = dict(zip((description[0] for description in cursor.description), cursor.fetchone()))
        finally:
            connection.close()
        self.assertEqual(_validate(projected), PlayerHistoryRowValidationV1(True, None))

    def test_source_schema_is_exact_dict_with_required_fields_but_ignores_extras(self) -> None:
        self.assert_code([], "invalid_source_schema")
        self.assert_code(DictSubclass(_row()), "invalid_source_schema")
        for field in tuple(_row()):
            changed = _row()
            del changed[field]
            self.assert_code(changed, "invalid_source_schema")
        pre_save = _row()
        del pre_save["participants_json"]
        del pre_save["participants_private_json"]
        pre_save["participants"], pre_save["participants_private"] = _participants()
        self.assert_code(pre_save, "invalid_source_schema")
        extra_canary = object()
        valid = _row()
        valid["unaccessed_extra"] = extra_canary
        self.assertTrue(_validate(valid).is_valid)

    def test_all_scalar_boundaries_types_and_subclasses(self) -> None:
        invalid_values = {
            "game_id": (0, "", "0", "01", "+1", "9223372036854775808", "1" * 21, "１２", StrSubclass("1")),
            "queue_id": (-1, _MAX_INT32_PLUS_ONE := 1 << 31, True, 2400.0, IntSubclass(2400)),
            "patch": (None, "16.10", "16.10.01", "016.10.1", "16.010.1", "16.10.2147483648", "１６.１０.１", StrSubclass("16.10.1")),
            "blue_wins": (-1, 2, False, 1.0, IntSubclass(1)),
            "duration_sec": (0, 86401, True, 1.0, IntSubclass(1)),
            "created_ms": (-1, 1 << 63, True, 0.0, IntSubclass(0)),
            "participants_json": (None, b"[]", StrSubclass("[]"), "\ud800", "x" * 262_145),
            "participants_private_json": (None, b"[]", StrSubclass("[]"), "\ud800", "x" * 262_145),
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=repr(value)[:80]):
                    changed = _row()
                    changed[field] = value
                    self.assert_code(changed, "invalid_row_scalar")

        valid_boundaries = {
            "game_id": ("1", str((1 << 63) - 1)),
            "queue_id": (0, (1 << 31) - 1),
            "patch": ("0.0.0", "2147483647.2147483647.2147483647"),
            "blue_wins": (0, 1),
            "duration_sec": (1, 86400),
            "created_ms": (0, (1 << 63) - 1),
        }
        for field, values in valid_boundaries.items():
            for value in values:
                changed = _row()
                changed[field] = value
                result = _validate(changed)
                if field in ("queue_id", "patch") and value not in (2400, "16.10.1"):
                    self.assertEqual(result.exclusion_code, "out_of_scope")
                else:
                    self.assertTrue(result.is_valid)

    def test_json_utf8_byte_limit_is_enforced_after_character_limit(self) -> None:
        for field, expected in (
            ("participants_json", "invalid_row_scalar"),
            ("participants_private_json", "invalid_row_scalar"),
        ):
            changed = _row()
            changed[field] = "界" * 87_382
            self.assertLessEqual(len(changed[field]), 262_144)
            self.assertGreater(len(changed[field].encode("utf-8")), 262_144)
            self.assert_code(changed, expected)

    def test_scope_drops_only_build_component(self) -> None:
        self.assertTrue(_validate(_row()).is_valid)
        changed = _row()
        changed["patch"] = "16.10.2147483647"
        self.assertTrue(_validate(changed).is_valid)
        changed["patch"] = "16.11.1"
        self.assert_code(changed, "out_of_scope")
        changed = _row()
        changed["queue_id"] = 2399
        self.assert_code(changed, "out_of_scope")
        changed = _row()
        changed["patch"] = "16.10"
        self.assert_code(changed, "invalid_row_scalar")

    def test_json_rejects_nonstandard_constants_duplicates_top_shapes_and_deep_input(self) -> None:
        for field, expected in (
            ("participants_json", "invalid_participants_json"),
            ("participants_private_json", "invalid_private_json"),
        ):
            for payload in ("NaN", "Infinity", "-Infinity", "{}", '[[[[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]]]]'):
                changed = _row()
                changed[field] = payload
                self.assert_code(changed, expected)
            changed = _row()
            changed[field] = '[{"teamId":100,"teamId":200}]'
            self.assert_code(changed, expected)
            changed = _row()
            changed[field] = '[{"teamId":100,"extra":{"x":1,"x":2}}]'
            self.assert_code(changed, expected)
            changed = _row()
            changed[field] = "[" * 1500 + "0" + "]" * 1500
            self.assert_code(changed, expected)
            changed = _row()
            changed[field] = json.dumps([1] * 10)
            self.assert_code(changed, expected)

    def test_json_and_cardinality_precedence(self) -> None:
        changed = _row()
        changed["participants_json"] = "["
        changed["participants_private_json"] = "["
        self.assert_code(changed, "invalid_participants_json")
        changed["participants_json"] = "[]"
        self.assert_code(changed, "invalid_private_json")
        changed["participants_private_json"] = "[]"
        self.assert_code(changed, "invalid_cardinality")

    def test_team_then_champion_precedence_across_both_arrays(self) -> None:
        changed = _with_json_mutation(_row(), "participants_json", lambda rows: rows[0].update(teamId=300))
        changed = _with_json_mutation(changed, "participants_private_json", lambda rows: rows[0].update(championId=0))
        self.assert_code(changed, "invalid_team")
        for field in ("participants_json", "participants_private_json"):
            for value in (True, 99, 201):
                changed = _with_json_mutation(_row(), field, lambda rows, value=value: rows[0].update(teamId=value))
                self.assert_code(changed, "invalid_team")
            for value in (True, 0, 1 << 31):
                changed = _with_json_mutation(_row(), field, lambda rows, value=value: rows[0].update(championId=value))
                self.assert_code(changed, "invalid_champion")

    def test_private_identity_rules(self) -> None:
        mutations = (
            lambda rows: rows[0].update(participantId=True),
            lambda rows: rows[0].update(participantId=0),
            lambda rows: rows[0].update(participantId=11),
            lambda rows: rows[0].update(participantId=rows[1]["participantId"]),
            lambda rows: rows[0].update(puuid=None),
            lambda rows: rows[0].update(puuid="00000000-0000-0000-0000-00000000000A"),
            lambda rows: rows[0].update(puuid="not-a-uuid"),
            lambda rows: rows[0].update(puuid=rows[1]["puuid"]),
        )
        for mutation in mutations:
            self.assert_code(
                _with_json_mutation(_row(), "participants_private_json", mutation),
                "invalid_identity",
            )

    def test_riot_id_normalization_aliases_rejections_bounds_and_uniqueness(self) -> None:
        alias = _with_json_mutation(
            _row(),
            "participants_private_json",
            lambda rows: rows[0].update(
                gameName="\uff30\uff4c\uff41\uff59\uff45\uff52\uff11"
            ),
        )
        self.assertTrue(_validate(alias).is_valid)
        changed = _with_json_mutation(
            _row(),
            "participants_private_json",
            lambda rows: rows[0].update(gameName="Player2", tagLine="TW02"),
        )
        self.assert_code(changed, "invalid_identity")

        for mutation in (
            lambda rows: rows[0].update(gameName=None),
            lambda rows: rows[0].update(tagLine=None),
            lambda rows: rows[0].update(gameName="ab"),
            lambda rows: rows[0].update(tagLine="ab"),
            lambda rows: rows[0].update(gameName="bad#name"),
            lambda rows: rows[0].update(gameName="a" * 129),
            lambda rows: rows[0].update(tagLine="a" * 129),
            lambda rows: rows[0].update(gameName="界" * 43),
        ):
            self.assert_code(
                _with_json_mutation(_row(), "participants_private_json", mutation),
                "invalid_riot_id",
            )
        self.assert_code(
            _with_json_mutation(
                _row(),
                "participants_private_json",
                lambda rows: rows[0].update(gameName="\ud800"),
            ),
            "invalid_row_scalar",
        )

    def test_alignment_allows_shuffle_and_opposing_duplicate_champion_only(self) -> None:
        changed = _with_json_mutation(
            _row(), "participants_private_json", lambda rows: rows.reverse()
        )
        self.assertTrue(_validate(changed).is_valid)

        public, private = _participants()
        public[5]["championId"] = public[0]["championId"]
        private[5]["championId"] = private[0]["championId"]
        changed = _row()
        changed["participants_json"] = json.dumps(public)
        changed["participants_private_json"] = json.dumps(private)
        self.assertTrue(_validate(changed).is_valid)

        changed = _with_json_mutation(
            _row(), "participants_json", lambda rows: rows[1].update(championId=rows[0]["championId"])
        )
        self.assert_code(changed, "invalid_participant_alignment")
        changed = _with_json_mutation(
            _row(), "participants_private_json", lambda rows: rows[0].update(championId=999)
        )
        self.assert_code(changed, "invalid_participant_alignment")

    def test_nonmutation_determinism_and_no_private_material_in_public_objects_or_errors(self) -> None:
        row = _row()
        original = deepcopy(row)
        first = _validate(row)
        second = _validate(row)
        self.assertEqual(first, second)
        self.assertEqual(row, original)
        result_text = repr(first) + str(first)
        for canary in ("Player1", "TW01", "00000000-0000", row["game_id"]):
            self.assertNotIn(canary, result_text)

        invalid = _with_json_mutation(
            row,
            "participants_private_json",
            lambda rows: rows[0].update(gameName="SECRET#CANARY"),
        )
        result = _validate(invalid)
        self.assertEqual(result.exclusion_code, "invalid_riot_id")
        self.assertNotIn("SECRET-CANARY", repr(result) + str(result))

    def test_internal_projection_is_frozen_aligned_and_repr_safe(self) -> None:
        row = _row()
        projection, exclusion = _project_player_history_source_row_v1(
            row,
            queue_id=2400,
            patches=("16.10", "16.9"),
            expected_normalizer_id=NORMALIZER_ID,
        )
        self.assertIsNone(exclusion)
        self.assertIsNotNone(projection)
        assert projection is not None
        self.assertEqual(
            (projection.game_id, projection.patch, projection.blue_wins),
            (9007199254740991, "16.10", 1),
        )
        self.assertEqual(len(projection.participants), 10)
        self.assertEqual(
            (projection.participants[0].team_id, projection.participants[0].champion_id),
            (100, 1),
        )
        rendered = repr(projection) + repr(projection.participants[0])
        for canary in ("player1#tw01", "00000000-0000", "Player1", "TW01"):
            self.assertNotIn(canary, rendered)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            projection.game_id = 1  # type: ignore[misc]

    def test_module_has_no_io_logging_or_forbidden_private_exports(self) -> None:
        source_path = Path(inspect.getsourcefile(validate_player_history_source_row_v1) or "")
        source = source_path.read_text(encoding="utf-8")
        for forbidden in (
            "sqlite3", "open(", "Path(", "requests", "urllib", "socket", "logging", "print(",
            "participants_private_json =",
        ):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("_ValidatedPrivateRowV1", __import__(
            "aram_nn.site.player_history_transform", fromlist=["__all__"]
        ).__all__)


if __name__ == "__main__":
    unittest.main()
