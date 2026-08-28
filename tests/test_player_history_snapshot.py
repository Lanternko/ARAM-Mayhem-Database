from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from aram_nn.site import player_history_snapshot as snapshot_module
from aram_nn.site.player_history_readmodel import (
    MAX_STORED_HISTORY_V1,
    audit_player_history_snapshot_streaming_v1,
    audit_player_history_snapshot_v1,
)
from aram_nn.site.player_history_security import NORMALIZER_ID, derive_lookup_key, normalize_riot_id_v1
from aram_nn.site.player_history_snapshot import (
    PlayerHistorySnapshotConfigV1,
    PlayerHistorySnapshotError,
    build_and_publish_player_history_snapshot_from_live_v1,
    build_and_publish_player_history_snapshot_from_sqlite_v1,
    build_player_history_graph_v1,
    main,
    publish_player_history_snapshot_v1,
    read_player_history_source_sqlite_v1,
)


LOOKUP_SECRET = bytes(range(32))
EVENT_SECRET = bytes(range(32, 64))


def source_row(
    game_id: int = 101,
    *,
    created_ms: int = 1000,
    duration_sec: int = 899,
    blue_wins: int = 1,
    queue_id: int = 2400,
    patch: str = "16.10.1",
    first_name: str = "Player1",
    first_puuid: str = "00000000-0000-0000-0000-000000000001",
) -> dict[str, object]:
    public = []
    private = []
    for index in range(10):
        team = 100 if index < 5 else 200
        public.append({"teamId": team, "championId": index + 1})
        private.append(
            {
                "teamId": team,
                "championId": index + 1,
                "participantId": index + 1,
                "puuid": first_puuid if index == 0 else f"00000000-0000-0000-0000-{index + 1:012x}",
                "gameName": first_name if index == 0 else f"Player{index + 1}",
                "tagLine": f"TW{index + 1:02d}",
                "privateCanary": "NEVER-PUBLISH-THIS",
            }
        )
    return {
        "game_id": str(game_id),
        "queue_id": queue_id,
        "patch": patch,
        "blue_wins": blue_wins,
        "duration_sec": duration_sec,
        "created_ms": created_ms,
        "participants_json": json.dumps(public, separators=(",", ":")),
        "participants_private_json": json.dumps(private, separators=(",", ":")),
    }


def config() -> PlayerHistorySnapshotConfigV1:
    return PlayerHistorySnapshotConfigV1(
        "history-test", ("16.10", "16.9"), "2026-08-13", LOOKUP_SECRET, EVENT_SECRET
    )


LIVE_SCHEMA = """
CREATE TABLE games (
    game_id TEXT PRIMARY KEY,
    queue_id INTEGER NOT NULL,
    patch TEXT NOT NULL,
    blue_champs TEXT NOT NULL,
    red_champs TEXT NOT NULL,
    blue_wins INTEGER NOT NULL,
    duration_sec INTEGER NOT NULL,
    created_ms INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    participants_json TEXT,
    seed_family TEXT NOT NULL DEFAULT '',
    participants_private_json TEXT
)
"""


def _insert_live_row(connection: sqlite3.Connection, row: dict[str, object]) -> None:
    connection.execute(
        "INSERT INTO games (game_id,queue_id,patch,blue_champs,red_champs,blue_wins,"
        "duration_sec,created_ms,captured_at,participants_json,seed_family,"
        "participants_private_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["game_id"],
            row["queue_id"],
            row["patch"],
            "[1,2,3,4,5]",
            "[6,7,8,9,10]",
            row["blue_wins"],
            row["duration_sec"],
            row["created_ms"],
            "2026-08-13T00:00:00+00:00",
            row["participants_json"],
            "private-seed-must-not-be-selected",
            row["participants_private_json"],
        ),
    )


def _open_live_database(
    path: Path,
    rows: list[dict[str, object]],
    *,
    schema: str = LIVE_SCHEMA,
) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    self_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
    if self_mode != ("wal",):
        connection.close()
        raise AssertionError("test database did not enter WAL mode")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute(schema)
    for row in rows:
        _insert_live_row(connection, row)
    connection.commit()
    return connection


def _observed_matches(graph, alias: str = "Player1#TW01") -> int:
    lookup_key = derive_lookup_key(
        LOOKUP_SECRET,
        expected_normalizer_id=NORMALIZER_ID,
        normalized_riot_id=normalize_riot_id_v1(alias),
    )
    return next(row.observed_matches for row in graph.lookups if row.lookup_key == lookup_key)


def _snapshot_player(path: Path, alias: str = "Player1#TW01") -> tuple[int, list[tuple]]:
    lookup_key = derive_lookup_key(
        LOOKUP_SECRET,
        expected_normalizer_id=NORMALIZER_ID,
        normalized_riot_id=normalize_riot_id_v1(alias),
    )
    connection = sqlite3.connect(path)
    try:
        observed = connection.execute(
            "SELECT observed_matches FROM player_lookup WHERE lookup_key=?",
            (lookup_key,),
        ).fetchone()[0]
        histories = connection.execute(
            "SELECT ordinal,patch FROM player_history WHERE lookup_key=? ORDER BY ordinal",
            (lookup_key,),
        ).fetchall()
        return observed, histories
    finally:
        connection.close()


