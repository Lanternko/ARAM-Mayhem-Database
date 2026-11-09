"""Game-ID gap sweep: capture target-queue games by walking the TW game-ID sequence.

Why this exists
---------------
TW game IDs are one platform-wide ascending sequence, and the LCU endpoint
``/lol-match-history/v1/games/{id}`` resolves *any* of them -- not only games
in some player's recent history.  The snowball crawler only finds games that a
crawled player happened to play, which on 2026-11-09 covered ~48% of TW Mayhem.
Probing the IDs the database does not have closes that gap directly:

* ~46% of unknown IDs answer 200 (any LoL queue), ~54% answer 404 (TFT,
  customs and other non-LoL games);
* ~1 in 3 probes yields a new Mayhem game, versus snowball's per-player
  history walk;
* it never asks for a puuid's match list, so it does not feed the
  LeagueClient per-puuid history cache leak.

How it runs
-----------
One sweeper is an RPC-only producer in the single-writer fleet (see
``scripts/lcu_collector.py snowball-workers --id-gap-producers``).  It keeps an
ascending cursor in ``crawl_runtime_state`` (``id_gap:cursor``), asks the
writer which IDs of the next batch are already stored, probes only the rest,
and claims + commits a game only once its detail parsed as a target queue --
so 404s and other-queue games never leave claim rows behind.

It stays ``lag_hours`` behind real time: a game is only probed once a 200 at or
after its ID shows a creation time older than the lag, so an in-progress game
(which also answers 404) is never mistaken for a non-LoL ID and skipped.

Backfill
--------
The forward cursor cold-starts ``start_lookback_hours`` back, so everything
before that origin (``id_gap:origin``) is only as complete as snowball made it.
Whenever the forward sweep is caught up, the idle time between frontier checks
walks a second, descending cursor (``id_gap:back_cursor``) down from the
origin, so the forward sweep always keeps priority.  It stops for good
(``id_gap:back_done``) once it has crossed into the previous patch -- a run of
``backfill_older_patch_run`` consecutive LoL games older than the first patch
it saw -- or reached ``backfill_floor_id``.  After that the forward sweep alone
covers every later patch.
"""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, NamedTuple

from .poller import _parse_game_detail
from .sgp import SGPError, SGPMatchHistoryClient, SGPRateLimited, sgp_game_to_lcu_detail
from .snowball import (
    SnowballWriterClaimsStopped,
    SnowballWriterFacade,
    _extract_participant_puuids,
    _utc_now,
)

CURSOR_KEY = "id_gap:cursor"
ORIGIN_KEY = "id_gap:origin"
BACK_CURSOR_KEY = "id_gap:back_cursor"
BACK_PATCH_KEY = "id_gap:back_patch"
BACK_DONE_KEY = "id_gap:back_done"
SEED_FAMILY = "id_gap"
# 2400 Mayhem and 450 ARAM.  Deliberately narrower than the snowball fleet's
# queue list: sweeping every 4310 經典 game on the platform would multiply the
# database without serving the Mayhem product.
DEFAULT_QUEUES = frozenset({2400, 450})


class ProbeResult(NamedTuple):
    """One LCU answer.  status -1 = no HTTP answer (client down / restarting)."""

    status: int
    body: dict | None


