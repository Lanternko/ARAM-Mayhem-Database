"""A closed patch without a snapshot must not block the build.

Three things are proven here: the build-side deferral serves stale-or-empty
instead of scanning (and says what it skipped), the background scan resumes
from its checkpoints and still equals a single plain pass, and the worker loop
keeps slicing until the settle reports done -- without starting a real process.
"""
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import settle_patch  # noqa: E402
import tierlist_engine as engine  # noqa: E402
from aram_nn import patch_snapshot  # noqa: E402
from aram_nn.heavy_jobs import ResourcePressure, exclusive_lock  # noqa: E402
from aram_nn.site import chunk_cache, patch_extract, settle_background  # noqa: E402

BLUE = [1, 2, 3, 4, 5]
RED = [6, 7, 8, 9, 10]
ITEM_META = {
    3001: {"id": 3001, "price_total": 3000, "categories": ["Damage"], "name_en": "Core One"},
    1001: {"id": 1001, "price_total": 300, "categories": ["Boots"], "name_en": "Boots"},
}
# Four games per checkpoint window.
STEP_MS = chunk_cache.WINDOW_MS // 4


def _participants(blue_wins: int, augment_base: int) -> str:
    rows = []
    for cid in BLUE:
        rows.append({"teamId": 100, "championId": cid, "augments": [augment_base + cid % 3],
                     "items": [3001, 1001]})
    for cid in RED:
        rows.append({"teamId": 200, "championId": cid, "augments": [augment_base + 10 + cid % 3],
                     "items": [3001, 1001]})
    return json.dumps(rows)


