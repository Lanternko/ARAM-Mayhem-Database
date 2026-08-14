from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from aram_nn.site.player_history_query import (
    PlayerHistoryQueryError,
    PlayerHistorySnapshotHandle,
    query_player_history_v1,
)
from aram_nn.site.player_history_snapshot import (
    build_player_history_graph_v1,
    publish_player_history_snapshot_v1,
)
from test_player_history_snapshot import EVENT_SECRET, LOOKUP_SECRET, config, source_row


class PlayerHistoryQueryTests(unittest.TestCase):
    def _snapshot(self, directory: str, rows) -> Path:
        destination = Path(directory) / "snapshot.sqlite"
        graph = build_player_history_graph_v1(rows, config=config())
        publish_player_history_snapshot_v1(destination, graph)
        return destination

    def test_happy_query_exact_allowlist_and_no_sensitive_echo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self._snapshot(directory, [source_row()])
            result = query_player_history_v1(
                snapshot_path=snapshot,
                riot_id="Player1#TW01",
                lookup_secret=LOOKUP_SECRET,
            )
        self.assertEqual(
            set(result),
            {"status", "snapshot", "observed_matches", "low_sample", "histories"},
        )
        self.assertEqual(set(result["snapshot"]), {"dataset_id", "patches", "generated_date"})
        self.assertEqual(
            set(result["histories"][0]),
            {"ordinal", "patch", "champion_id", "outcome", "duration_bucket"},
        )
        self.assertEqual(result["histories"][0]["outcome"], "win")
        encoded = json.dumps(result, ensure_ascii=False)
        for canary in (
            "Player1",
            "TW01",
            "00000000-0000",
            LOOKUP_SECRET.hex(),
            EVENT_SECRET.hex(),
            str(snapshot),
            "NEVER-PUBLISH-THIS",
            "lookup_key",
            "event_key",
        ):
            self.assertNotIn(canary, encoded)

    def test_unknown_and_ambiguous_are_uniform(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self._snapshot(
                directory,
                [
                    source_row(1, first_name="SharedName"),
                    source_row(
                        2,
                        first_name="SharedName",
                        first_puuid="10000000-0000-0000-0000-000000000001",
                    ),
                ],
            )
            unknown = query_player_history_v1(
                snapshot_path=snapshot,
                riot_id="Unknown#TW00",
                lookup_secret=LOOKUP_SECRET,
            )
            ambiguous = query_player_history_v1(
                snapshot_path=snapshot,
                riot_id="SharedName#TW01",
                lookup_secret=LOOKUP_SECRET,
            )
        self.assertEqual(
            set(unknown),
            {"status", "snapshot", "observed_matches", "low_sample", "histories"},
        )
        self.assertEqual(
            (unknown["status"], unknown["observed_matches"], unknown["low_sample"], unknown["histories"]),
            ("not_found", None, None, []),
        )
        self.assertIsNone(unknown["snapshot"])
        self.assertEqual(ambiguous, unknown)

    def test_handle_audits_once_then_reuses_only_public_metadata(self) -> None:
        from unittest import mock
        import aram_nn.site.player_history_query as query_module

        with tempfile.TemporaryDirectory() as directory:
            snapshot = self._snapshot(directory, [source_row()])
            with mock.patch.object(
                query_module,
                "audit_player_history_snapshot_v1",
                wraps=query_module.audit_player_history_snapshot_v1,
            ) as audit:
                handle = PlayerHistorySnapshotHandle.open(snapshot)
                for _ in range(3):
                    result = handle.query(riot_id="Player1#TW01", lookup_secret=LOOKUP_SECRET)
                    self.assertEqual(result["status"], "ready")
                self.assertEqual(audit.call_count, 1)

    def test_malformed_input_and_corrupt_snapshot_have_stable_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self._snapshot(directory, [source_row()])
            with self.assertRaisesRegex(PlayerHistoryQueryError, "^invalid_query$") as caught:
                query_player_history_v1(
                    snapshot_path=snapshot,
                    riot_id="private-canary",
                    lookup_secret=LOOKUP_SECRET,
                )
            self.assertNotIn("private-canary", repr(caught.exception))

            connection = sqlite3.connect(snapshot)
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute("UPDATE sqlite_schema SET sql='CREATE TABLE broken(x)' WHERE name='player_lookup'")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(PlayerHistoryQueryError, "^snapshot_invalid$"):
                query_player_history_v1(
                    snapshot_path=snapshot,
                    riot_id="Player1#TW01",
                    lookup_secret=LOOKUP_SECRET,
                )


if __name__ == "__main__":
    unittest.main()