@dataclass(frozen=True)
class IdGapConfig:
    target_queues: frozenset[int] = DEFAULT_QUEUES
    rps: float = 2.0
    # Requests in flight.  LCU latency is long-tailed (median ~0.4s, frequent
    # 3-7s), so one-at-a-time probing measured only ~0.45 rps live; the rps
    # cap above is the real limiter once a few requests overlap.
    concurrency: int = 3
    lag_hours: float = 4.0
    start_lookback_hours: float = 24.0
    batch_size: int = 200
    claim_timeout_ms: int = 300_000
    caught_up_sleep_sec: float = 300.0
    seed_family: str = SEED_FAMILY
    # Off by default: at ~2k games/h, enqueueing 10 participants per game
    # would flood the snowball frontier far faster than producers drain it.
    enqueue_participants: bool = False
    # Consecutive unanswered probes before the LCU connection is rebuilt.  The
    # watchdog restarts LeagueClient routinely (memory leak), so this is a
    # normal event, not a failure: the sweep waits and resumes at the same ID.
    max_not_found_run: int = 300
    # A 404 run that is still all-404 after this many pauses is a permanently
    # dead block (not the frontier: new games would have filled it), so it is
    # skipped.
    dead_block_after_pauses: int = 3
    max_transport_errors: int = 5
    reconnect_sleep_sec: float = 15.0
    # Descending backfill from the forward origin, run only while caught up.
    backfill: bool = True
    # Consecutive LoL games (any queue) on an older patch that end the backfill;
    # IDs are platform-wide, so a few stragglers can straddle a patch boundary.
    backfill_older_patch_run: int = 50
    # Never walk below this ID (0 = no floor besides the patch boundary).
    backfill_floor_id: int = 0
    # Cap per caught-up pause, besides the caught_up_sleep_sec wall-clock budget.
    backfill_max_batches_per_pause: int = 20


@dataclass
class IdGapStats:
    probed: int = 0
    skipped_known: int = 0
    not_found: int = 0
    other_queue: int = 0
    saved: int = 0
    backfill_saved: int = 0
    already_claimed: int = 0
    transport_errors: int = 0
    by_queue: dict[int, int] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"probed={self.probed} known_skipped={self.skipped_known} 404={self.not_found} "
            f"other_queue={self.other_queue} saved={self.saved} backfill_saved={self.backfill_saved} claimed_elsewhere={self.already_claimed} "
            f"transport_errors={self.transport_errors}"
        )