class PlayerHistorySnapshotTests(unittest.TestCase):
    def test_streaming_build_caps_history_but_preserves_full_observed_count(self) -> None:
        rows = [
            source_row(game_id, created_ms=game_id)
            for game_id in range(1, MAX_STORED_HISTORY_V1 + 6)
        ]
        rows.extend(
            (
                source_row(1001, first_name="SharedName"),
                source_row(
                    1002,
                    first_name="SharedName",
                    first_puuid="10000000-0000-0000-0000-000000000001",
                ),
            )
        )
        malformed = source_row(1003)
        malformed["participants_private_json"] = "not-json"
        rows.append(malformed)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "games.db"
            destination = root / "snapshot.sqlite"
            writer = _open_live_database(source, rows)
            try:
                with (
                    mock.patch.object(
                        snapshot_module,
                        "_trusted_live_database_path_v1",
                        return_value=source,
                    ),
                    mock.patch.object(
                        snapshot_module,
                        "build_player_history_graph_v1",
                        side_effect=AssertionError("full graph path forbidden"),
                    ),
                ):
                    result = build_and_publish_player_history_snapshot_from_live_v1(
                        destination=destination,
                        manifest=root / "snapshot.manifest.json",
                        config=config(),
                    )
                observed, histories = _snapshot_player(destination)
                self.assertEqual(observed, MAX_STORED_HISTORY_V1 + 5)
                self.assertEqual(len(histories), MAX_STORED_HISTORY_V1)
                self.assertEqual(
                    [row[0] for row in histories],
                    list(range(1, MAX_STORED_HISTORY_V1 + 1)),
                )
                self.assertEqual(
                    [row[1] for row in histories[:3]],
                    ["16.10", "16.10", "16.10"],
                )
                shared_key = derive_lookup_key(
                    LOOKUP_SECRET,
                    expected_normalizer_id=NORMALIZER_ID,
                    normalized_riot_id=normalize_riot_id_v1("SharedName#TW01"),
                )
                connection = sqlite3.connect(destination)
                try:
                    connection.execute("PRAGMA foreign_keys=ON")
                    self.assertEqual(
                        connection.execute(
                            "SELECT status,observed_matches,low_sample FROM player_lookup "
                            "WHERE lookup_key=?",
                            (shared_key,),
                        ).fetchone(),
                        ("ambiguous", None, None),
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT count(*) FROM player_history WHERE lookup_key=?",
                            (shared_key,),
                        ).fetchone(),
                        (0,),
                    )
                    summary = audit_player_history_snapshot_streaming_v1(connection)
                finally:
                    connection.close()
                exclusions = json.loads(summary.meta.exclusions_json)
                self.assertEqual(exclusions["invalid_private_json"], 1)
                self.assertEqual(result.row_count, summary.row_count)
                raw = destination.read_bytes()
                for canary in (
                    b"Player1",
                    b"SharedName",
                    b"TW01",
                    b"00000000-0000",
                    b"NEVER-PUBLISH-THIS",
                    LOOKUP_SECRET,
                    EVENT_SECRET,
                ):
                    self.assertNotIn(canary, raw)
                self.assertEqual(list(root.glob("*.stage.*.tmp")), [])
            finally:
                writer.close()

    def test_offline_streaming_sql_filters_before_projection_and_no_clobber(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "copy.sqlite"
            connection = sqlite3.connect(source)
            connection.execute(
                "CREATE TABLE games (game_id TEXT PRIMARY KEY,queue_id INTEGER NOT NULL,"
                "patch TEXT NOT NULL,blue_wins INTEGER NOT NULL,duration_sec INTEGER NOT NULL,"
                "created_ms INTEGER NOT NULL,participants_json TEXT,participants_private_json TEXT)"
            )
            valid = source_row(1)
            excluded = source_row(2, queue_id=450, patch="16.8.1")
            excluded["participants_json"] = "not-json"
            excluded["participants_private_json"] = "not-json"
            for row in (valid, excluded):
                connection.execute(
                    "INSERT INTO games VALUES (?,?,?,?,?,?,?,?)",
                    tuple(row[column] for column in snapshot_module.SOURCE_COLUMNS_V1),
                )
            connection.commit()
            connection.close()
            destination = root / "snapshot.sqlite"
            manifest_path = root / "snapshot.manifest.json"
            original_project = snapshot_module._project_player_history_source_row_v1
            with mock.patch.object(
                snapshot_module,
                "_project_player_history_source_row_v1",
                wraps=original_project,
            ) as project:
                result = build_and_publish_player_history_snapshot_from_sqlite_v1(
                    source=source,
                    destination=destination,
                    manifest=manifest_path,
                    config=config(),
                )
            self.assertEqual(project.call_count, 1)
            self.assertEqual(result.selected_source_rows, 1)
            self.assertEqual(_snapshot_player(destination)[0], 1)
            manifest = json.loads(manifest_path.read_text(encoding="ascii"))
            self.assertEqual(manifest["source_class"], "operator-sqlite-backup")
            self.assertEqual(manifest["selected_source_rows"], 1)
            self.assertEqual(manifest["max_created_ms"], 1000)
            self.assertEqual(manifest["snapshot_row_count"], result.row_count)
            self.assertEqual(
                manifest["snapshot_sha256"],
                hashlib.sha256(destination.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                set(manifest["source_identity"]),
                {
                    "device",
                    "inode",
                    "start_size",
                    "end_size",
                    "start_mtime_ns",
                    "end_mtime_ns",
                },
            )
            self.assertEqual(
                manifest["source_identity"]["start_size"],
                manifest["source_identity"]["end_size"],
            )
            self.assertEqual(
                manifest["source_identity"]["start_mtime_ns"],
                manifest["source_identity"]["end_mtime_ns"],
            )
            manifest_bytes = manifest_path.read_bytes()
            for canary in (
                b"Player1",
                b"TW01",
                b"00000000-0000",
                str(source).encode(),
                LOOKUP_SECRET,
                EVENT_SECRET,
            ):
                self.assertNotIn(canary, manifest_bytes)

            owner = root / "owner.sqlite"
            owner.write_bytes(b"owner")
            owner_manifest = root / "owner.manifest.json"
            with self.assertRaisesRegex(
                PlayerHistorySnapshotError, "^destination_exists$"
            ):
                build_and_publish_player_history_snapshot_from_sqlite_v1(
                    source=source,
                    destination=owner,
                    manifest=owner_manifest,
                    config=config(),
                )
            self.assertEqual(owner.read_bytes(), b"owner")
            self.assertFalse(owner_manifest.exists())

            manifest_owner = root / "manifest-owner.json"
            manifest_owner.write_bytes(b"manifest-owner")
            blocked_snapshot = root / "blocked.sqlite"
            with self.assertRaisesRegex(
                PlayerHistorySnapshotError, "^destination_exists$"
            ):
                build_and_publish_player_history_snapshot_from_sqlite_v1(
                    source=source,
                    destination=blocked_snapshot,
                    manifest=manifest_owner,
                    config=config(),
                )
            self.assertFalse(blocked_snapshot.exists())
            self.assertEqual(manifest_owner.read_bytes(), b"manifest-owner")

            race_snapshot = root / "race.sqlite"
            race_manifest = root / "race.manifest.json"
            real_link = os.link

            def race_snapshot_commit(source_path, target_path):
                if Path(target_path) == race_snapshot:
                    race_snapshot.write_bytes(b"racer")
                    raise FileExistsError
                return real_link(source_path, target_path)

            with mock.patch.object(
                snapshot_module.os, "link", side_effect=race_snapshot_commit
            ):
                with self.assertRaisesRegex(
                    PlayerHistorySnapshotError, "^destination_exists$"
                ):
                    build_and_publish_player_history_snapshot_from_sqlite_v1(
                        source=source,
                        destination=race_snapshot,
                        manifest=race_manifest,
                        config=config(),
                    )
            self.assertEqual(race_snapshot.read_bytes(), b"racer")
            self.assertFalse(race_manifest.exists())
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_trusted_primary_root_supports_normal_and_linked_checkouts(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            normal = root / "normal"
            (normal / ".git").mkdir(parents=True)
            with mock.patch.object(
                snapshot_module,
                "_module_repository_root_v1",
                return_value=normal,
            ):
                self.assertEqual(
                    snapshot_module._trusted_primary_checkout_root_v1(), normal
                )

            primary = root / "primary"
            gitdir = primary / ".git" / "worktrees" / "linked"
            gitdir.mkdir(parents=True)
            linked = root / "linked"
            linked.mkdir()
            (linked / ".git").write_text(
                f"gitdir: {gitdir.as_posix()}\n", encoding="ascii"
            )
            with mock.patch.object(
                snapshot_module,
                "_module_repository_root_v1",
                return_value=linked,
            ):
                self.assertEqual(
                    snapshot_module._trusted_primary_checkout_root_v1(), primary
                )
                self.assertEqual(
                    snapshot_module._trusted_live_database_path_v1(),
                    primary / "data" / "lcu" / "games.db",
                )

    def test_trusted_primary_root_rejects_malformed_relative_and_alternate_gitdir(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            linked = root / "linked"
            linked.mkdir()
            alternate = root / "primary" / ".git" / "alternate" / "linked"
            alternate.mkdir(parents=True)
            cases = (
                "gitdir: relative/.git/worktrees/linked\n",
                "gitdir: relative\ngitdir: second\n",
                f"gitdir: {alternate.as_posix()}\n",
                "not-gitdir: invalid\n",
            )
            for content in cases:
                with self.subTest(content=content):
                    (linked / ".git").write_text(content, encoding="ascii")
                    with mock.patch.object(
                        snapshot_module,
                        "_module_repository_root_v1",
                        return_value=linked,
                    ):
                        with self.assertRaisesRegex(
                            PlayerHistorySnapshotError, "^invalid_source$"
                        ):
                            snapshot_module._trusted_primary_checkout_root_v1()

    def test_trusted_primary_root_rejects_reparse_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            primary = root / "primary"
            gitdir = primary / ".git" / "worktrees" / "linked"
            gitdir.mkdir(parents=True)
            linked = root / "linked"
            linked.mkdir()
            dot_git = linked / ".git"
            dot_git.write_text(f"gitdir: {gitdir.as_posix()}\n", encoding="ascii")
            rejected_inode = os.lstat(primary / ".git").st_ino
            original_detector = snapshot_module._is_reparse_or_symlink_v1

            def detect(status):
                return status.st_ino == rejected_inode or original_detector(status)

            with (
                mock.patch.object(
                    snapshot_module,
                    "_module_repository_root_v1",
                    return_value=linked,
                ),
                mock.patch.object(
                    snapshot_module,
                    "_is_reparse_or_symlink_v1",
                    side_effect=detect,
                ),
            ):
                with self.assertRaisesRegex(
                    PlayerHistorySnapshotError, "^invalid_source$"
                ):
                    snapshot_module._trusted_primary_checkout_root_v1()

    def test_current_linked_worktree_resolves_existing_primary_live_db(self) -> None:
        module_root = Path(snapshot_module.__file__).resolve().parents[3]
        dot_git = module_root / ".git"
        if not dot_git.is_file():
            self.skipTest("current checkout is not a linked worktree")
        line = dot_git.read_text(encoding="ascii").strip()
        expected_gitdir = Path(line.removeprefix("gitdir: ")).resolve(strict=True)
        expected = expected_gitdir.parents[2] / "data" / "lcu" / "games.db"
        if not expected.is_file():
            self.skipTest("linked primary checkout has no local live database")
        self.assertEqual(snapshot_module._trusted_live_database_path_v1(), expected)

    def test_determinism_duplicate_policy_outcomes_buckets_and_low_sample(self) -> None:
        rows = [
            source_row(101, created_ms=1000, duration_sec=899, blue_wins=1),
            source_row(102, created_ms=2000, duration_sec=900, blue_wins=0),
            source_row(103, created_ms=3000, duration_sec=1200, blue_wins=1),
            source_row(104, created_ms=4000, duration_sec=1500, blue_wins=1),
        ]
        rows.append(deepcopy(rows[0]))
        conflict = source_row(104, created_ms=4000, duration_sec=1499, blue_wins=1)
        rows.append(conflict)
        first = build_player_history_graph_v1(rows, config=config())
        second = build_player_history_graph_v1(reversed(rows), config=config())
        self.assertEqual(first, second)
        exclusions = json.loads(first.meta.exclusions_json)
        self.assertEqual(exclusions["duplicate_event"], 3)
        player_key = derive_lookup_key(
            LOOKUP_SECRET,
            expected_normalizer_id=NORMALIZER_ID,
            normalized_riot_id=normalize_riot_id_v1("Player1#TW01"),
        )
        player = next(item for item in first.lookups if item.lookup_key == player_key)
        self.assertEqual(player.low_sample, 1)
        histories = [row for row in first.histories if row.lookup_key == player.lookup_key]
        self.assertEqual([row.ordinal for row in histories], [1, 2, 3])
        self.assertEqual(
            [row.duration_bucket for row in histories],
            ["20_25m", "15_20m", "lt_15m"],
        )
        self.assertEqual([row.outcome for row in histories], ["win", "loss", "win"])

    def test_lookup_ambiguity_has_zero_history(self) -> None:
        graph = build_player_history_graph_v1(
            [
                source_row(1, first_name="SharedName"),
                source_row(
                    2,
                    first_name="SharedName",
                    first_puuid="10000000-0000-0000-0000-000000000001",
                ),
            ],
            config=config(),
        )
        ambiguous = [row for row in graph.lookups if row.status == "ambiguous"]
        self.assertEqual(len(ambiguous), 1)
        self.assertFalse(any(row.lookup_key == ambiguous[0].lookup_key for row in graph.histories))

    def test_alias_rename_does_not_link_history_across_aliases(self) -> None:
        graph = build_player_history_graph_v1(
            [
                source_row(1, first_name="OldAlias"),
                source_row(2, first_name="NewAlias"),
            ],
            config=config(),
        )
        keys = {
            alias: derive_lookup_key(
                LOOKUP_SECRET,
                expected_normalizer_id=NORMALIZER_ID,
                normalized_riot_id=normalize_riot_id_v1(f"{alias}#TW01"),
            )
            for alias in ("OldAlias", "NewAlias")
        }
        lookups = {row.lookup_key: row for row in graph.lookups}
        for key in keys.values():
            self.assertEqual(lookups[key].observed_matches, 1)
            self.assertEqual(
                [row.ordinal for row in graph.histories if row.lookup_key == key],
                [1],
            )

    def test_publish_audits_and_contains_no_raw_private_material(self) -> None:
        graph = build_player_history_graph_v1([source_row()], config=config())
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "snapshot.sqlite"
            publish_player_history_snapshot_v1(destination, graph)
            connection = sqlite3.connect(destination)
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                self.assertEqual(audit_player_history_snapshot_v1(connection), graph)
            finally:
                connection.close()
            raw = destination.read_bytes()
            for canary in (
                b"Player1",
                b"TW01",
                b"00000000-0000",
                b"NEVER-PUBLISH-THIS",
                LOOKUP_SECRET,
                EVENT_SECRET,
            ):
                self.assertNotIn(canary, raw)

    def test_publish_no_clobber_race_and_audit_failure_cleanup(self) -> None:
        graph = build_player_history_graph_v1([source_row()], config=config())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "snapshot.sqlite"

            def race_link(_source, target):
                Path(target).write_bytes(b"racer")
                raise FileExistsError

            with mock.patch("aram_nn.site.player_history_snapshot.os.link", side_effect=race_link):
                with self.assertRaisesRegex(PlayerHistorySnapshotError, "^destination_exists$"):
                    publish_player_history_snapshot_v1(destination, graph)
            self.assertEqual(destination.read_bytes(), b"racer")
            self.assertEqual(list(root.glob("*.tmp")), [])

            destination.unlink()
            with mock.patch(
                "aram_nn.site.player_history_snapshot.audit_player_history_snapshot_v1",
                side_effect=RuntimeError("forced"),
            ):
                with self.assertRaisesRegex(PlayerHistorySnapshotError, "^snapshot_failed$"):
                    publish_player_history_snapshot_v1(destination, graph)
            self.assertFalse(destination.exists())
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_source_adapter_projection_schema_drift_and_live_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "copy.sqlite"
            connection = sqlite3.connect(source)
            connection.execute(
                "CREATE TABLE games (game_id TEXT PRIMARY KEY, queue_id INTEGER NOT NULL, patch TEXT NOT NULL, "
                "blue_champs TEXT, blue_wins INTEGER NOT NULL, duration_sec INTEGER NOT NULL, created_ms INTEGER NOT NULL, "
                "participants_json TEXT, participants_private_json TEXT)"
            )
            row = source_row()
            fields = tuple(row)
            connection.execute(
                f"INSERT INTO games ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                tuple(row[name] for name in fields),
            )
            connection.commit()
            connection.close()
            self.assertEqual(list(read_player_history_source_sqlite_v1(source)), [row])

            iterator = read_player_history_source_sqlite_v1(source)
            self.assertEqual(next(iterator), row)
            source.touch()
            with self.assertRaisesRegex(PlayerHistorySnapshotError, "^invalid_source$"):
                next(iterator)

            companion = Path(f"{source}-wal")
            companion.touch()
            with self.assertRaisesRegex(PlayerHistorySnapshotError, "^invalid_source$"):
                list(read_player_history_source_sqlite_v1(source))
            companion.unlink()

            drift = Path(directory) / "drift.sqlite"
            connection = sqlite3.connect(drift)
            connection.execute("CREATE TABLE games (game_id INTEGER)")
            connection.close()
            with self.assertRaisesRegex(PlayerHistorySnapshotError, "^source_schema_invalid$"):
                list(read_player_history_source_sqlite_v1(drift))

        live = Path(__file__).resolve().parents[1] / "data" / "lcu" / "games.db"
        with mock.patch.object(Path, "resolve", return_value=live.resolve()):
            with self.assertRaisesRegex(PlayerHistorySnapshotError, "^invalid_source$"):
                list(read_player_history_source_sqlite_v1(Path("explicit-copy")))

        alternate_root = Path(tempfile.mkdtemp()) / "another-worktree" / "data" / "lcu"
        try:
            alternate_root.mkdir(parents=True)
            alternate_live = alternate_root / "games.db"
            alternate_live.touch()
            with self.assertRaisesRegex(PlayerHistorySnapshotError, "^invalid_source$"):
                list(read_player_history_source_sqlite_v1(alternate_live))
        finally:
            import shutil

            shutil.rmtree(alternate_root.parents[2])

    def test_live_wal_reads_committed_rows_and_filters_queue_and_patch(self) -> None:
        rows = [
            source_row(1, created_ms=1),
            source_row(2, created_ms=2, queue_id=450),
            source_row(3, created_ms=3, patch="16.8.9"),
            source_row(4, created_ms=4, patch="16.9.7"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "games.db"
            destination = root / "snapshot.sqlite"
            writer = _open_live_database(source, rows)
            try:
                self.assertTrue(Path(f"{source}-wal").exists())
                with mock.patch.object(
                    snapshot_module,
                    "_trusted_live_database_path_v1",
                    return_value=source,
                ):
                    graph = build_and_publish_player_history_snapshot_from_live_v1(
                        destination=destination,
                        manifest=root / "snapshot.manifest.json",
                        config=config(),
                    )
                observed, histories = _snapshot_player(destination)
                self.assertEqual(observed, 2)
                self.assertEqual({row[1] for row in histories}, {"16.10", "16.9"})
                self.assertEqual(writer.execute("SELECT count(*) FROM games").fetchone(), (4,))
                manifest_path = root / "snapshot.manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="ascii"))
                self.assertEqual(
                    set(manifest),
                    {
                        "schema_version",
                        "dataset_id",
                        "region",
                        "queue_id",
                        "patches",
                        "coverage",
                        "generated_date",
                        "build_started_utc",
                        "source_class",
                        "source_identities",
                        "selected_source_rows",
                        "max_created_ms",
                        "exclusions",
                        "snapshot_row_count",
                        "snapshot_sha256",
                        "audit_status",
                        "public_identifiers_emitted",
                    },
                )
                self.assertEqual(manifest["schema_version"], 1)
                self.assertEqual(manifest["dataset_id"], "history-test")
                self.assertEqual(manifest["region"], "TW")
                self.assertEqual(manifest["queue_id"], 2400)
                self.assertEqual(manifest["patches"], ["16.10", "16.9"])
                self.assertEqual(manifest["coverage"], "captured-subset")
                self.assertEqual(manifest["generated_date"], "2026-08-13")
                self.assertRegex(manifest["build_started_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
                self.assertEqual(
                    manifest["source_class"], "git-common-primary/data/lcu/games.db"
                )
                self.assertEqual(set(manifest["source_identities"]), {"db", "wal", "shm"})
                for identity in manifest["source_identities"].values():
                    self.assertEqual(
                        set(identity), {"device", "inode", "start_size", "end_size"}
                    )
                self.assertEqual(manifest["selected_source_rows"], 2)
                self.assertEqual(manifest["max_created_ms"], 4)
                self.assertEqual(manifest["exclusions"], json.loads(graph.meta.exclusions_json))
                self.assertEqual(manifest["snapshot_row_count"], graph.row_count)
                self.assertEqual(
                    manifest["snapshot_sha256"], hashlib.sha256(destination.read_bytes()).hexdigest()
                )
                self.assertEqual(manifest["audit_status"], "ok")
                self.assertIs(manifest["public_identifiers_emitted"], False)
                raw_manifest = manifest_path.read_bytes()
                for canary in (
                    b"Player1",
                    b"TW01",
                    b"00000000-0000",
                    b"NEVER-PUBLISH-THIS",
                    str(source).encode(),
                    LOOKUP_SECRET,
                    EVENT_SECRET,
                ):
                    self.assertNotIn(canary, raw_manifest)
                audit = sqlite3.connect(destination)
                try:
                    audit.execute("PRAGMA foreign_keys=ON")
                    summary = audit_player_history_snapshot_streaming_v1(audit)
                    self.assertEqual(summary.meta, graph.meta)
                    self.assertEqual(summary.row_count, graph.row_count)
                finally:
                    audit.close()
            finally:
                writer.close()

    def test_live_read_is_one_snapshot_during_concurrent_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "games.db"
            writer = _open_live_database(
                source,
                [source_row(1, created_ms=1), source_row(2, created_ms=2)],
            )
            original_project = snapshot_module._project_player_history_source_row_v1
            appended = False

            def project_while_appending(*args, **kwargs):
                nonlocal appended
                result = original_project(*args, **kwargs)
                if not appended:
                    appended = True
                    _insert_live_row(writer, source_row(3, created_ms=3))
                    writer.commit()
                return result

            try:
                with (
                    mock.patch.object(
                        snapshot_module,
                        "_trusted_live_database_path_v1",
                        return_value=source,
                    ),
                    mock.patch.object(
                        snapshot_module,
                        "_project_player_history_source_row_v1",
                        side_effect=project_while_appending,
                    ),
                ):
                    first_graph = build_and_publish_player_history_snapshot_from_live_v1(
                        destination=root / "first.sqlite",
                        manifest=root / "first.manifest.json",
                        config=config(),
                    )
                self.assertEqual(_snapshot_player(root / "first.sqlite")[0], 2)
                first_manifest = json.loads(
                    (root / "first.manifest.json").read_text(encoding="ascii")
                )
                self.assertEqual(
                    (first_manifest["selected_source_rows"], first_manifest["max_created_ms"]),
                    (2, 2),
                )
                with mock.patch.object(
                    snapshot_module,
                    "_trusted_live_database_path_v1",
                    return_value=source,
                ):
                    second_graph = build_and_publish_player_history_snapshot_from_live_v1(
                        destination=root / "second.sqlite",
                        manifest=root / "second.manifest.json",
                        config=config(),
                    )
                self.assertEqual(_snapshot_player(root / "second.sqlite")[0], 3)
                second_manifest = json.loads(
                    (root / "second.manifest.json").read_text(encoding="ascii")
                )
                self.assertEqual(
                    (second_manifest["selected_source_rows"], second_manifest["max_created_ms"]),
                    (3, 3),
                )
            finally:
                writer.close()

    def test_live_pair_failure_cleanup_and_no_clobber_races(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "games.db"
            writer = _open_live_database(source, [source_row()])
            try:
                with (
                    mock.patch.object(
                        snapshot_module,
                        "_trusted_live_database_path_v1",
                        return_value=source,
                    ),
                    mock.patch.object(
                        snapshot_module,
                        "_write_manifest_temp_v1",
                        side_effect=RuntimeError("forced"),
                    ),
                ):
                    with self.assertRaisesRegex(
                        PlayerHistorySnapshotError, "^snapshot_failed$"
                    ):
                        build_and_publish_player_history_snapshot_from_live_v1(
                            destination=root / "serialization.sqlite",
                            manifest=root / "serialization.manifest.json",
                            config=config(),
                        )
                self.assertFalse((root / "serialization.sqlite").exists())
                self.assertFalse((root / "serialization.manifest.json").exists())

                destination = root / "link-failure.sqlite"
                manifest = root / "link-failure.manifest.json"
                real_link = os.link

                def fail_snapshot_link(source_path, target_path):
                    if Path(target_path) == destination:
                        raise OSError("forced")
                    return real_link(source_path, target_path)

                with (
                    mock.patch.object(
                        snapshot_module,
                        "_trusted_live_database_path_v1",
                        return_value=source,
                    ),
                    mock.patch.object(snapshot_module.os, "link", side_effect=fail_snapshot_link),
                ):
                    with self.assertRaisesRegex(
                        PlayerHistorySnapshotError, "^publish_failed$"
                    ):
                        build_and_publish_player_history_snapshot_from_live_v1(
                            destination=destination,
                            manifest=manifest,
                            config=config(),
                        )
                self.assertFalse(destination.exists())
                self.assertFalse(manifest.exists())

                existing_manifest = root / "existing.manifest.json"
                existing_manifest.write_bytes(b"owner")
                with mock.patch.object(
                    snapshot_module,
                    "_trusted_live_database_path_v1",
                    return_value=source,
                ):
                    with self.assertRaisesRegex(
                        PlayerHistorySnapshotError, "^destination_exists$"
                    ):
                        build_and_publish_player_history_snapshot_from_live_v1(
                            destination=root / "existing.sqlite",
                            manifest=existing_manifest,
                            config=config(),
                        )
                self.assertFalse((root / "existing.sqlite").exists())
                self.assertEqual(existing_manifest.read_bytes(), b"owner")

                race_destination = root / "race.sqlite"
                race_manifest = root / "race.manifest.json"

                def race_snapshot_link(source_path, target_path):
                    if Path(target_path) == race_destination:
                        race_destination.write_bytes(b"racer")
                        raise FileExistsError
                    return real_link(source_path, target_path)

                with (
                    mock.patch.object(
                        snapshot_module,
                        "_trusted_live_database_path_v1",
                        return_value=source,
                    ),
                    mock.patch.object(snapshot_module.os, "link", side_effect=race_snapshot_link),
                ):
                    with self.assertRaisesRegex(
                        PlayerHistorySnapshotError, "^destination_exists$"
                    ):
                        build_and_publish_player_history_snapshot_from_live_v1(
                            destination=race_destination,
                            manifest=race_manifest,
                            config=config(),
                        )
                self.assertEqual(race_destination.read_bytes(), b"racer")
                self.assertFalse(race_manifest.exists())
                self.assertEqual(list(root.glob("*.tmp")), [])
            finally:
                writer.close()

    def test_live_connection_is_query_only_and_uri_is_not_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "games.db"
            writer = _open_live_database(source, [source_row()])
            configured = sqlite3.connect(source)
            try:
                snapshot_module._configure_live_connection_v1(configured)
                self.assertEqual(configured.execute("PRAGMA query_only").fetchone(), (1,))
                self.assertEqual(configured.execute("PRAGMA trusted_schema").fetchone(), (0,))
                self.assertEqual(configured.execute("PRAGMA temp_store").fetchone(), (2,))
                self.assertEqual(configured.execute("PRAGMA busy_timeout").fetchone(), (250,))
                with self.assertRaises(sqlite3.OperationalError):
                    configured.execute("INSERT INTO games (game_id) VALUES ('forbidden')")
            finally:
                configured.close()

            real_connect = sqlite3.connect
            opened: list[tuple[object, dict[str, object]]] = []

            def recording_connect(database, *args, **kwargs):
                opened.append((database, kwargs.copy()))
                return real_connect(database, *args, **kwargs)

            try:
                with (
                    mock.patch.object(
                        snapshot_module,
                        "_trusted_live_database_path_v1",
                        return_value=source,
                    ),
                    mock.patch.object(
                        snapshot_module.sqlite3,
                        "connect",
                        side_effect=recording_connect,
                    ),
                ):
                    build_and_publish_player_history_snapshot_from_live_v1(
                        destination=root / "snapshot.sqlite",
                        manifest=root / "snapshot.manifest.json",
                        config=config(),
                    )
                live_uri, live_kwargs = opened[0]
                self.assertTrue(str(live_uri).endswith("?mode=ro"))
                self.assertNotIn("immutable", str(live_uri))
                self.assertEqual(live_kwargs.get("uri"), True)
            finally:
                writer.close()

    def test_live_deadline_and_wal_growth_abort_close_reader(self) -> None:
        for constant in (
            "_LIVE_READ_MAX_SECONDS_V1",
            "_LIVE_WAL_MAX_GROWTH_BYTES_V1",
        ):
            with self.subTest(constant=constant), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "games.db"
                destination = root / "snapshot.sqlite"
                writer = _open_live_database(source, [source_row()])
                try:
                    with (
                        mock.patch.object(
                            snapshot_module,
                            "_trusted_live_database_path_v1",
                            return_value=source,
                        ),
                        mock.patch.object(snapshot_module, constant, -1),
                    ):
                        with self.assertRaisesRegex(
                            PlayerHistorySnapshotError, "^invalid_source$"
                        ):
                            build_and_publish_player_history_snapshot_from_live_v1(
                                destination=destination,
                                manifest=root / "snapshot.manifest.json",
                                config=config(),
                            )
                    self.assertFalse(destination.exists())
                    _insert_live_row(writer, source_row(999, created_ms=999))
                    writer.commit()
                    checkpoint = writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    self.assertEqual(checkpoint[0], 0)
                finally:
                    writer.close()

    def test_live_rejects_reparse_database_and_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "games.db"
            writer = _open_live_database(source, [source_row()])
            original_detector = snapshot_module._is_reparse_or_symlink_v1
            try:
                for rejected_path in (source, Path(f"{source}-wal")):
                    rejected_inode = os.lstat(rejected_path).st_ino

                    def detect(status, *, inode=rejected_inode):
                        return status.st_ino == inode or original_detector(status)

                    with (
                        self.subTest(path=rejected_path.name),
                        mock.patch.object(
                            snapshot_module,
                            "_trusted_live_database_path_v1",
                            return_value=source,
                        ),
                        mock.patch.object(
                            snapshot_module,
                            "_is_reparse_or_symlink_v1",
                            side_effect=detect,
                        ),
                    ):
                        with self.assertRaisesRegex(
                            PlayerHistorySnapshotError, "^invalid_source$"
                        ):
                            build_and_publish_player_history_snapshot_from_live_v1(
                                destination=root / f"{rejected_path.name}.sqlite",
                                manifest=root / f"{rejected_path.name}.manifest.json",
                                config=config(),
                            )
            finally:
                writer.close()

    def test_live_rejects_exact_schema_descriptor_mutation_and_non_table(self) -> None:
        mutated_schema = LIVE_SCHEMA.replace(
            "seed_family TEXT NOT NULL DEFAULT ''",
            "seed_family TEXT NOT NULL DEFAULT 'changed'",
        )
        for schema in (
            mutated_schema,
            "CREATE VIEW games AS SELECT '1' AS game_id",
        ):
            with self.subTest(schema=schema[:20]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "games.db"
                writer = _open_live_database(source, [], schema=schema)
                try:
                    with mock.patch.object(
                        snapshot_module,
                        "_trusted_live_database_path_v1",
                        return_value=source,
                    ):
                        with self.assertRaisesRegex(
                            PlayerHistorySnapshotError, "^source_schema_invalid$"
                        ):
                            build_and_publish_player_history_snapshot_from_live_v1(
                                destination=root / "snapshot.sqlite",
                                manifest=root / "snapshot.manifest.json",
                                config=config(),
                            )
                finally:
                    writer.close()

    def test_cli_source_and_live_source_are_mutually_exclusive(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite"
            source.touch()
            base_without_manifest = [
                "--destination",
                str(root / "snapshot.sqlite"),
                "--dataset-id",
                "history-test",
                "--patch",
                "16.10",
                "--generated-date",
                "2026-08-13",
            ]
            base = [
                "--manifest",
                str(root / "private.json"),
                *base_without_manifest,
            ]
            environment = {
                "ARAM_PLAYER_HISTORY_LOOKUP_SECRET_HEX": LOOKUP_SECRET.hex(),
                "ARAM_PLAYER_HISTORY_EVENT_SECRET_HEX": EVENT_SECRET.hex(),
            }
            for selectors in ([], ["--source", str(source), "--live-source"]):
                with self.subTest(selectors=selectors):
                    result = runner.invoke(main, [*selectors, *base], env=environment)
                    self.assertNotEqual(result.exit_code, 0)
                    self.assertIn("invalid_source", result.output)
            for selector in (["--live-source"], ["--source", str(source)]):
                with self.subTest(missing_manifest=selector):
                    missing = runner.invoke(
                        main, [*selector, *base_without_manifest], env=environment
                    )
                    self.assertNotEqual(missing.exit_code, 0)
                    self.assertIn("Missing option '--manifest'", missing.output)
            offline = runner.invoke(
                main, ["--source", str(source), *base], env=environment
            )
            self.assertNotEqual(offline.exit_code, 0)
            self.assertIn("source_schema_invalid", offline.output)
            self.assertFalse((root / "snapshot.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
