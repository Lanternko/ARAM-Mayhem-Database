"""Frozen per-patch aggregate snapshots ("patch 結算") for the tier-list build.

A closed patch's games stop changing -- almost.  The LCU only exposes each
player's ~20 most recent games, so once a new patch ships the old one keeps
receiving a decaying tail of stragglers (measured 2026-08-04: of the last 30k
Mayhem rows inserted, 96.9% were 16.15, 2.1% were still 16.14, 0.5% 16.13).
Rescanning those hundreds of thousands of games on every build to re-derive
numbers that barely move is the single largest cost in the site rebuild.

So: settle a patch the first time a build sees it as a *non-current* patch
(i.e. on flip day), and re-settle only when its game count has grown by
``RESETTLE_GROWTH_RATIO`` since -- which is exactly the decay tail's shape.
16.14 grew +2.7% after 16.15 opened (stays frozen); 16.13 grew +10.2% after
16.14 opened (settles a second time, then stops).

What is stored is deliberately the *counters*, never the derived win rates:
smoothing constants, tier cuts and lift formulas all still change, and a
snapshot of raw (games, wins) tallies survives those changes.  Only a change in
what the counters MEAN needs an invalidation, which is what ``SCHEMA_VERSION``
and the per-section ``fingerprint`` are for.

Layout -- one file per (queue, patch), several independently-validated sections:

    data/patch_snapshots/q2400-16.14.json
      {"queue_id": 2400, "patch": "16.14",
       "sections": {"<name>": {"schema_version": 1, "games_at_settle": 431245,
                               "settled_at": "...", "fingerprint": "...",
                               "payload": {...}}}}

Callers own the payload's encoding (see tierlist_engine's counter codecs); this
module only owns *when* a snapshot may be trusted.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

# Bump when a section's payload semantics change in a way that makes an existing
# snapshot wrong (a new counter, a changed key, a different scan predicate).
SCHEMA_VERSION = 1

# Re-settle a frozen patch once its game count has grown this much since the
# snapshot was taken.  See the module docstring for the measured decay tail.
RESETTLE_GROWTH_RATIO = 0.10

DEFAULT_SNAPSHOT_DIR = Path("data/patch_snapshots")


@dataclass(frozen=True)
class SectionStatus:
    """Why a section was (not) reused -- surfaced by the build log and the CLI."""

    state: str  # "fresh" | "missing" | "stale-schema" | "stale-fingerprint" | "stale-growth"
    games_at_settle: int = 0
    live_games: int = 0
    settled_at: str = ""

    @property
    def usable(self) -> bool:
        return self.state == "fresh"

    def describe(self) -> str:
        if self.state == "fresh":
            return (
                f"reused snapshot ({self.games_at_settle:,} games settled "
                f"{self.settled_at[:10]}; live {self.live_games:,})"
            )
        if self.state == "stale-growth":
            grew = self.live_games - self.games_at_settle
            pct = (grew / self.games_at_settle * 100.0) if self.games_at_settle else 0.0
            return (
                f"re-settling: grew {grew:+,} games ({pct:+.1f}%) since "
                f"{self.games_at_settle:,}, over the {RESETTLE_GROWTH_RATIO:.0%} bar"
            )
        return {
            "missing": "no snapshot yet; settling",
            "stale-schema": "snapshot schema outdated; re-settling",
            "stale-fingerprint": "snapshot inputs changed; re-settling",
        }.get(self.state, self.state)


def snapshot_path(patch: str, *, queue_id: int, snapshot_dir: Path | None = None) -> Path:
    directory = Path(snapshot_dir) if snapshot_dir is not None else DEFAULT_SNAPSHOT_DIR
    return directory / f"q{queue_id}-{patch}.json"


def _read_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A truncated / hand-mangled snapshot must degrade to a rescan, never
        # take the build down.
        return {}
    return data if isinstance(data, dict) else {}


def read_section(
    patch: str,
    *,
    queue_id: int,
    section: str,
    snapshot_dir: Path | None = None,
) -> dict | None:
    """Return the stored section record (meta + payload), without validating it."""
    sections = _read_file(snapshot_path(patch, queue_id=queue_id, snapshot_dir=snapshot_dir))
    record = (sections.get("sections") or {}).get(section)
    return record if isinstance(record, dict) else None


def section_status(
    patch: str,
    *,
    queue_id: int,
    section: str,
    live_games: int,
    fingerprint: str | None = None,
    snapshot_dir: Path | None = None,
) -> SectionStatus:
    """Decide whether the stored section may be reused for *live_games*."""
    record = read_section(patch, queue_id=queue_id, section=section, snapshot_dir=snapshot_dir)
    if not record or "payload" not in record:
        return SectionStatus("missing", live_games=live_games)
    settled = int(record.get("games_at_settle", 0) or 0)
    common = {
        "games_at_settle": settled,
        "live_games": live_games,
        "settled_at": str(record.get("settled_at", "")),
    }
    if int(record.get("schema_version", 0) or 0) != SCHEMA_VERSION:
        return SectionStatus("stale-schema", **common)
    if (record.get("fingerprint") or None) != (fingerprint or None):
        return SectionStatus("stale-fingerprint", **common)
    # A settled patch that somehow lost rows (a merge-db rollback, a rebuilt DB)
    # is also untrustworthy: the snapshot describes a superset of what is there.
    if settled <= 0 or live_games < settled:
        return SectionStatus("stale-growth", **common)
    if live_games >= settled * (1.0 + RESETTLE_GROWTH_RATIO):
        return SectionStatus("stale-growth", **common)
    return SectionStatus("fresh", **common)


def load_section(
    patch: str,
    *,
    queue_id: int,
    section: str,
    live_games: int,
    fingerprint: str | None = None,
    snapshot_dir: Path | None = None,
) -> tuple[dict | None, SectionStatus]:
    """Return (payload, status); payload is None unless the snapshot is fresh."""
    status = section_status(
        patch,
        queue_id=queue_id,
        section=section,
        live_games=live_games,
        fingerprint=fingerprint,
        snapshot_dir=snapshot_dir,
    )
    if not status.usable:
        return None, status
    record = read_section(patch, queue_id=queue_id, section=section, snapshot_dir=snapshot_dir)
    payload = (record or {}).get("payload")
    if not isinstance(payload, dict):
        return None, SectionStatus("missing", live_games=live_games)
    return payload, status


def save_section(
    patch: str,
    *,
    queue_id: int,
    section: str,
    payload: dict,
    live_games: int,
    fingerprint: str | None = None,
    snapshot_dir: Path | None = None,
) -> Path:
    """Freeze *payload* for (queue, patch, section) at the current game count."""
    path = snapshot_path(patch, queue_id=queue_id, snapshot_dir=snapshot_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_file(path)
    data.setdefault("queue_id", queue_id)
    data.setdefault("patch", patch)
    sections = data.setdefault("sections", {})
    if not isinstance(sections, dict):
        sections = {}
        data["sections"] = sections
    sections[section] = {
        "schema_version": SCHEMA_VERSION,
        "games_at_settle": int(live_games),
        "settled_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "fingerprint": fingerprint,
        "payload": payload,
    }
    # Write-then-replace: a build interrupted mid-write must not leave a
    # half-parsed snapshot that the next build silently treats as missing.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return path


def list_snapshots(*, queue_id: int | None = None, snapshot_dir: Path | None = None) -> list[dict]:
    """Summarise every stored snapshot -- what the settle CLI prints."""
    directory = Path(snapshot_dir) if snapshot_dir is not None else DEFAULT_SNAPSHOT_DIR
    if not directory.exists():
        return []
    rows: list[dict] = []
    for path in sorted(directory.glob("q*-*.json")):
        data = _read_file(path)
        stored_queue = int(data.get("queue_id", 0) or 0)
        if queue_id is not None and stored_queue != queue_id:
            continue
        for name, record in (data.get("sections") or {}).items():
            if not isinstance(record, dict):
                continue
            rows.append({
                "path": path,
                "queue_id": stored_queue,
                "patch": str(data.get("patch", "")),
                "section": name,
                "schema_version": int(record.get("schema_version", 0) or 0),
                "games_at_settle": int(record.get("games_at_settle", 0) or 0),
                "settled_at": str(record.get("settled_at", "")),
            })
    return rows