class IdGapSweeper:
    """Walk the game-ID sequence and commit unseen target-queue games."""

    def __init__(
        self,
        storage: SnowballWriterFacade,
        probe: Callable[[int], ProbeResult],
        config: IdGapConfig = IdGapConfig(),
        *,
        reconnect: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
        worker_id: str = "G01",
    ) -> None:
        self.storage = storage
        self.probe = probe
        self.config = config
        self.reconnect = reconnect
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self.worker_id = worker_id
        self.stats = IdGapStats()
        self._last_probe_at = 0.0
        self._throttle_lock = threading.Lock()
        self._transport_streak = 0
        # A long run of 404s with no 200 behind it means the sweep has passed
        # the newest assigned ID (quiet hours can make the platform allocate
        # IDs slower than we probe).  Never advance through such a run: an ID
        # that is unassigned now will hold a real game later.
        self._not_found_run = 0
        self._not_found_run_start: int | None = None
        self._not_found_run_last: int | None = None
        self._stalled_at: int | None = None
        self._stalled_count = 0
        self._older_patch_run = 0
        self._back_patch: tuple[int, int] | None = None
        self._backfill_done = not config.backfill

    # ------------------------------------------------------------- cursor
    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    def load_cursor(self) -> int | None:
        raw = self.storage.runtime_get(CURSOR_KEY)
        if raw is not None and raw.strip().isdigit():
            return int(raw.strip())
        anchor = self.storage.latest_game_id(
            created_before_ms=self._now_ms() - int(self.config.start_lookback_hours * 3600_000)
        )
        if anchor is None:
            return None
        cursor = anchor + 1
        self.storage.runtime_set(CURSOR_KEY, str(cursor))
        self.storage.runtime_set(ORIGIN_KEY, str(cursor))
        self.log(f"[id-gap] cold start: cursor={cursor} (latest stored game {self.config.start_lookback_hours:g}h back) worker={self.worker_id}")
        return cursor

    def save_cursor(self, cursor: int) -> None:
        self.storage.runtime_set(CURSOR_KEY, str(int(cursor)))

    # -------------------------------------------------------------- probing
    def _throttle(self) -> None:
        if self.config.rps <= 0:
            return
        with self._throttle_lock:
            wait = self._last_probe_at + 1.0 / self.config.rps - self.clock()
            if wait > 0:
                self.sleep(wait)
            self._last_probe_at = self.clock()

    def _throttled_probe(self, game_id: int) -> ProbeResult:
        self._throttle()
        return self.probe(game_id)

    def sweep_batch(self, cursor: int) -> tuple[int, str]:
        """Process [cursor, cursor + batch_size).

        Returns ``(next_cursor, state)``:

        * ``progress``  -- batch done, next_cursor = first ID of the next batch;
        * ``caught_up`` -- reached the lag frontier, next_cursor = first ID not
          yet known to be old enough (re-probed after a pause);
        * ``retry`` / ``lcu_down`` -- no LCU verdict for next_cursor; it is
          retried, never skipped.
        """
        hi = cursor + max(1, int(self.config.batch_size)) - 1
        known = self.storage.known_game_ids(cursor, hi)
        lag_cutoff_ms = self._now_ms() - int(self.config.lag_hours * 3600_000)
        gaps = [game_id for game_id in range(cursor, hi + 1) if game_id not in known]
        self.stats.skipped_known += (hi + 1 - cursor) - len(gaps)
        executor = ThreadPoolExecutor(max_workers=max(1, int(self.config.concurrency)), thread_name_prefix="id-gap")
        try:
            # map() yields in submission order, so IDs are judged in sequence
            # and the cursor semantics are the same as a one-at-a-time sweep.
            return self._judge(zip(gaps, executor.map(self._throttled_probe, gaps)), lag_cutoff_ms, hi)
        finally:
            # Probes already queued past an early return are dropped; their
            # IDs are re-probed from the returned cursor.
            executor.shutdown(wait=True, cancel_futures=True)

    def _judge(self, results, lag_cutoff_ms: int, hi: int) -> tuple[int, str]:
        for game_id, result in results:
            if result.status == 404:
                self.stats.probed += 1
                self.stats.not_found += 1
                self._transport_streak = 0
                if self._not_found_run_start is None:
                    self._not_found_run_start = game_id
                self._not_found_run += 1
                self._not_found_run_last = game_id
                if self._not_found_run >= self.config.max_not_found_run:
                    return self._not_found_run_start, "caught_up"
                continue
            if result.status != 200 or result.body is None:
                # Not a verdict about this ID (client restarting, 5xx, 429).
                self.stats.transport_errors += 1
                self._transport_streak += 1
                if self._transport_streak >= self.config.max_transport_errors:
                    return game_id, "lcu_down"
                self.sleep(min(30.0, 2.0 * self._transport_streak))
                return game_id, "retry"
            self._transport_streak = 0
            self.stats.probed += 1
            detail = result.body
            if int(detail.get("gameCreation") or 0) > lag_cutoff_ms:
                return game_id, "caught_up"
            self._not_found_run, self._not_found_run_start, self._not_found_run_last = 0, None, None
            record = _parse_game_detail(detail, set(self.config.target_queues))
            if record is None:
                self.stats.other_queue += 1
                continue
            self._commit(str(game_id), record, detail)
        return hi + 1, "progress"

    def _commit(self, game_id: str, record: dict, detail: dict, *, backfill: bool = False) -> None:
        claim = self.storage.claim_game(game_id, claim_timeout_ms=self.config.claim_timeout_ms)
        if claim is None:
            # DONE (a snowball producer stored it since the batch lookup) or
            # BUSY (one is fetching it right now).
            self.stats.already_claimed += 1
            return
        record = dict(record)
        record["captured_at"] = _utc_now()
        record["seed_family"] = self.config.seed_family
        puuids = _extract_participant_puuids(detail) if self.config.enqueue_participants else []
        try:
            inserted = self.storage.commit_game(claim, record, puuids)
        except SnowballWriterClaimsStopped:
            raise
        except Exception:
            self._release_quietly(claim)
            raise
        if not inserted:
            self._release_quietly(claim)
            return
        self.stats.saved += 1
        if backfill:
            self.stats.backfill_saved += 1
        queue_id = int(record["queue_id"])
        self.stats.by_queue[queue_id] = self.stats.by_queue.get(queue_id, 0) + 1
        self.log(
            f"  [saved] q={queue_id:<5} game_id={game_id} patch={record['patch']} "
            f"total_saved={self.stats.saved}{' backfill' if backfill else ''} worker={self.worker_id}"
        )

    def _release_quietly(self, claim: Any) -> None:
        try:
            self.storage.release_game(claim)
        except Exception:
            pass

    # ------------------------------------------------------------- backfill
    @staticmethod
    def _patch_of(detail: dict) -> tuple[int, int] | None:
        parts = str(detail.get("gameVersion") or "").split(".")
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        return int(parts[0]), int(parts[1])

    def load_back_cursor(self) -> int | None:
        """Next ID the backfill probes (inclusive, descending); None = nothing to do."""
        if self._backfill_done:
            return None
        if self.storage.runtime_get(BACK_DONE_KEY):
            self._backfill_done = True
            return None
        raw = self.storage.runtime_get(BACK_CURSOR_KEY)
        if raw is not None and raw.strip().isdigit():
            cursor = int(raw.strip())
        else:
            origin = self.storage.runtime_get(ORIGIN_KEY)
            if origin is None or not origin.strip().isdigit():
                self.log(f"[id-gap] backfill disabled: no {ORIGIN_KEY} recorded worker={self.worker_id}")
                self._backfill_done = True
                return None
            cursor = int(origin.strip()) - 1
            self.storage.runtime_set(BACK_CURSOR_KEY, str(cursor))
            self.log(f"[id-gap] backfill start: back_cursor={cursor} worker={self.worker_id}")
        if self._back_patch is None:
            major, _, minor = (self.storage.runtime_get(BACK_PATCH_KEY) or "").partition(".")
            if major.isdigit() and minor.isdigit():
                self._back_patch = (int(major), int(minor))
        if cursor < self._backfill_floor():
            self._finish_backfill(cursor, "reached floor")
            return None
        return cursor

    def _backfill_floor(self) -> int:
        return max(1, int(self.config.backfill_floor_id))

    def _finish_backfill(self, cursor: int, reason: str) -> None:
        self._backfill_done = True
        self.storage.runtime_set(BACK_DONE_KEY, f"{reason} at {cursor}")
        self.log(f"[id-gap] backfill done ({reason}) at {cursor}; {self.stats.summary()} worker={self.worker_id}")

    def backfill_batch(self, cursor: int) -> tuple[int, str]:
        """Process [cursor - batch_size + 1, cursor] high to low.

        Returns ``(next_back_cursor, state)`` with state ``progress``,
        ``patch_boundary``, ``floor``, or ``retry`` / ``lcu_down``
        (next_back_cursor is then retried, never skipped).
        """
        floor = self._backfill_floor()
        lo = max(floor, cursor - max(1, int(self.config.batch_size)) + 1)
        known = self.storage.known_game_ids(lo, cursor)
        gaps = [game_id for game_id in range(cursor, lo - 1, -1) if game_id not in known]
        self.stats.skipped_known += (cursor + 1 - lo) - len(gaps)
        executor = ThreadPoolExecutor(max_workers=max(1, int(self.config.concurrency)), thread_name_prefix="id-gap-back")
        try:
            for game_id, result in zip(gaps, executor.map(self._throttled_probe, gaps)):
                if result.status == 404:
                    # Old IDs: a 404 is final (TFT, customs, unassigned), never
                    # an in-progress game.
                    self.stats.probed += 1
                    self.stats.not_found += 1
                    self._transport_streak = 0
                    continue
                if result.status != 200 or result.body is None:
                    self.stats.transport_errors += 1
                    self._transport_streak += 1
                    if self._transport_streak >= self.config.max_transport_errors:
                        return game_id, "lcu_down"
                    self.sleep(min(30.0, 2.0 * self._transport_streak))
                    return game_id, "retry"
                self._transport_streak = 0
                self.stats.probed += 1
                detail = result.body
                patch = self._patch_of(detail)
                if patch is not None:
                    if self._back_patch is None:
                        self._back_patch = patch
                        self.storage.runtime_set(BACK_PATCH_KEY, f"{patch[0]}.{patch[1]}")
                    self._older_patch_run = self._older_patch_run + 1 if patch < self._back_patch else 0
                record = _parse_game_detail(detail, set(self.config.target_queues))
                if record is None:
                    self.stats.other_queue += 1
                else:
                    # Older-patch stragglers are still unseen games: keep them.
                    self._commit(str(game_id), record, detail, backfill=True)
                if self._older_patch_run >= self.config.backfill_older_patch_run:
                    return game_id - 1, "patch_boundary"
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        return lo - 1, ("floor" if lo <= floor else "progress")

    def _backfill_during_pause(self, should_stop: Callable[[], bool]) -> None:
        """Spend a caught-up pause on the backfill, then sleep whatever is left."""
        budget = float(self.config.caught_up_sleep_sec)
        started = self.clock()
        batches = 0
        while not should_stop() and batches < max(1, int(self.config.backfill_max_batches_per_pause)):
            if self.clock() - started >= budget:
                return
            cursor = self.load_back_cursor()
            if cursor is None:
                break
            next_cursor, state = self.backfill_batch(cursor)
            batches += 1
            if next_cursor != cursor:
                self.storage.runtime_set(BACK_CURSOR_KEY, str(next_cursor))
            if state == "patch_boundary":
                self._finish_backfill(next_cursor, "crossed into the previous patch")
                break
            if state == "floor":
                self._finish_backfill(next_cursor, "reached floor")
                break
            if state == "lcu_down":
                self._transport_streak = 0
                break
            if batches % 10 == 0:
                self.log(
                    f"[id-gap] backfill back_cursor={next_cursor} {self.stats.summary()}"
                    f"{self._probe_summary()} worker={self.worker_id}"
                )
        self._sleep_unless_stopped(max(0.0, budget - (self.clock() - started)), should_stop)

    # ----------------------------------------------------------------- loop
    def _sleep_unless_stopped(self, seconds: float, should_stop: Callable[[], bool]) -> None:
        remaining = float(seconds)
        while remaining > 0 and not should_stop():
            step = min(5.0, remaining)
            self.sleep(step)
            remaining -= step

    def run(self, should_stop: Callable[[], bool] = lambda: False, *, max_batches: int | None = None) -> IdGapStats:
        batches = 0
        try:
            cursor = None
            while not should_stop():
                if cursor is None:
                    cursor = self.load_cursor()
                    if cursor is None:
                        self.log("[id-gap] no stored game old enough to anchor the sweep; waiting")
                        self._sleep_unless_stopped(self.config.caught_up_sleep_sec, should_stop)
                        continue
                next_cursor, state = self.sweep_batch(cursor)
                if state == "caught_up":
                    hit_404_run = self._not_found_run >= self.config.max_not_found_run
                    run_end = (self._not_found_run_last or next_cursor) + 1
                    self._not_found_run, self._not_found_run_start, self._not_found_run_last = 0, None, None
                    if hit_404_run:
                        self._stalled_count = self._stalled_count + 1 if self._stalled_at == next_cursor else 1
                        self._stalled_at = next_cursor
                        if self._stalled_count > self.config.dead_block_after_pauses:
                            self.log(
                                f"[id-gap] {next_cursor}..{run_end - 1} stayed all-404 across "
                                f"{self._stalled_count - 1} pauses; skipping dead block"
                            )
                            next_cursor, state = run_end, "progress"
                            self._stalled_at, self._stalled_count = None, 0
                if next_cursor != cursor:
                    self.save_cursor(next_cursor)
                cursor = next_cursor
                batches += 1
                if state == "progress" and batches % 10 == 0:
                    self.log(f"[id-gap] cursor={cursor} {self.stats.summary()}{self._probe_summary()} worker={self.worker_id}")
                if state == "caught_up":
                    self.log(f"[id-gap] caught up to the {self.config.lag_hours:g}h lag frontier at {cursor}; {self.stats.summary()}")
                    self._backfill_during_pause(should_stop)
                elif state == "lcu_down":
                    self.log(f"[id-gap] LCU not answering at {cursor}; reconnecting")
                    self._sleep_unless_stopped(self.config.reconnect_sleep_sec, should_stop)
                    if self.reconnect is not None:
                        try:
                            self.reconnect()
                        except Exception as exc:  # keep waiting; the watchdog owns the client
                            self.log(f"[id-gap] reconnect failed: {exc!r}")
                    self._transport_streak = 0
                if max_batches is not None and batches >= max_batches:
                    break
        except SnowballWriterClaimsStopped:
            self.log("[id-gap] writer stopped claims; exiting")
        self.log(f"[id-gap] done: {self.stats.summary()}{self._probe_summary()} worker={self.worker_id}")
        return self.stats

    def _probe_summary(self) -> str:
        summary = getattr(self.probe, "summary", None)
        return f" {summary()}" if callable(summary) else ""


