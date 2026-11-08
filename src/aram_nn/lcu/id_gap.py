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
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, NamedTuple

from .poller import _parse_game_detail
from .snowball import (
    SnowballWriterClaimsStopped,
    SnowballWriterFacade,
    _extract_participant_puuids,
    _utc_now,
)

CURSOR_KEY = "id_gap:cursor"
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


@dataclass
class IdGapStats:
    probed: int = 0
    skipped_known: int = 0
    not_found: int = 0
    other_queue: int = 0
    saved: int = 0
    already_claimed: int = 0
    transport_errors: int = 0
    by_queue: dict[int, int] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"probed={self.probed} known_skipped={self.skipped_known} 404={self.not_found} "
            f"other_queue={self.other_queue} saved={self.saved} claimed_elsewhere={self.already_claimed} "
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

    def _commit(self, game_id: str, record: dict, detail: dict) -> None:
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
        queue_id = int(record["queue_id"])
        self.stats.by_queue[queue_id] = self.stats.by_queue.get(queue_id, 0) + 1
        self.log(
            f"  [saved] q={queue_id:<5} game_id={game_id} patch={record['patch']} "
            f"total_saved={self.stats.saved} worker={self.worker_id}"
        )

    def _release_quietly(self, claim: Any) -> None:
        try:
            self.storage.release_game(claim)
        except Exception:
            pass

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
                    self.log(f"[id-gap] cursor={cursor} {self.stats.summary()} worker={self.worker_id}")
                if state == "caught_up":
                    self.log(f"[id-gap] caught up to the {self.config.lag_hours:g}h lag frontier at {cursor}; {self.stats.summary()}")
                    self._sleep_unless_stopped(self.config.caught_up_sleep_sec, should_stop)
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
        self.log(f"[id-gap] done: {self.stats.summary()} worker={self.worker_id}")
        return self.stats


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
) -> IdGapStats:
    """Fleet entry point: run one sweeper until the writer stops claims."""
    storage = SnowballWriterFacade(writer_client, request_prefix=f"id-gap-{worker_id}")
    config = IdGapConfig(
        target_queues=frozenset(int(q) for q in target_queues),
        rps=float(rps),
        concurrency=max(1, int(concurrency)),
        lag_hours=float(lag_hours),
        start_lookback_hours=float(start_lookback_hours),
        claim_timeout_ms=max(1, int(claim_timeout_sec)) * 1000,
    )
    probe = None
    while probe is None:
        try:
            probe = _LcuProbe()
        except RuntimeError:
            print("[id-gap] League client not found; retrying in 30s", flush=True)
            time.sleep(30.0)
    try:
        sweeper = IdGapSweeper(storage, probe, config, reconnect=probe.reconnect, worker_id=worker_id,
                               log=lambda line: print(line, flush=True))
        print(
            f"[id-gap] start worker={worker_id} queues={sorted(config.target_queues)} rps={config.rps:g} concurrency={config.concurrency} "
            f"lag={config.lag_hours:g}h",
            flush=True,
        )
        return sweeper.run()
    finally:
        probe.close()