def _make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE games (
            game_id TEXT PRIMARY KEY, queue_id INTEGER NOT NULL,
            patch TEXT NOT NULL, blue_champs TEXT NOT NULL,
            red_champs TEXT NOT NULL, blue_wins INTEGER NOT NULL,
            duration_sec INTEGER, created_ms INTEGER,
            captured_at TEXT, participants_json TEXT,
            seed_family TEXT, participants_private_json TEXT
        )"""
    )
    con.execute("CREATE INDEX idx_games_queue_patch_created ON games(queue_id, patch, created_ms)")
    con.commit()
    con.close()


def _add_games(path: Path, patch: str, count: int, *, prefix: str, augment_base: int = 70,
               start: int = 0) -> None:
    con = sqlite3.connect(path)
    for i in range(start, start + count):
        blue_wins = i % 2
        con.execute(
            "INSERT INTO games VALUES (?,?,?,?,?,?,?,?,NULL,?,NULL,NULL)",
            (
                f"{prefix}{i}", 2400, f"{patch}.1", json.dumps(BLUE), json.dumps(RED),
                blue_wins, 1200, i * STEP_MS, _participants(blue_wins, augment_base),
            ),
        )
    con.commit()
    con.close()


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "games.db"
        self.snapshots = self.root / "patch_snapshots"
        self.store = self.root / "resume.sqlite"
        _make_db(self.db)
        _add_games(self.db, "16.14", 40, prefix="a")  # 10 windows

    def tearDown(self) -> None:
        chunk_cache.configure_resume(None)
        engine.set_settle_deferral(False)
        patch_extract.clear_scan_dbs()
        self._tmp.cleanup()


class WindowedFoldTests(_Base):
    def _plain(self):
        chunk_cache.configure_resume(None)
        return engine._scan_patch_counters(self.db, 2400, "16.14")

    def _run_until_done(self) -> tuple[dict, int]:
        """Slices with an already-expired deadline: one new window per slice."""
        interruptions = 0
        while True:
            chunk_cache.configure_resume(self.store, deadline=time.monotonic() - 1)
            try:
                return engine._scan_patch_counters(self.db, 2400, "16.14"), interruptions
            except chunk_cache.FoldInterrupted:
                interruptions += 1
                self.assertLess(interruptions, 500, "no progress between slices")

    def test_windowed_scan_equals_a_plain_pass(self) -> None:
        plain = self._plain()
        chunk_cache.configure_resume(self.store)
        self.assertEqual(plain, engine._scan_patch_counters(self.db, 2400, "16.14"))

    def test_interrupted_scan_resumes_and_matches(self) -> None:
        plain = self._plain()
        resumed, interruptions = self._run_until_done()
        self.assertEqual(plain, resumed)
        # Every slice was cut short after exactly one window, so the scan can
        # only have finished by picking the earlier windows up from the store.
        self.assertGreaterEqual(interruptions, 9)

    def test_finished_windows_are_not_folded_again(self) -> None:
        self._run_until_done()
        folds: list[int] = []
        original = engine._scan_patch_counters

        def counting_fold(rows):
            rows = list(rows)
            folds.append(len(rows))
            return len(rows)

        chunk_cache.configure_resume(self.store)
        # Same scan name + fingerprint parts as a throwaway fold of our own.
        del original
        first = engine._fold_patch(self.db, 2400, "16.14", scan="t", select="game_id",
                                   fold=counting_fold)
        self.assertEqual(first, 40)
        self.assertEqual(len(folds), 10)
        folds.clear()
        again = engine._fold_patch(self.db, 2400, "16.14", scan="t", select="game_id",
                                   fold=counting_fold)
        self.assertEqual(again, 40)
        self.assertEqual(folds, [])

    def test_a_window_that_gained_a_game_is_refolded(self) -> None:
        self._run_until_done()
        # A straggler lands in an already-checkpointed window (window 0).
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO games VALUES (?,?,?,?,?,?,?,?,NULL,?,NULL,NULL)",
            ("late", 2400, "16.14.1", json.dumps(BLUE), json.dumps(RED), 1, 1200, 5,
             _participants(1, 70)),
        )
        con.commit()
        con.close()
        chunk_cache.configure_resume(self.store)
        windowed = engine._scan_patch_counters(self.db, 2400, "16.14")
        self.assertEqual(self._plain(), windowed)
        self.assertEqual(windowed["games"][1], 41)

    def test_settle_one_survives_being_cut_into_slices(self) -> None:
        direct = engine.compute_winrates(self.db, 2400, "16.14")
        slices = 0
        while True:
            slices += 1
            chunk_cache.configure_resume(self.store, deadline=time.monotonic() - 1)
            try:
                settle_patch.settle_one(
                    self.db, 2400, "16.14", (engine.SNAPSHOT_CHAMP_SECTION,), 40,
                    snapshot_dir=self.snapshots, log=lambda _line: None,
                )
                break
            except chunk_cache.FoldInterrupted:
                # Nothing is frozen until the whole patch has been folded.
                self.assertFalse(patch_snapshot.snapshot_path(
                    "16.14", queue_id=2400, snapshot_dir=self.snapshots).exists())
                self.assertLess(slices, 500)
        self.assertGreater(slices, 1)
        chunk_cache.configure_resume(None)
        self.assertEqual(direct, engine.compute_settled_winrates(
            self.db, 2400, "16.14", snapshot_dir=self.snapshots))


class DeferralTests(_Base):
    def _settled(self):
        return engine.compute_settled_winrates(self.db, 2400, "16.14", snapshot_dir=self.snapshots)

    def test_missing_snapshot_is_left_empty_and_reported(self) -> None:
        engine.set_settle_deferral(True)
        champs, champ_aug, pairs = self._settled()
        self.assertEqual((champs, champ_aug, pairs), ([], [], []))
        self.assertEqual(
            engine.deferred_settles(), {(2400, "16.14"): {engine.SNAPSHOT_CHAMP_SECTION: "empty"}}
        )
        self.assertTrue(engine.settle_unavailable(2400, "16.14"))
        # Nothing was scanned, so nothing was frozen.
        self.assertFalse(patch_snapshot.snapshot_path(
            "16.14", queue_id=2400, snapshot_dir=self.snapshots).exists())

    def test_outgrown_snapshot_keeps_serving_its_old_values(self) -> None:
        before = self._settled()
        _add_games(self.db, "16.14", 8, prefix="tail", start=40)  # +20%: stale-growth
        engine.set_settle_deferral(True)
        self.assertEqual(before, self._settled())
        self.assertEqual(
            engine.deferred_settles(), {(2400, "16.14"): {engine.SNAPSHOT_CHAMP_SECTION: "stale"}}
        )
        self.assertFalse(engine.settle_unavailable(2400, "16.14"))

    def test_fresh_snapshot_is_not_deferred(self) -> None:
        before = self._settled()
        engine.set_settle_deferral(True)
        self.assertEqual(before, self._settled())
        self.assertEqual(engine.deferred_settles(), {})

    def test_without_deferral_the_scan_still_runs(self) -> None:
        self.assertEqual(self._settled(), engine.compute_winrates(self.db, 2400, "16.14"))
        self.assertEqual(engine.deferred_settles(), {})

    def test_item_and_destiny_sections_defer_too(self) -> None:
        engine.set_settle_deferral(True)
        stats = engine.settled_core_item_patch_stats(
            self.db, 2400, "16.14", ITEM_META, [], snapshot_dir=self.snapshots
        )
        self.assertEqual(dict(stats["item"]), {})
        self.assertEqual(
            engine.settled_champ_destiny_items(
                self.db, 2400, "16.14", {3001}, snapshot_dir=self.snapshots
            ),
            {},
        )
        self.assertEqual(
            engine.deferred_settles(),
            {(2400, "16.14"): {
                engine.SNAPSHOT_ITEM_SECTION: "empty",
                engine.SNAPSHOT_DESTINY_SECTION: "empty",
            }},
        )

    def test_an_empty_baseline_does_not_flag_every_augment_as_new(self) -> None:
        _add_games(self.db, "16.15", 40, prefix="b", augment_base=70)
        current = engine.compute_winrates(self.db, 2400, "16.15")[1]
        # What an unguarded empty baseline would do: everything looks new.
        self.assertTrue(engine.derive_new_augment_ids(current, []))
        engine.set_settle_deferral(True)
        baseline = engine.compute_settled_winrates(self.db, 2400, "16.14")[1] if False else \
            engine.compute_settled_winrates(self.db, 2400, "16.14", snapshot_dir=self.snapshots)[1]
        self.assertEqual(baseline, [])
        self.assertEqual(
            engine.derive_recent_augment_ids(
                self.db, 2400, "16.15", current, window=1,
                baseline_prefix="16.14", baseline_champ_aug=baseline,
            ),
            frozenset(),
        )


class _Result:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.lines: list[str] = []
        self.sleeps: list[float] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, outcomes, **kwargs) -> int:
        queue = list(outcomes)

        def runner(command, cwd=None):
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return _Result(outcome)

        return settle_background.run_worker(
            ["slice"], snapshot_dir=self.dir, runner=runner, admit=contextlib.nullcontext,
            sleep=self.sleeps.append, log=self.lines.append, **kwargs,
        )

    def test_keeps_slicing_until_the_settle_reports_done(self) -> None:
        more = settle_background.EXIT_MORE_WORK
        self.assertEqual(self._run([more, more, 0]), 0)
        # It left the heavy-job lock (and slept) between slices.
        self.assertEqual(self.sleeps, [settle_background.PAUSE_SEC] * 2)

    def test_a_memory_pressure_kill_is_retried_not_counted_as_failure(self) -> None:
        self.assertEqual(self._run([ResourcePressure("killed")] * 5 + [0]), 0)
        self.assertEqual(self.sleeps, [settle_background.PRESSURE_PAUSE_SEC] * 5)

    def test_repeated_real_failures_give_up(self) -> None:
        self.assertEqual(self._run([1, 1, 1]), 1)

    def test_progress_resets_the_failure_count(self) -> None:
        more = settle_background.EXIT_MORE_WORK
        self.assertEqual(self._run([1, 1, more, 1, 1, more, 0]), 0)

    def test_a_second_worker_backs_off(self) -> None:
        inner: list[int] = []

        def runner(command, cwd=None):
            inner.append(self._run([0]))  # same lock, while the first holds it
            return _Result(0)

        code = settle_background.run_worker(
            ["slice"], snapshot_dir=self.dir, runner=runner, admit=contextlib.nullcontext,
            sleep=self.sleeps.append, log=self.lines.append,
        )
        self.assertEqual((code, inner), (0, [0]))
        self.assertTrue(any("another background settle" in line for line in self.lines))

    def test_spawn_is_windowless_detached_and_logs_to_a_file(self) -> None:
        calls: list[tuple[list[str], dict]] = []
        command = settle_background.spawn_background_settle(
            "scripts/settle_patch.py", db="data/lcu/games.db", queue_id=2400,
            patches=["16.22:champ_counters", "16.22:champ_counters", "16.21"],
            snapshot_dir=self.dir, popen=lambda cmd, **kw: calls.append((cmd, kw)),
        )
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv, command)
        self.assertEqual(
            argv[1:],
            ["scripts/settle_patch.py", "--db", "data/lcu/games.db", "--queue", "2400",
             "--background", "--patch", "16.22:champ_counters", "--patch", "16.21"],
        )
        if sys.platform == "win32":
            self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(settle_background.log_path(self.dir).exists())

    def _fake_checkout(self) -> tuple[Path, Path]:
        """A throwaway 'worktree' with code, and a separate project owning data/."""
        source = self.dir / "worktree"
        (source / "src" / "aram_nn" / "site").mkdir(parents=True)
        (source / "src" / "aram_nn" / "__init__.py").write_text("X = 1\n", encoding="utf-8")
        (source / "src" / "aram_nn" / "site" / "mod.py").write_text("Y = 2\n", encoding="utf-8")
        (source / "src" / "aram_nn" / "__pycache__").mkdir()
        (source / "src" / "aram_nn" / "__pycache__" / "junk.pyc").write_bytes(b"x")
        (source / "scripts" / "nested").mkdir(parents=True)
        (source / "scripts" / "settle_patch.py").write_text("# settle\n", encoding="utf-8")
        (source / "scripts" / "overrides.json").write_text("{}", encoding="utf-8")
        (source / "scripts" / "nested" / "skip.py").write_text("", encoding="utf-8")
        snapshots = self.dir / "project" / "data" / "patch_snapshots"
        snapshots.mkdir(parents=True)
        return source, snapshots

    def test_worker_runs_from_its_own_copy_in_the_project_that_owns_data(self) -> None:
        source, snapshots = self._fake_checkout()
        calls: list[tuple[list[str], dict]] = []
        for _ in range(2):  # a second spawn replaces the copy cleanly
            command = settle_background.spawn_background_settle(
                "ignored.py", db="data/lcu/games.db", queue_id=2400, patches=["16.22"],
                source_root=source, snapshot_dir=snapshots,
                popen=lambda cmd, **kw: calls.append((cmd, kw)),
            )
        runtime = snapshots.resolve() / "runtime"
        argv, kwargs = calls[-1]
        self.assertEqual(argv, command)
        self.assertEqual(Path(argv[1]), runtime / "scripts" / "settle_patch.py")
        self.assertEqual(Path(kwargs["cwd"]), (self.dir / "project").resolve())
        self.assertEqual(Path(kwargs["env"]["PYTHONPATH"]), runtime / "src")
        if sys.platform == "win32":
            self.assertTrue(kwargs["creationflags"] & subprocess.IDLE_PRIORITY_CLASS)
        # Byte-identical code (the checkpoints are keyed on its hash), no junk.
        self.assertEqual(
            (runtime / "src" / "aram_nn" / "site" / "mod.py").read_bytes(),
            (source / "src" / "aram_nn" / "site" / "mod.py").read_bytes(),
        )
        self.assertTrue((runtime / "scripts" / "overrides.json").exists())
        self.assertFalse((runtime / "src" / "aram_nn" / "__pycache__").exists())
        self.assertFalse((runtime / "scripts" / "nested").exists())
        self.assertFalse(runtime.with_name("runtime.new").exists())
        # Nothing of the worker's depends on the worktree surviving.
        self.assertNotIn(str(source), " ".join(argv))

    def test_a_running_worker_is_left_alone(self) -> None:
        source, snapshots = self._fake_checkout()
        with exclusive_lock(settle_background.lock_path(snapshots)) as acquired:
            self.assertTrue(acquired)
            self.assertIsNone(settle_background.spawn_background_settle(
                "s.py", db="d", queue_id=2400, patches=["16.22"], source_root=source,
                snapshot_dir=snapshots, popen=lambda *a, **k: self.fail("spawned"),
                log=self.lines.append,
            ))
        # ...and the code it is running from was not swapped out under it.
        self.assertFalse((snapshots / "runtime").exists())
        self.assertTrue(any("already running" in line for line in self.lines))

    def test_the_environment_switch_keeps_a_test_build_from_spawning(self) -> None:
        with mock.patch.dict(os.environ, {settle_background.DISABLE_ENV: "1"}):
            self.assertIsNone(settle_background.spawn_background_settle(
                "s.py", db="d", queue_id=2400, patches=["16.22"], snapshot_dir=self.dir,
                popen=lambda *a, **k: self.fail("spawned"),
            ))

    def test_spawn_never_raises(self) -> None:
        def broken(*_args, **_kwargs):
            raise OSError("no such interpreter")

        self.assertIsNone(settle_background.spawn_background_settle(
            "s.py", db="d", queue_id=2400, patches=["16.22"], snapshot_dir=self.dir,
            popen=broken, log=self.lines.append,
        ))
        self.assertTrue(any("could not start" in line for line in self.lines))

    def test_nothing_pending_spawns_nothing(self) -> None:
        self.assertIsNone(settle_background.spawn_background_settle(
            "s.py", db="d", queue_id=2400, patches=[], snapshot_dir=self.dir,
            popen=lambda *a, **k: self.fail("spawned"),
        ))

    def test_patch_spec_parsing(self) -> None:
        self.assertEqual(settle_patch.parse_patch_spec("16.22"), ("16.22", settle_patch.ALL_SECTIONS))
        self.assertEqual(
            settle_patch.parse_patch_spec("16.21:champ_counters"),
            ("16.21", (engine.SNAPSHOT_CHAMP_SECTION,)),
        )


if __name__ == "__main__":
    unittest.main()