class _LcuProbe:
    """Owns one LCU connection and rebuilds it after a client restart."""

    def __init__(self) -> None:
        self._client = None
        self.reconnect()

    def reconnect(self) -> None:
        from .client import LCUClient
        from .process import get_credentials

        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        creds = get_credentials()
        if creds is None:
            raise RuntimeError("League client not found")
        self._client = LCUClient(creds)

    def __call__(self, game_id: int) -> ProbeResult:
        if self._client is None:
            return ProbeResult(-1, None)
        status, body = self._client.get_with_status(f"/lol-match-history/v1/games/{int(game_id)}")
        return ProbeResult(status, body if isinstance(body, dict) else None)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


class _LcuTokenSource:
    """``get()`` against whatever LCU connection the probe holds right now.

    The LCU probe rebuilds its client after a League restart; the SGP client
    keeps this indirection so it always asks the live connection for a token.
    """

    def __init__(self, lcu_probe: "_LcuProbe") -> None:
        self._probe = lcu_probe

    def get(self, path: str) -> Any:
        client = self._probe._client
        return client.get(path) if client is not None else None


class _SgpProbe:
    """Probe IDs through Riot SGP, falling back to the LCU probe per ID.

    SGP ``/match-history-query/v1/products/lol/TW2_{id}/SUMMARY`` is the
    service LCU ``games/{id}`` proxies, with the same 404 for unused, non-LoL
    and in-progress IDs -- but without LeagueClient's ~0.4-7s plugin latency,
    which capped the LCU sweep below the platform's ID allocation rate.  The
    LCU connection still issues the access token.

    Any SGP failure sends that ID to LCU; ``failure_threshold`` consecutive
    failures, or any 429, send every ID to LCU for ``cooldown_sec`` (or the
    server's Retry-After, if longer).  Thread-safe: the sweeper probes
    ``concurrency`` IDs at once and paces them itself.
    """

    def __init__(
        self,
        lcu_probe: Callable[[int], ProbeResult],
        client: Any,
        *,
        failure_threshold: int = 3,
        cooldown_sec: float = 300.0,
        log: Callable[[str], None] = print,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.lcu = lcu_probe
        self.client = client
        self._failure_threshold = max(1, int(failure_threshold))
        self._cooldown_sec = max(1.0, float(cooldown_sec))
        self._log = log
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._failures = 0
        self._disabled_until = 0.0
        self.sgp_found = 0
        self.sgp_not_found = 0
        self.lcu_fallbacks = 0
        self.rate_limited = 0

    def reconnect(self) -> None:
        reconnect = getattr(self.lcu, "reconnect", None)
        if reconnect is not None:
            reconnect()

    def _trip(self, seconds: float, reason: str) -> None:
        self._disabled_until = self._monotonic() + seconds
        self._failures = 0
        self._log(f"[id-gap] sgp circuit open for {seconds:.0f}s; probing via LCU  reason={reason}")

    def _fallback(self, game_id: int) -> ProbeResult:
        with self._lock:
            self.lcu_fallbacks += 1
        return self.lcu(game_id)

    def __call__(self, game_id: int) -> ProbeResult:
        with self._lock:
            circuit_open = self._monotonic() < self._disabled_until
            if not circuit_open and self._disabled_until:
                self._disabled_until = 0.0
                self._log("[id-gap] sgp circuit closed; probing via SGP")
        if circuit_open:
            return self._fallback(game_id)
        try:
            block = self.client.game(game_id)
        except SGPRateLimited as exc:
            with self._lock:
                self.rate_limited += 1
                if self._monotonic() >= self._disabled_until:
                    self._trip(max(self._cooldown_sec, exc.retry_after_sec), "rate_limited")
            return self._fallback(game_id)
        except SGPError as exc:
            with self._lock:
                self._failures += 1
                if self._failures >= self._failure_threshold and self._monotonic() >= self._disabled_until:
                    self._trip(self._cooldown_sec, str(exc))
            return self._fallback(game_id)
        with self._lock:
            self._failures = 0
            if block is None:
                self.sgp_not_found += 1
            else:
                self.sgp_found += 1
        if block is None:
            return ProbeResult(404, None)
        return ProbeResult(200, sgp_game_to_lcu_detail(block))

    def summary(self) -> str:
        return (
            f"sgp_200={self.sgp_found} sgp_404={self.sgp_not_found} "
            f"lcu_fallback={self.lcu_fallbacks} sgp_429={self.rate_limited}"
        )

    def close(self) -> None:
        try:
            self.client.close()
        finally:
            close = getattr(self.lcu, "close", None)
            if close is not None:
                close()


def run_id_gap(
    *,
    writer_client: Any,
    worker_id: str = "G01",
    target_queues: set[int] | frozenset[int] = DEFAULT_QUEUES,
    rps: float = 2.0,
    concurrency: int = 3,
    lag_hours: float = 4.0,
    start_lookback_hours: float = 24.0,
    claim_timeout_sec: int = 300,
    source: str = "lcu",
) -> IdGapStats:
    """Fleet entry point: run one sweeper until the writer stops claims.

    ``source="sgp"`` probes through Riot SGP and keeps LCU as the per-ID and
    circuit-breaker fallback (see ``_SgpProbe``).
    """
    if source not in {"lcu", "sgp"}:
        raise ValueError(f"unknown id-gap source: {source!r}")
    # Request IDs are journaled in writer_requests; a prefix that repeats across
    # restarts replays "id-gap-G01-0" with a different payload and the writer
    # rejects it (REQUEST_ID_CONFLICT). Keep the worker id for readability but
    # make every process unique.
    storage = SnowballWriterFacade(writer_client, request_prefix=f"id-gap-{worker_id}-{uuid.uuid4().hex[:12]}")
    config = IdGapConfig(
        target_queues=frozenset(int(q) for q in target_queues),
        rps=float(rps),
        concurrency=max(1, int(concurrency)),
        lag_hours=float(lag_hours),
        start_lookback_hours=float(start_lookback_hours),
        claim_timeout_ms=max(1, int(claim_timeout_sec)) * 1000,
    )
    lcu_probe = None
    while lcu_probe is None:
        try:
            lcu_probe = _LcuProbe()
        except RuntimeError:
            print("[id-gap] League client not found; retrying in 30s", flush=True)
            time.sleep(30.0)

    def log(line: str) -> None:
        print(line, flush=True)

    probe: Any = lcu_probe
    if source == "sgp":
        # The sweeper's rps throttle paces requests; the client adds none.
        probe = _SgpProbe(lcu_probe, SGPMatchHistoryClient(_LcuTokenSource(lcu_probe), min_interval_sec=0.0), log=log)
    try:
        sweeper = IdGapSweeper(storage, probe, config, reconnect=probe.reconnect, worker_id=worker_id, log=log)
        print(
            f"[id-gap] start worker={worker_id} queues={sorted(config.target_queues)} rps={config.rps:g} concurrency={config.concurrency} "
            f"lag={config.lag_hours:g}h source={source}",
            flush=True,
        )
        return sweeper.run()
    finally:
        probe.close()
