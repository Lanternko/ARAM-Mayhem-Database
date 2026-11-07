"""Tk GUI for the ARAM champ-select recommender.

A standalone always-on-top window that shows bench-swap suggestions with
live updates as the LCU champ-select state changes.  Architecturally the
same as `lcu_collector.py recommend` but renders into a Tk window instead
of clearing the terminal.

Threading:
  - main thread: Tk event loop, owns all widgets.
  - poll thread: runs the LCU polling loop, never touches Tk; pushes
    updates onto a queue.Queue that the main thread drains via root.after.

Tkinter is not thread-safe - keep this separation strict.

Usage:
  python scripts/recommend_gui.py
"""
from __future__ import annotations

import json
import math
import os
import queue
import ssl
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import click

from aram_nn.augment_bridge import start_bridge_thread
from aram_nn.augment_ocr import start_ocr_watcher
from aram_nn.augment_overlay import AugmentOverlay, badge_layout
from aram_nn.icons import IconCache
from aram_nn.lcu.client import (
    LCUClient, get_champion_summary, get_champ_select_session, get_current_summoner,
    get_gameflow_phase, get_gameflow_session,
)
from aram_nn.lcu.process import get_credentials
from aram_nn.recommend import (
    ParsedSession, best_available_team_combos, describe_team_combo, load_composition_lr, load_lr,
    load_synergy, parse_session, session_state_hash, suggest_for_cell,
)
from aram_nn.pair_synergy import PairSynergyStats
from aram_nn.quick_picks import QuickPickSource
from aram_nn.role_synergy import RoleSynergyStats


APP_NAME = "ARAMRecommender"
# Explicit AppUserModelID.  The recommender is launched from source via a venv
# pythonw copy (ARAMRecommender.exe); without an explicit id the live window's
# taskbar identity is the interpreter's, which differs from the pinned shortcut
# -> Windows shows TWO taskbar buttons.  Tagging both the process (here) and the
# pinned .lnk with this same id makes the running window merge into the pin.
APP_USER_MODEL_ID = "Lanternko.ARAMRecommender"
# Pooled cross-patch + recency-weighted models (half-life 7d): pool 16.10-16.12
# and weight recent games up so the current patch leads.  Verified +1.6pp acc
# over the 16.10-pinned model on held-out 16.12 (scripts/train_composition_lr_pooled.py).
DEFAULT_LR_MODEL = Path("models/composition_lr_pooled_recency_7d/lr_weights.json")
DEFAULT_VOCAB = Path("models/composition_lr_pooled_recency_7d/champ_to_idx.json")
# Champion x teammate-ROLE synergy (replaces raw champ-pair synergy, which was
# winner's-curse noise: train->test r~0.17 vs r~0.37 for role-pooled — see
# scripts/ablation_champ_role_persistence.py).  Lives in the model dir so it is
# refreshed together with the pooled models.  load_synergy below still accepts a
# legacy pair_synergy_*.json if one is passed explicitly.
DEFAULT_SYNERGY_STATS = Path("models/composition_lr_pooled_recency_7d/role_synergy.json")
DEFAULT_COMPOSITION_MODEL = Path("models/composition_lr_pooled_recency_7d/model.pkl")
DEFAULT_CHAMPION_NAMES = Path("data/cache/champion_abilities.json")
DEFAULT_TIER_PAYLOAD = Path("docs/api/tier-list.json")
DEFAULT_APP_ICON = Path("docs/recommender-app-icon.ico")
DESKTOP_DATA_DIR: Path | None = None
DESKTOP_STATUS = ""
_DESKTOP_PATHS = {
    str(DEFAULT_LR_MODEL): "lr_weights.json", str(DEFAULT_VOCAB): "champ_to_idx.json",
    str(DEFAULT_SYNERGY_STATS): "role_synergy.json", str(DEFAULT_COMPOSITION_MODEL): "composition.json",
    str(DEFAULT_CHAMPION_NAMES): "champion_names.json", str(DEFAULT_TIER_PAYLOAD): "tier-list.json",
}
LIVE_CLIENT_ALL_GAME_DATA_URL = "https://127.0.0.1:2999/liveclientdata/allgamedata"
OVERWOLF_AUGMENT_EVENT = Path("data/overwolf/latest_augments.json")
OVERWOLF_AUGMENT_LOG = Path("data/overwolf/augment_events.jsonl")
# An offer stays on screen until the player picks (the probe then posts
# `picked`, which hides the panel) or this long passes.  The Mayhem selection
# screen does not force a pick, so allow a generous window.
AUGMENT_OFFER_TTL_SEC = 300


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _resource_path(relative: Path | str) -> Path:
    rel = Path(relative)
    if getattr(sys, "frozen", False):
        root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        if str(rel) in _DESKTOP_PATHS:
            return (DESKTOP_DATA_DIR or root / "desktop-data") / _DESKTOP_PATHS[str(rel)]
        return root / rel

    cwd_candidate = Path.cwd() / rel
    if cwd_candidate.exists():
        return cwd_candidate
    return _project_root() / rel


def _resolve_resource(path: Path | None, default_relative: Path) -> Path:
    if path is None:
        return _resource_path(default_relative)

    candidate = Path(path)
    if candidate.exists() or candidate.is_absolute():
        return candidate

    bundled_candidate = _resource_path(candidate)
    return bundled_candidate if bundled_candidate.exists() else candidate


def _icon_cache_dir() -> Path:
    if getattr(sys, "frozen", False):
        local_appdata = os.environ.get("LOCALAPPDATA")
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / APP_NAME / "icons"
    return _resource_path("data/icons")


def _quick_pick_source(payload: dict, payload_path: Path) -> QuickPickSource | None:
    """In-game quick picks read the site's generated champion shards.

    From source those sit next to the tier payload under docs/; the frozen app
    ships only the tier payload, so it fetches shards from the public site.
    """
    if not payload:
        return None
    frozen = getattr(sys, "frozen", False)
    return QuickPickSource(
        payload,
        _icon_cache_dir().parent / "quick-picks",
        local_root=None if frozen else payload_path.parent.parent,
    )


def _log_gui_exception(context: str) -> None:
    """Best-effort: append the current exception traceback to a log file.

    The recommender runs under pythonw (no console), so an unhandled exception
    inside a Tk callback otherwise vanishes to a discarded stderr.  Worse, if it
    escapes the `_drain` loop it kills the reschedule and the overlay silently
    freezes on its last good frame.  Capturing the traceback here makes such a
    freeze diagnosable after the fact; failures to log are swallowed so logging
    can never itself break the GUI.
    """
    try:
        import traceback

        stamp = datetime.now(timezone.utc).isoformat()
        log_path = _icon_cache_dir().parent / "recommender_errors.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n----- {stamp}  {context} -----\n")
            traceback.print_exc(file=fh)
    except Exception:
        pass


def _load_settings() -> dict:
    from aram_nn.desktop.update import cache_root, read_json

    try:
        return read_json(cache_root() / "settings.json")
    except Exception:
        return {}


def _save_settings(**changes) -> None:
    """Best-effort: a failed write only loses the preference for next launch."""
    from aram_nn.desktop.update import atomic_json, cache_root

    try:
        atomic_json(cache_root() / "settings.json", {**_load_settings(), **changes})
    except Exception:
        _log_gui_exception("save settings")


def _set_window_icon(root: tk.Tk) -> None:
    icon_path = _resource_path(DEFAULT_APP_ICON)
    if not icon_path.exists():
        return
    try:
        root.iconbitmap(default=str(icon_path))
    except Exception:
        pass


# ---------- Polling thread ----------

def _enable_windows_dpi_awareness() -> None:
    """Avoid Windows bitmap-scaling Tk, which makes text and icons blurry."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        # Per-monitor aware when available; fall back for older Windows.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _set_app_user_model_id() -> None:
    """Tag this process with an explicit AppUserModelID (Windows only).

    Makes the live window merge with the pinned taskbar shortcut that carries
    the same id, instead of appearing as a separate second taskbar button.
    No-op off Windows or if the shell call is unavailable.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def load_fallback_champion_names() -> dict[int, str]:
    """Offline championId -> English alias map for --fake or LCU static misses."""
    path = _resource_path(DEFAULT_CHAMPION_NAMES)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    out: dict[int, str] = {}
    for row in data.get("champions", []):
        try:
            cid = int(row.get("champion_id") or 0)
        except (TypeError, ValueError):
            continue
        name = row.get("alias") or row.get("name_en")
        if cid > 0 and isinstance(name, str) and name:
            out[cid] = name
    return out


def load_champion_alias_to_id(tier_payload: Path | None = None) -> dict[str, int]:
    """Offline Data Dragon alias -> championId map for Live Client Data.

    The abilities cache under data/cache can be cleared; the site payload the
    augment panel already needs carries the same ddragon aliases, so it backs
    it up.  Without either, a 1-player game (practice/custom) could not name
    the champion and every augment showed "no data".
    """
    out: dict[str, int] = {}
    try:
        payload = json.loads(_resolve_resource(tier_payload, DEFAULT_TIER_PAYLOAD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    for cid, champ in (payload.get("champs") or {}).items():
        try:
            champion_id = int(cid)
        except (TypeError, ValueError):
            continue
        for alias in (champ.get("alias"), champ.get("name_en")):
            if isinstance(alias, str) and alias and champion_id > 0:
                out.setdefault(alias.lower(), champion_id)
    for cid, alias in load_fallback_champion_names().items():
        out[alias.lower()] = cid
    return out


def _norm_key(value: object) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("_", "").replace("-", "")


class AugmentAdvisor:
    """Small in-GUI augment scorer backed by the generated site payload."""

    def __init__(self, payload_path: Path | None = None) -> None:
        self.payload_path = _resolve_resource(payload_path, DEFAULT_TIER_PAYLOAD)
        self.payload: dict = {}
        self.augments: dict[int, dict] = {}
        self.champs: dict[int, dict] = {}
        self.name_to_id: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if not self.payload_path.exists():
            return
        try:
            payload = json.loads(self.payload_path.read_text(encoding="utf-8"))
        except Exception:
            return
        self.payload = payload

        raw_augs = payload.get("augs") or payload.get("augments") or {}
        raw_champs = payload.get("champs") or payload.get("champions") or {}
        self.augments = {int(k): v for k, v in raw_augs.items() if str(k).isdigit()}
        self.champs = {int(k): v for k, v in raw_champs.items() if str(k).isdigit()}
        # tier-list.json only carries each champion's top/bottom few augments;
        # the full per-champion table lives next to it (augmentStatsUrl).
        self.champ_stats: dict[int, dict] = {}
        stats_path = self.docs_dir / str(payload.get("augmentStatsUrl") or "api/champions/augment-stats.json")
        try:
            raw_stats = json.loads(stats_path.read_text(encoding="utf-8"))
            self.champ_stats = {int(k): v for k, v in raw_stats.items() if str(k).isdigit()}
        except (OSError, ValueError):
            pass

        for aid, meta in self.augments.items():
            keys = {
                aid,
                meta.get("name"),
                meta.get("name_zh"),
                meta.get("name_en"),
                meta.get("set"),
                meta.get("set_zh"),
                meta.get("set_en"),
                meta.get("setSlug"),
            }
            for key in keys:
                norm = _norm_key(key)
                if norm:
                    self.name_to_id[norm] = aid

    def resolve_id(self, augment: dict) -> int | None:
        raw = augment.get("raw")
        candidates: list[object] = [
            augment.get("id"),
            augment.get("augment_id"),
            augment.get("augmentId"),
            augment.get("name"),
        ]
        if isinstance(raw, dict):
            candidates.extend([
                raw.get("id"),
                raw.get("augment_id"),
                raw.get("augmentId"),
                raw.get("augment"),
                raw.get("name"),
                raw.get("displayName"),
            ])
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                aid = int(candidate)
                if aid > 0:
                    return aid
            except (TypeError, ValueError):
                pass
            norm = _norm_key(candidate)
            if norm in self.name_to_id:
                return self.name_to_id[norm]
        return None

    @property
    def docs_dir(self) -> Path:
        # docs/api/tier-list.json -> docs/
        return self.payload_path.parent.parent

    def icon_path(self, augment_id: int | None) -> Path | None:
        icon = (self.augments.get(augment_id or -1) or {}).get("icon")
        if not icon or str(icon).startswith("http"):
            return None
        path = self.docs_dir / str(icon)
        return path if path.exists() else None

    def _champ_rows(self, champion_id: int) -> dict[int, dict]:
        champ = self.champs.get(int(champion_id)) or {}
        rows: dict[int, dict] = {}
        for group in (self.champ_stats.get(int(champion_id)) or {}).values():
            for row in group or []:
                try:
                    rows[int(row.get("id"))] = row
                except (TypeError, ValueError):
                    continue
        for side in ("top", "bot"):
            by_rarity = champ.get(side) or {}
            if isinstance(by_rarity, dict):
                groups = by_rarity.values()
            else:
                groups = [by_rarity]
            for group in groups:
                for row in group or []:
                    try:
                        rows.setdefault(int(row.get("id") or row.get("augment_id")), row)
                    except (TypeError, ValueError):
                        continue
        return rows

    def _champ_ranks(self, champion_id: int) -> dict[int, int]:
        """Win-rate rank (1 = best) of each augment among this champion's same-rarity augments."""
        ranks: dict[int, int] = {}
        for group in (self.champ_stats.get(int(champion_id)) or {}).values():
            rated = [row for row in group or [] if row.get("wr") is not None and row.get("id") is not None]
            rated.sort(key=lambda row: -float(row["wr"]))
            for rank, row in enumerate(rated, start=1):
                try:
                    ranks[int(row["id"])] = rank
                except (TypeError, ValueError):
                    continue
        return ranks

    def recommend(self, champion_id: int | None, offered: list[dict]) -> list[dict]:
        if not offered:
            return []
        champ_rows = self._champ_rows(int(champion_id or 0)) if champion_id else {}
        champ_ranks = self._champ_ranks(int(champion_id or 0)) if champion_id else {}
        out: list[dict] = []
        for index, augment in enumerate(offered, start=1):
            aid = self.resolve_id(augment)
            meta = self.augments.get(aid or -1, {})
            row = champ_rows.get(aid or -1)
            name = (
                meta.get("name_zh")
                or meta.get("name")
                or meta.get("name_en")
                or augment.get("name")
                or f"option {index}"
            )
            out.append({
                "slot": augment.get("slot") or f"option {index}",
                "id": aid,
                "name": name,
                "raw_name": augment.get("name"),
                "wr": (row or {}).get("wr"),
                "lift": (row or {}).get("lift"),
                "score": (row or {}).get("score"),
                "games": int((row or {}).get("g") or (row or {}).get("games") or 0),
                "pick": (row or {}).get("pick"),
                "rank": champ_ranks.get(aid or -1),
                "rarity": meta.get("rarity"),
                "order": index,
                "screen": augment.get("screen"),
            })
        out.sort(key=lambda row: (
            row["score"] is None,
            -(float(row["score"]) if row["score"] is not None else -999.0),
            -(float(row["wr"]) if row["wr"] is not None else -999.0),
        ))
        return out


def _augment_event_path() -> Path:
    candidates: list[Path] = []
    env_latest = os.environ.get("ARAM_OVERWOLF_LATEST")
    env_dir = os.environ.get("ARAM_OVERWOLF_DIR")
    if env_latest:
        candidates.append(Path(env_latest))
    if env_dir:
        candidates.append(Path(env_dir) / "latest_augments.json")

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend([
            exe_dir.parent / OVERWOLF_AUGMENT_EVENT,
            exe_dir / OVERWOLF_AUGMENT_EVENT,
        ])
    candidates.extend([
        _project_root() / OVERWOLF_AUGMENT_EVENT,
        Path.cwd() / OVERWOLF_AUGMENT_EVENT,
    ])
    seen: set[Path] = set()
    for path in candidates:
        try:
            path = path.resolve()
        except OSError:
            pass
        if path in seen:
            continue
        seen.add(path)
        if path.exists():
            return path
    return candidates[0]


def _augment_log_path() -> Path:
    latest = _augment_event_path()
    candidate = latest.parent / "augment_events.jsonl"
    if candidate.exists():
        return candidate
    return _project_root() / OVERWOLF_AUGMENT_LOG


def _is_fragment_offer(event: dict) -> bool:
    if str(event.get("type") or "") != "offer":
        return False
    augments = [aug for aug in event.get("augments") or [] if isinstance(aug, dict)]
    if not augments:
        return False
    names = [str(aug.get("name") or "") for aug in augments]
    return all(("碎片" in name or "fragment" in name.lower() or "shard" in name.lower()) for name in names)


def _parse_event_time(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _event_from_record(record: dict, path: Path, stat_ns: int | None, *, recovered: bool = False) -> dict | None:
    payload = record.get("payload") or {}
    event = payload.get("event") or {}
    if not isinstance(event, dict):
        return None
    return {
        "path": str(path),
        "mtime_ns": stat_ns,
        "received_at": record.get("received_at"),
        "type": event.get("type") or "unknown",
        "augments": event.get("augments") or [],
        "source": event.get("source") or payload.get("app") or "",
        "recovered": recovered,
    }


def _latest_non_fragment_offer(before: datetime | None, max_age_sec: int = 180) -> dict | None:
    path = _augment_log_path()
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for line in reversed(lines[-200:]):
        try:
            record = json.loads(line)
        except Exception:
            continue
        event = _event_from_record(record, path, None, recovered=True)
        if not event or event.get("type") != "offer" or _is_fragment_offer(event):
            continue
        event_time = _parse_event_time(event.get("received_at"))
        if before and event_time:
            age = (before - event_time).total_seconds()
            if age < 0:
                continue
            if age > max_age_sec:
                return None
        return event
    return None


def read_latest_augment_event() -> dict | None:
    path = _augment_event_path()
    if not path.exists():
        return None
    try:
        stat = path.stat()
    except OSError:
        stat = None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "path": str(path),
            "mtime_ns": stat.st_mtime_ns if stat else None,
            "received_at": None,
            "type": "error",
            "augments": [],
            "source": "bad_json",
        }
    event = _event_from_record(record, path, stat.st_mtime_ns if stat else None)
    if event and _is_fragment_offer(event):
        recovered = _latest_non_fragment_offer(_parse_event_time(event.get("received_at")))
        if recovered:
            return recovered
    return event


def is_live_offer(event: dict | None, now: datetime | None = None,
                  ttl_sec: int = AUGMENT_OFFER_TTL_SEC) -> bool:
    """True while an offer is on screen: newest event is an offer, and recent.

    A `picked` event is written to the same latest file, so picking an augment
    hides the panel; a leftover offer from an earlier game ages out.
    """
    if not event or event.get("type") != "offer":
        return False
    if not any(isinstance(aug, dict) for aug in event.get("augments") or []):
        return False
    received = _parse_event_time(event.get("received_at"))
    if received is None:
        return False
    age = ((now or datetime.now(timezone.utc)) - received).total_seconds()
    return -30 <= age <= ttl_sec


def _live_raw_champion_alias(raw_name: object) -> str:
    raw = str(raw_name or "")
    for prefix in (
        "game_character_displayname_",
        "game_character_skin_displayname_",
    ):
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return raw


def get_live_game_snapshot(alias_to_id: dict[str, int], timeout_sec: float = 1.0) -> dict | None:
    """Read in-game 10-player composition from Live Client Data.

    Champ-select data disappears once the match starts.  During InProgress,
    Riot exposes the visible in-game roster through HTTPS port 2999 instead.
    """
    try:
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(
            LIVE_CLIENT_ALL_GAME_DATA_URL,
            timeout=timeout_sec,
            context=context,
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    players = data.get("allPlayers") or []
    active = data.get("activePlayer") or {}
    active_riot_id = active.get("riotId") or active.get("summonerName")
    active_team = None
    for player in players:
        if active_riot_id and (
            player.get("riotId") == active_riot_id
            or player.get("summonerName") == active_riot_id
        ):
            active_team = player.get("team")
            break
    if not active_team:
        return None

    names: dict[int, str] = {}
    teams: dict[str, list[int]] = {"ORDER": [], "CHAOS": []}
    active_champion_id: int | None = None
    for player in players:
        team = player.get("team")
        if team not in teams:
            continue
        alias = _live_raw_champion_alias(player.get("rawChampionName"))
        champion_id = alias_to_id.get(alias.lower())
        if not champion_id:
            continue
        teams[team].append(champion_id)
        display_name = player.get("championName") or alias
        if isinstance(display_name, str) and display_name:
            names[champion_id] = display_name
        if active_riot_id and (
            player.get("riotId") == active_riot_id
            or player.get("summonerName") == active_riot_id
        ):
            active_champion_id = champion_id

    enemy_team = "CHAOS" if active_team == "ORDER" else "ORDER"
    my_team_ids = teams.get(str(active_team), [])
    enemy_team_ids = teams.get(enemy_team, [])
    if len(my_team_ids) != 5 or len(enemy_team_ids) != 5:
        return None

    return {
        "my_team_ids": my_team_ids,
        "enemy_team_ids": enemy_team_ids,
        "active_champion_id": active_champion_id or my_team_ids[0],
        "names": names,
        "game_time": ((data.get("gameData") or {}).get("gameTime")),
    }


def get_live_active_champion(alias_to_id: dict[str, int], timeout_sec: float = 1.0) -> int | None:
    """The local player's champion from Live Client Data, even without a full 5v5.

    The augment panel only needs this one id; custom/practice games and the
    first seconds of a match have no complete roster, which used to leave the
    panel saying 「此英雄無資料」.
    """
    try:
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(LIVE_CLIENT_ALL_GAME_DATA_URL, timeout=timeout_sec, context=context) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    active = data.get("activePlayer") or {}
    active_riot_id = active.get("riotId") or active.get("summonerName")
    if not active_riot_id:
        return None
    for player in data.get("allPlayers") or []:
        if active_riot_id in (player.get("riotId"), player.get("summonerName")):
            alias = _live_raw_champion_alias(player.get("rawChampionName"))
            return alias_to_id.get(alias.lower())
    return None


def get_gameflow_team_snapshot(session: dict | None, current_summoner: dict | None) -> dict | None:
    """Read a full 5v5 composition from /lol-gameflow/v1/session.

    Mayhem can enter gameflow InProgress while the ordinary champ-select
    endpoint returns "No active delegate" and port 2999 is not ready yet.
    In that window, gameflow.gameData already contains teamOne/teamTwo and
    playerChampionSelections, which is enough for the recommender.
    """
    if not isinstance(session, dict):
        return None
    game_data = session.get("gameData") or {}
    team_one = game_data.get("teamOne") or []
    team_two = game_data.get("teamTwo") or []
    if len(team_one) != 5 or len(team_two) != 5:
        return None

    current_puuid = str((current_summoner or {}).get("puuid") or "")
    current_summoner_id = int((current_summoner or {}).get("summonerId") or 0)

    def champion_ids(rows: list[dict]) -> list[int]:
        return [int(row.get("championId") or 0) for row in rows]

    def contains_current(rows: list[dict]) -> bool:
        for row in rows:
            if current_puuid and str(row.get("puuid") or "") == current_puuid:
                return True
            if current_summoner_id and int(row.get("summonerId") or 0) == current_summoner_id:
                return True
        return False

    if contains_current(team_two):
        my_rows, enemy_rows = team_two, team_one
    else:
        my_rows, enemy_rows = team_one, team_two

    my_team_ids = champion_ids(my_rows)
    enemy_team_ids = champion_ids(enemy_rows)
    if any(cid <= 0 for cid in [*my_team_ids, *enemy_team_ids]):
        return None

    active_champion_id = my_team_ids[0]
    for row in my_rows:
        if current_puuid and str(row.get("puuid") or "") == current_puuid:
            active_champion_id = int(row.get("championId") or active_champion_id)
            break
        if current_summoner_id and int(row.get("summonerId") or 0) == current_summoner_id:
            active_champion_id = int(row.get("championId") or active_champion_id)
            break

    return {
        "my_team_ids": my_team_ids,
        "enemy_team_ids": enemy_team_ids,
        "active_champion_id": active_champion_id,
        "names": {},
        "game_time": None,
    }


def _sigmoid_scalar(logit: float) -> float:
    if logit >= 0:
        z = math.exp(-logit)
        return 1.0 / (1.0 + z)
    z = math.exp(logit)
    return z / (1.0 + z)


def predict_matchup_prob(
    my_team_ids: list[int],
    enemy_team_ids: list[int],
    model,
    composition_model,
) -> float:
    """Predict P(my team wins) with both in-game teams known."""
    if len(my_team_ids) != 5 or len(enemy_team_ids) != 5:
        return float("nan")

    if composition_model is not None:
        my_logit, my_unknown = composition_model.team_logit_contribution(my_team_ids)
        enemy_logit, enemy_unknown = composition_model.team_logit_contribution(enemy_team_ids)
        if not my_unknown and not enemy_unknown:
            return _sigmoid_scalar(float(my_logit - enemy_logit + composition_model.intercept))

    logit = float(model.intercept)
    for cid in my_team_ids:
        idx = model.champ_to_idx.get(int(cid))
        if idx is None:
            return float("nan")
        logit += float(model.coef[idx])
    for cid in enemy_team_ids:
        idx = model.champ_to_idx.get(int(cid))
        if idx is None:
            return float("nan")
        logit -= float(model.coef[idx])
    return _sigmoid_scalar(logit)


def poll_loop(
    stop_event: threading.Event, q: queue.Queue, model, pair_stats, composition_model, creds,
    poll_interval: float, verbose: bool = False,
) -> None:
    """Run in background thread.  Pushes messages onto `q`:
      ("static", id_to_name)         - once, after LCU static data loads
      ("idle", phase)                - when not in (or about to leave) champ select
      ("suggestions", parsed, sugs)  - when champ select state changes
      ("error", message)             - on unrecoverable failure

    When verbose, also prints a status line to stdout on every poll so the
    user can see what the LCU is returning (phase + session presence)
    while watching the terminal during a real game.
    """
    def log(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    current_creds = creds
    alias_to_id = load_champion_alias_to_id()

    while not stop_event.is_set():
        if current_creds is None:
            current_creds = get_credentials()
            if current_creds is None:
                q.put(("status", "Waiting for League client", "No LCU credentials found."))
                log("[poll] waiting for LCU credentials")
                stop_event.wait(max(poll_interval, 2.0))
                continue

        try:
            with LCUClient(current_creds) as lcu:
                if get_current_summoner(lcu) is None:
                    q.put(("status", "LCU not ready", "Refreshing League client credentials..."))
                    log("[poll] LCU health check failed; refreshing credentials")
                    current_creds = None
                    stop_event.wait(max(poll_interval, 1.0))
                    continue
                id_to_name: dict[int, str] = {}
                for entry in get_champion_summary(lcu):
                    cid = entry.get("id")
                    name = entry.get("name") or entry.get("alias")
                    if isinstance(cid, int) and isinstance(name, str) and cid > 0:
                        id_to_name[cid] = name
                q.put(("static", id_to_name))
                log(f"[poll] loaded {len(id_to_name)} champion names from LCU")

                last_hash: tuple | None = None
                last_phase: str | None = None

                while not stop_event.is_set():
                    session = get_champ_select_session(lcu)
                    parsed = parse_session(session) if session else None

                    if parsed is None:
                        phase = get_gameflow_phase(lcu)
                        if phase == "None" and get_current_summoner(lcu) is None:
                            q.put(("status", "LCU reconnecting", "League client credentials changed."))
                            log("[poll] LCU became unreachable; reconnecting")
                            current_creds = None
                            break
                        if phase == "InProgress":
                            snapshot = get_live_game_snapshot(alias_to_id)
                            if snapshot is None:
                                snapshot = get_gameflow_team_snapshot(
                                    get_gameflow_session(lcu),
                                    get_current_summoner(lcu),
                                )
                            if snapshot is not None:
                                state = (
                                    "ingame",
                                    tuple(snapshot["my_team_ids"]),
                                    tuple(snapshot["enemy_team_ids"]),
                                )
                                if state != last_hash:
                                    current_combo = describe_team_combo(
                                        snapshot["my_team_ids"],
                                        model,
                                        composition_model,
                                    )
                                    enemy_combo = describe_team_combo(
                                        snapshot["enemy_team_ids"],
                                        model,
                                        composition_model,
                                    )
                                    snapshot["matchup_prob"] = predict_matchup_prob(
                                        snapshot["my_team_ids"],
                                        snapshot["enemy_team_ids"],
                                        model,
                                        composition_model,
                                    )
                                    q.put(("ingame", snapshot, current_combo, enemy_combo))
                                    last_hash = state
                                stop_event.wait(max(poll_interval, 2.0))
                                continue
                            active_id = get_live_active_champion(alias_to_id)
                            if active_id:
                                q.put(("active_champion", active_id))
                            phase = "InProgress (live data unavailable)"
                        phase_label = f"{phase} (session incomplete)" if session else phase
                        if verbose or phase != last_phase:
                            log(f"[poll] idle  phase={phase_label}  "
                                f"session={'yes(incomplete)' if session else 'no'}")
                        if phase != last_phase:
                            q.put(("idle", phase_label))
                            last_phase = phase
                            last_hash = None
                        stop_event.wait(max(poll_interval, 2.0))
                        continue
                    last_phase = "ChampSelect"

                    state = session_state_hash(parsed)
                    if state != last_hash:
                        suggestions = suggest_for_cell(
                            parsed.my_team_ids,
                            parsed.my_current_id,
                            parsed.bench_ids,
                            model,
                            pair_stats,
                            composition_model,
                        )
                        combos = best_available_team_combos(
                            parsed.my_team_ids,
                            parsed.bench_ids,
                            model,
                            composition_model,
                        )
                        current_combo = describe_team_combo(
                            parsed.my_team_ids,
                            model,
                            composition_model,
                        )
                        q.put(("suggestions", parsed, suggestions, combos, current_combo))
                        last_hash = state
                        log(f"[poll] champ-select update  cell={parsed.my_cell_id}  "
                            f"current={parsed.my_current_id}  bench={len(parsed.bench_ids)}")

                    stop_event.wait(poll_interval)
        except Exception as exc:  # pragma: no cover - surfaced to GUI as status
            q.put(("status", "LCU reconnecting", repr(exc)))
            log(f"[poll] reconnect after error: {exc!r}")
            current_creds = None
            stop_event.wait(max(poll_interval, 2.0))


def fake_poll_loop(
    stop_event: threading.Event,
    q: queue.Queue,
    model,
    pair_stats: PairSynergyStats | RoleSynergyStats | None,
    composition_model,
    interval: float = 3.0,
    ingame: bool = False,
) -> None:
    """Synthetic poll loop for --fake mode.

    Emits randomly-generated champ-select states every `interval` seconds so
    the GUI can be validated without an LCU connection.  Predictions use the
    real LR model on the random teams, so delta magnitudes match what real
    play would produce - only the champion picks are synthetic.

    Bench size is randomized between 5 and 10 each tick to exercise the
    GUI's vertical scrolling and to match the bench sizes a real ARAM
    queue produces once teammates start rerolling.

    With `ingame`, emits one random 5v5 in-game state instead (the real loop
    also sends that view once per game, not once per poll).
    """
    import random

    names = load_fallback_champion_names()
    q.put(("static", names))
    all_ids = sorted(model.champ_to_idx.keys())
    cell_id = 2

    if ingame:
        sample = random.sample(all_ids, 10)
        my_team, enemy_team = sample[:5], sample[5:]
        snapshot = {
            "my_team_ids": my_team,
            "enemy_team_ids": enemy_team,
            "active_champion_id": my_team[cell_id],
            "names": {cid: names[cid] for cid in sample if cid in names},
            "matchup_prob": predict_matchup_prob(my_team, enemy_team, model, composition_model),
        }
        q.put((
            "ingame", snapshot,
            describe_team_combo(my_team, model, composition_model),
            describe_team_combo(enemy_team, model, composition_model),
        ))
        stop_event.wait()
        return

    while not stop_event.is_set():
        bench_size = random.randint(5, 10)
        sample = random.sample(all_ids, 5 + bench_size)
        my_team = sample[:5]
        bench = sample[5:]
        my_current = my_team[cell_id]

        parsed = ParsedSession(
            my_team_ids=my_team,
            my_current_id=my_current,
            my_cell_id=cell_id,
            bench_ids=bench,
            bench_enabled=True,
        )
        suggestions = suggest_for_cell(my_team, my_current, bench, model, pair_stats, composition_model)
        combos = best_available_team_combos(my_team, bench, model, composition_model)
        current_combo = describe_team_combo(my_team, model, composition_model)
        q.put(("suggestions", parsed, suggestions, combos, current_combo))
        stop_event.wait(interval)


# ---------- GUI ----------

# Palette - aligned with the public site: slate neutrals plus Mayhem-like
# tier accents.  The GUI runs over a visually busy League client, so clarity
# wins over transparency.
# Three depth layers: the header band (lit, cool navy, continuous with the
# DWM title bar), the body floor (darkest), and panels raised off the floor
# with a hairline edge.  Depth comes from these value steps, not shadows.
HEAD_TOP  = "#1c2333"  # also the Windows caption colour, so the band starts at the title bar
HEAD_BOT  = "#121722"  # header gradient end and the tab strip
BG        = "#0a0d13"  # body floor
SURFACE   = "#121722"  # raised panel
EDGE      = "#232b3a"  # panel hairline
ROW       = "#181e2b"  # row stripe inside a panel
ROW_HI    = "#1f2636"  # emphasised row (best combo)
BEST_BG   = "#18261a"
WARN_BG   = "#2a1918"
FG        = "#e8eaed"
DIM       = "#9aa3b2"
MUTED     = "#5f6877"
DIVIDER   = "#2a3242"
GOLD      = "#f5c518"
GOLD_DIM  = "#8a6f12"
SITE_BG   = "#0a0b0d"  # site.css --bg, so the WebView area has no seam

# (key, label): the first four are site embed tabs, matchup is the Tk view.
SITE_TABS = (
    ("overview", "概覽"),
    ("compare", "詳細"),
    ("items", "裝備"),
    ("augments", "增幅"),
    ("matchup", "對戰勝率"),
)
GREEN     = "#8ec441"
RED       = "#ff6a4a"
BLUE      = "#3aa0ff"

# Fonts - Segoe UI for prose (Windows default sans, ships with the OS and
# pairs well next to League's own Latin UI), Consolas for tabular numbers
# so Δ% and z columns stay aligned across rows.
FONT_HEAD    = ("Microsoft JhengHei UI", 15, "bold")
FONT_SUB     = ("Microsoft JhengHei UI", 9)
FONT_SECTION = ("Microsoft JhengHei UI", 8, "bold")
FONT_NAME    = ("Microsoft JhengHei UI", 11)
FONT_NAME_B  = ("Microsoft JhengHei UI", 11, "bold")
FONT_SCORE    = ("Bahnschrift", 24, "bold")
FONT_ICON     = ("Segoe UI Symbol", 11)
# Bahnschrift (Windows 10+) has tabular lining figures, so columns still
# align, without Consolas' terminal look.
FONT_NUM      = ("Bahnschrift", 11)
FONT_NUM_B    = ("Bahnschrift", 11, "bold")
FONT_NUM_BEST = ("Bahnschrift", 15, "bold")
FONT_TAB      = ("Microsoft JhengHei UI", 10)
FONT_PANEL    = ("Microsoft JhengHei UI", 9, "bold")
AUGMENT_ICON_PX = 64
AUGMENT_RARITY_COLORS = {"kSilver": "#9fb0c0", "kGold": "#d4a73a", "kPrismatic": "#b77cff"}

# U+2212 MINUS SIGN - proper typographic minus instead of HYPHEN-MINUS.
# Same width as "+" in Consolas so the columns still align.
MINUS = "−"
COPY_ICON = "⧉"
COPIED_ICON = "✓"


def _fmt_signed_pct(value_pp: float) -> str:
    """Format a percentage-point delta with a typographic minus for negatives."""
    if math.isnan(value_pp):
        return "n/a"
    if value_pp > 0:
        return f"+{value_pp:.1f}%"
    if value_pp < 0:
        return f"{MINUS}{abs(value_pp):.1f}%"
    return f" {value_pp:.1f}%"


def _fmt_signed_z(z: float) -> str:
    """Format a z-score with a typographic minus for negatives."""
    if math.isnan(z):
        return "n/a"
    if z > 0:
        return f"+{z:.2f}"
    if z < 0:
        return f"{MINUS}{abs(z):.2f}"
    return f" {z:.2f}"


def _fmt_prob(value: float) -> str:
    if math.isnan(value):
        return "n/a"
    return f"{value * 100:.1f}%"


def _fmt_rating(value: float) -> str:
    if math.isnan(value):
        return "n/a"
    value = max(0.0, min(5.0, float(value)))
    bands = (
        (4.75, "S+"),
        (4.40, "S"),
        (4.05, "S-"),
        (3.75, "A+"),
        (3.35, "A"),
        (3.05, "A-"),
        (2.75, "B+"),
        (2.35, "B"),
        (2.05, "B-"),
        (1.75, "C+"),
        (1.35, "C"),
        (1.05, "C-"),
        (0.75, "D+"),
        (0.35, "D"),
    )
    for cutoff, tier in bands:
        if value >= cutoff:
            return tier
    return "D-"


def _rating_value_text(rating) -> str:
    if rating.label == "AD佔比" and rating.detail:
        return f"{rating.detail} {_fmt_rating(rating.value)}"
    return _fmt_rating(rating.value)


def _rating_copy_text(rating) -> str:
    if rating.label == "AD佔比" and rating.detail:
        return f"AD佔比：{rating.detail} {_fmt_rating(rating.value)}"
    detail = f"（{rating.detail}）" if rating.detail else ""
    return f"{rating.label}：{_fmt_rating(rating.value)}{detail}"


def _make_observer_window(root: tk.Tk) -> None:
    """Keep the overlay visible without activating the IME/input focus on Windows."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        root.update_idletasks()
        hwnd = int(root.winfo_id())
        user32 = ctypes.windll.user32
        get_style = user32.GetWindowLongPtrW
        set_style = user32.SetWindowLongPtrW
        get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
        get_style.restype = ctypes.c_ssize_t
        set_style.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        set_style.restype = ctypes.c_ssize_t

        gwl_exstyle = -20
        ws_ex_noactivate = 0x08000000
        style = get_style(hwnd, gwl_exstyle)
        set_style(hwnd, gwl_exstyle, style | ws_ex_noactivate)

        # Re-show without activation so Chinese IME toolbars do not pop up
        # over League when this passive panel refreshes or starts.
        sw_shownoactivate = 4
        user32.ShowWindow(hwnd, sw_shownoactivate)
    except Exception:
        # The overlay is still usable if the platform call fails.
        return


def _colorref(hex_color: str) -> int:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return r | (g << 8) | (b << 16)


def _style_title_bar(root: tk.Tk, caption: str = HEAD_TOP, text: str = FG) -> None:
    """Paint the native title bar to match the header band (Windows 11).

    Without this Windows draws its default light caption above a dark app.
    Dark mode (attr 20) covers Windows 10 20H1+; the caption/text/border
    colours (35/36/34) are Windows 11 only and are silently ignored earlier.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        set_attr = ctypes.windll.dwmapi.DwmSetWindowAttribute
        for attr, value in ((20, 1), (35, _colorref(caption)), (36, _colorref(text)),
                            (34, _colorref(caption))):
            val = ctypes.c_int(value)
            set_attr(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        return


def _mix(a: str, b: str, t: float) -> str:
    """Linear blend of two #rrggbb colours (t=0 -> a, t=1 -> b)."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ca, cb))


class _CanvasText:
    """Label-like `config(text=, fg=, font=)` facade over a canvas text item."""

    def __init__(self, canvas: tk.Canvas, item: int) -> None:
        self.canvas, self.item = canvas, item

    def config(self, **kw) -> None:
        opts = {}
        if "text" in kw:
            opts["text"] = kw["text"]
        if "fg" in kw:
            opts["fill"] = kw["fg"]
        if "font" in kw:
            opts["font"] = kw["font"]
        self.canvas.itemconfigure(self.item, **opts)

    configure = config


class _HeaderBand:
    """Gradient header: title, subtitle and the pin toggle drawn on a canvas.

    Tk widgets are opaque, so anything placed over a gradient would punch a
    flat rectangle into it; everything here is a canvas item instead.  The
    gradient is a PhotoImage rebuilt (debounced) when the width changes.
    """

    HEIGHT = 74

    def __init__(self, root: tk.Tk, font_head, font_sub, on_pin) -> None:
        self.canvas = tk.Canvas(root, height=self.HEIGHT, bg=HEAD_TOP, highlightthickness=0, bd=0)
        self._image = None
        self._width = 0
        self._pending = None
        self._bg = self.canvas.create_image(0, 0, anchor="nw")
        self.canvas.create_rectangle(16, 18, 19, 56, fill=GOLD, width=0)
        self.title = _CanvasText(self.canvas, self.canvas.create_text(
            30, 16, anchor="nw", text="Loading...", fill=FG, font=font_head))
        self.sub = _CanvasText(self.canvas, self.canvas.create_text(
            31, 50, anchor="nw", text="", fill=DIM, font=font_sub))
        self._pin_box = self.canvas.create_polygon(0, 0, 0, 0, smooth=True, width=1, tags=("pin",))
        self._pin_text = self.canvas.create_text(0, 0, text="📌 置頂", font=font_sub, tags=("pin",))
        self.canvas.tag_bind("pin", "<Button-1>", lambda _e: on_pin())
        self.canvas.tag_bind("pin", "<Enter>", lambda _e: self.canvas.config(cursor="hand2"))
        self.canvas.tag_bind("pin", "<Leave>", lambda _e: self.canvas.config(cursor=""))
        self.canvas.bind("<Configure>", self._on_configure)

    def set_pin(self, on: bool, font_sub) -> None:
        self.canvas.itemconfigure(self._pin_text, fill=GOLD if on else DIM, font=font_sub)
        self.canvas.itemconfigure(self._pin_box, outline=GOLD_DIM if on else EDGE,
                                  fill=_mix(HEAD_TOP, GOLD, 0.08) if on else HEAD_TOP)
        self._place_pin()

    def _place_pin(self) -> None:
        width = self._width or self.canvas.winfo_width()
        x1, y0, y1 = width - 16, 20, 46
        bbox = self.canvas.bbox(self._pin_text)
        text_w = (bbox[2] - bbox[0]) if bbox else 48
        x0 = x1 - text_w - 24
        r = 8
        pts = (x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
               x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0)
        self.canvas.coords(self._pin_box, *pts)
        self.canvas.coords(self._pin_text, (x0 + x1) / 2, (y0 + y1) / 2)

    def _on_configure(self, event) -> None:
        if event.width == self._width:
            return
        self._width = event.width
        self._place_pin()
        if self._pending is not None:
            self.canvas.after_cancel(self._pending)
        self._pending = self.canvas.after(60, self._paint)

    def _paint(self) -> None:
        self._pending = None
        w, h = max(self._width, 1), self.HEIGHT
        # Vertical fall-off from the caption colour, plus a faint warm glow
        # rising from the lower left behind the gold tick.
        img = tk.PhotoImage(width=w, height=h)
        step = 8  # colour bands this wide; keeps the build ~10 ms at 900 px
        for y in range(h):
            ty = y / (h - 1)
            base = _mix(HEAD_TOP, HEAD_BOT, ty ** 1.4)
            row = []
            for x0 in range(0, w, step):
                d = ((x0 / w) / 0.55) ** 2 + ((1 - ty) / 1.3) ** 2
                glow = max(0.0, 1 - d) * 0.07 * ty  # zero on row 0: seamless with the caption
                row.append(" ".join([_mix(base, GOLD, glow)] * min(step, w - x0)))
            img.put("{" + " ".join(row) + "}", to=(0, y))
        self._image = img
        self.canvas.itemconfigure(self._bg, image=img)
        self.canvas.tag_lower(self._bg)


class RecommenderApp:
    def __init__(self, root: tk.Tk, q: queue.Queue, icon_cache: IconCache | None = None) -> None:
        self.root = root
        self.q = q
        self.id_to_name: dict[int, str] = {}
        self.icon_cache = icon_cache
        self.font_scale = 1.0
        self._last_render_args: tuple | None = None
        self.active_champion_id: int | None = None
        self._loadout_status: dict[int, str] = {}
        self.augment_advisor = AugmentAdvisor()
        self.quick_source = _quick_pick_source(
            self.augment_advisor.payload, self.augment_advisor.payload_path,
        )
        self._quick_state: dict[int, str] = {}  # champion id -> loading | ready | failed
        self._quick_route: dict[int, int] = {}
        self._asset_photos: dict[tuple[str, int], tk.PhotoImage] = {}
        self._last_ingame_args: tuple | None = None
        self.latest_augment_event: dict | None = None
        self._last_augment_signature: tuple | None = None

        root.title("ARAM Recommender")
        _set_window_icon(root)
        self.topmost = _load_settings().get("topmost", True)
        root.attributes("-topmost", self.topmost)
        root.attributes("-alpha", 1.0)
        root.geometry("860x800+40+40")
        root.configure(bg=BG)
        root.minsize(760, 560)

        # Pixel-perfect column geometry, applied identically to every row
        # frame so cells line up regardless of font.  Tk widget `width=N`
        # is in font-average chars; mixing FONT_SECTION (Segoe UI
        # proportional) with FONT_NUM (Consolas mono) at the same `width=N`
        # produced visibly different pixel widths in earlier versions.
        # Pinning to minsize fixes it regardless of font.
        self._base_cols = {
            "icon": 42,
            "delta": 82,
            "prob": 70,
            "z": 70,
        }
        self._apply_zoom_geometry()

        # Tk widget constructors only accept a single int for padx/pady
        # (internal padding).  Asymmetric padding goes on the geometry
        # manager call (.pack / .grid).  We use that distinction to set
        # generous outer rhythm without bloating the labels themselves.
        _style_title_bar(root)
        self.band = _HeaderBand(root, self._font(FONT_HEAD), self._font(FONT_SUB), self._toggle_topmost)
        self.band.canvas.pack(fill="x")
        self.header = self.band.title
        self.subheader = self.band.sub
        self._style_pin()
        if DESKTOP_STATUS:
            tk.Label(root, text=DESKTOP_STATUS, bg=HEAD_BOT, fg=DIM,
                     font=self._font(FONT_SUB), anchor="w", padx=16, wraplength=800).pack(fill="x")

        # Live augment offer (Overwolf GEP -> probe app -> embedded bridge).
        # The panel is only packed while an offer is live (see is_live_offer),
        # so a missing Overwolf or a finished pick never leaves stale text.
        self.augment_panel = tk.Frame(root, bg=BG)
        self._augment_panel_shown = False
        self._augment_bridge = start_bridge_thread(_augment_event_path().parent)
        # Overwolf will not load the probe app without Riot approval, so the
        # offer is also read off the screen (only while the game is focused).
        self._augment_ocr = None
        if os.environ.get("ARAM_AUGMENT_OCR", "1") != "0":
            self._augment_ocr = start_ocr_watcher(_augment_event_path().parent, {
                str(meta.get("name_zh")): aid
                for aid, meta in self.augment_advisor.augments.items()
                if meta.get("name_zh")
            })
        # Created at startup (not on the first offer) so it is never mapped
        # while the game has focus.
        self._augment_overlay = None
        if os.environ.get("ARAM_AUGMENT_OVERLAY", "1") != "0":
            try:
                self._augment_overlay = AugmentOverlay(root)
            except tk.TclError:
                self._augment_overlay = None

        # Site tabs mirror the public champion page; 對戰勝率 is the native
        # matchup view.  The WebView is only created on the first site-tab
        # click, so users who never leave 對戰勝率 pay nothing for it.
        self.tab = "matchup"
        self._tab_buttons: dict[str, tk.Button] = {}
        self.site_view = None
        self._site_error: str | None = None
        # Underline tabs on the band's tail colour; a hairline closes the
        # header so the body floor reads as a separate, deeper layer.
        tab_strip = tk.Frame(root, bg=HEAD_BOT)
        tab_strip.pack(fill="x")
        tab_bar = tk.Frame(tab_strip, bg=HEAD_BOT)
        tab_bar.pack(fill="x", padx=10)
        self._tab_rule = tk.Frame(root, bg=EDGE, height=1)
        self._tab_rule.pack(fill="x")
        self._tab_bar = tab_strip
        self._tab_marks: dict[str, tk.Frame] = {}
        for key, label in SITE_TABS:
            cell = tk.Frame(tab_bar, bg=HEAD_BOT)
            cell.pack(side="left", padx=(0, 2))
            button = tk.Button(
                cell, text=label, command=lambda k=key: self._select_tab(k),
                relief="flat", bd=0, padx=12, pady=6, cursor="hand2",
                bg=HEAD_BOT, activebackground=HEAD_BOT, activeforeground=FG,
                font=self._font(FONT_TAB),
            )
            button.pack()
            mark = tk.Frame(cell, height=2, bg=HEAD_BOT)
            mark.pack(fill="x", padx=8)
            self._tab_buttons[key] = button
            self._tab_marks[key] = mark
        self._style_tabs()

        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=14, pady=(12, 14))
        self.web_frame = tk.Frame(root, bg=SITE_BG)

        _make_observer_window(root)
        self.root.after_idle(lambda: _make_observer_window(root))
        self._bind_zoom_shortcuts()

        # Begin draining the queue.
        self.root.after(100, self._drain)
        self.root.after(750, self._poll_augments)

    # ----- Queue handling -----

    def _font(self, base: tuple) -> tuple:
        family, size, *style = base
        scaled_size = max(7, int(round(size * self.font_scale)))
        return (family, scaled_size, *style)

    def _apply_zoom_geometry(self) -> None:
        self.COL_ICON = self._base_cols["icon"]
        self.COL_DELTA = int(round(self._base_cols["delta"] * self.font_scale))
        self.COL_PROB = int(round(self._base_cols["prob"] * self.font_scale))
        self.COL_Z = int(round(self._base_cols["z"] * self.font_scale))

    def _bind_zoom_shortcuts(self) -> None:
        for seq in ("<Control-plus>", "<Control-KP_Add>", "<Control-equal>", "<Control-Shift-equal>"):
            self.root.bind_all(seq, lambda _event, delta=0.1: self._adjust_font_scale(delta))
        for seq in ("<Control-minus>", "<Control-KP_Subtract>"):
            self.root.bind_all(seq, lambda _event, delta=-0.1: self._adjust_font_scale(delta))
        for seq in ("<Control-0>", "<Control-KP_0>"):
            self.root.bind_all(seq, lambda _event: self._reset_font_scale())

    def _adjust_font_scale(self, delta: float) -> str:
        self.font_scale = max(0.8, min(1.6, round(self.font_scale + delta, 2)))
        self._apply_zoom()
        return "break"

    def _reset_font_scale(self) -> str:
        self.font_scale = 1.0
        self._apply_zoom()
        return "break"

    def _apply_zoom(self) -> None:
        self._apply_zoom_geometry()
        self.header.config(font=self._font(FONT_HEAD))
        self.subheader.config(font=self._font(FONT_SUB))
        self._style_pin()
        for button in self._tab_buttons.values():
            button.config(font=self._font(FONT_TAB))
        self._render_augment_panel()
        if self._last_render_args is not None:
            self._render(*self._last_render_args)
        elif self._last_ingame_args is not None:
            self._render_ingame(*self._last_ingame_args)

    def _drain(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                try:
                    self._handle(msg)
                except Exception:
                    # A single bad frame (edge-case champ-select state, missing
                    # icon, unexpected None) must never escape the drain loop:
                    # if it did, the `after` reschedule below would be skipped
                    # and the overlay would freeze on its last good render while
                    # the poll thread keeps queueing fresh updates.  Log the
                    # frame and move on instead.
                    _log_gui_exception(f"_handle kind={msg[0] if msg else '?'}")
        except queue.Empty:
            pass
        finally:
            # Reschedule unconditionally — the update loop must outlive any
            # error above, otherwise the GUI silently stops refreshing.
            self.root.after(150, self._drain)

    def _poll_augments(self, force: bool = False) -> None:
        event = read_latest_augment_event()
        signature = None
        if event is not None:
            signature = (
                event.get("path"),
                event.get("mtime_ns"),
                event.get("received_at"),
                event.get("type"),
                tuple(
                    (aug.get("slot"), aug.get("name"), json.dumps(aug.get("raw"), sort_keys=True, default=str))
                    for aug in event.get("augments") or []
                    if isinstance(aug, dict)
                ),
            )
        live = is_live_offer(event)
        signature = (signature, live, self.active_champion_id)
        if force or signature != self._last_augment_signature:
            self.latest_augment_event = event if live else None
            self._last_augment_signature = signature
            self._render_augment_panel()
        if not force:
            self.root.after(1000, self._poll_augments)

    def _render_augment_panel(self) -> None:
        for widget in self.augment_panel.winfo_children():
            widget.destroy()

        event = self.latest_augment_event
        augments = [aug for aug in (event or {}).get("augments") or [] if isinstance(aug, dict)]
        rows = self.augment_advisor.recommend(self.active_champion_id, augments) if augments else []
        if rows and all(row.get("wr") is None for row in rows):
            rows = self._recheck_augment_champion(augments, rows)
        self._write_augment_state(rows)
        overlay = getattr(self, "_augment_overlay", None)
        if overlay is not None:
            overlay.set_badges(badge_layout(rows))
        if not rows:
            if self._augment_panel_shown:
                self.augment_panel.pack_forget()
                self._augment_panel_shown = False
            return

        if not self._augment_panel_shown:
            self.augment_panel.pack(fill="x", padx=14, pady=(10, 0), after=self._tab_rule)
            self._augment_panel_shown = True

        panel = tk.Frame(self.augment_panel, bg=BG)
        panel.pack(fill="x")
        champ = (self.augment_advisor.champs.get(self.active_champion_id or -1) or {}).get("name_zh")
        title = f"海克斯選項 · {champ}" if champ else "海克斯選項 · 等待英雄資料"
        tk.Label(panel, text=title, bg=BG, fg=DIM, font=self._font(FONT_SECTION),
                 anchor="w").pack(fill="x", pady=(0, 4))
        # rows come best-first; cards keep the in-game left-to-right order so
        # each one sits under the same position as on screen.
        best_id = rows[0].get("id") if rows[0].get("wr") is not None else None
        cards = tk.Frame(panel, bg=BG)
        cards.pack(fill="x")
        ordered = sorted(rows, key=lambda row: row.get("order") or 0)
        for col, row in enumerate(ordered):
            cards.grid_columnconfigure(col, weight=1, uniform="augcard")
            self._augment_card(cards, row, best=row.get("id") is not None and row.get("id") == best_id).grid(
                row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 6, 0))

    def _recheck_augment_champion(self, augments: list[dict], rows: list[dict]) -> list[dict]:
        """No card has data: ask Live Client directly which champion we are.

        The poll thread can hold a stale/unknown champion (practice and custom
        games have no 5v5 roster).  Asked once per offer, short timeout, since
        this runs on the Tk thread.
        """
        key = tuple(aug.get("id") or aug.get("name") for aug in augments)
        if getattr(self, "_augment_recheck_key", None) == key:
            return rows
        self._augment_recheck_key = key
        if getattr(self, "_alias_to_id", None) is None:
            self._alias_to_id = load_champion_alias_to_id()
        champion_id = get_live_active_champion(self._alias_to_id, timeout_sec=0.4)
        if not champion_id or champion_id == self.active_champion_id:
            return rows
        self.active_champion_id = champion_id
        return self.augment_advisor.recommend(champion_id, augments)

    def _write_augment_state(self, rows: list[dict]) -> None:
        """What the panel/overlay showed, for diagnosing "no data" reports."""
        state = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "champion_id": self.active_champion_id,
            "rows": [{k: row.get(k) for k in ("id", "name", "wr", "games", "rank", "screen")} for row in rows],
        }
        try:
            path = _augment_event_path().with_name("overlay_state.json")
            path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _augment_card(self, parent: tk.Frame, row: dict, best: bool) -> tk.Frame:
        rarity_color = AUGMENT_RARITY_COLORS.get(str(row.get("rarity") or ""), DIVIDER)
        card = tk.Frame(parent, bg=ROW, highlightthickness=2 if best else 1,
                        highlightbackground=GREEN if best else rarity_color)
        photo = self._augment_photo(row.get("id"))
        icon = tk.Label(card, bg=ROW, bd=0)
        if photo is not None:
            icon.config(image=photo)
            icon.image = photo  # type: ignore[attr-defined]
        icon.pack(pady=(8, 2))
        name = str(row.get("name") or row.get("raw_name") or "?")
        tk.Label(card, text=("★ " if best else "") + name, bg=ROW, fg=GREEN if best else FG,
                 font=self._font(FONT_NAME_B), wraplength=180).pack(padx=6)
        if row.get("id") is None:
            stat, sub, fg = "未收錄", "", GOLD
        elif row.get("wr") is None:
            stat, sub, fg = "—", "此英雄無資料" if self.active_champion_id else "等待英雄資料", DIM
        else:
            stat, fg = _fmt_prob(float(row["wr"])), GREEN if best else FG
            lift = row.get("lift")
            sub = f"比此英雄平均 {_fmt_signed_pct(float(lift) * 100)}" if lift is not None else ""
        tk.Label(card, text=stat, bg=ROW, fg=fg, font=self._font(FONT_NUM_BEST)).pack(pady=(4, 0))
        tk.Label(card, text=sub, bg=ROW, fg=DIM, font=self._font(FONT_SUB)).pack(pady=(0, 8))
        return card

    def _augment_photo(self, augment_id: int | None) -> tk.PhotoImage | None:
        key = (f"augment:{augment_id}", AUGMENT_ICON_PX)
        if key in self._asset_photos:
            return self._asset_photos[key]
        path = self.augment_advisor.icon_path(augment_id)
        if path is None:
            return None
        try:
            photo = tk.PhotoImage(file=str(path))
            factor = max(1, round(photo.width() / AUGMENT_ICON_PX))
            if factor > 1:
                photo = photo.subsample(factor, factor)
        except tk.TclError:
            return None
        self._asset_photos[key] = photo
        return photo

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind not in ("ingame", "quickpicks", "static", "siteview_error", "active_champion"):
            self._last_ingame_args = None
        if kind == "static":
            self.id_to_name = msg[1]
            self.header.config(text="Waiting for champ select", fg=FG)
            self.subheader.config(text=f"{len(self.id_to_name)} champions loaded")
            self._clear_body()
        elif kind == "idle":
            phase = msg[1]
            self._loadout_status.clear()
            self.header.config(text=f"Idle · {phase}", fg=DIM)
            self.subheader.config(text="Queue for ARAM/Mayhem to see swap suggestions.")
            self._clear_body()
        elif kind == "status":
            _, title, detail = msg
            self.header.config(text=title, fg=DIM)
            self.subheader.config(text=detail)
            self._clear_body()
        elif kind == "error":
            self.header.config(text="LCU error", fg=RED)
            self.subheader.config(text=msg[1])
            self._clear_body()
        elif kind == "suggestions":
            _, parsed, suggestions, *rest = msg
            self.active_champion_id = int(parsed.my_current_id)
            combos = rest[0] if rest else []
            current_combo = rest[1] if len(rest) > 1 else None
            self._render(parsed, suggestions, combos, current_combo)
            self._render_augment_panel()
            self._sync_site()
        elif kind == "ingame":
            _, snapshot, current_combo, enemy_combo = msg
            active = snapshot.get("active_champion_id")
            self.active_champion_id = int(active) if active else None
            if self._quick_state.get(self.active_champion_id) == "failed":
                del self._quick_state[self.active_champion_id]  # new game, try again
            self._render_ingame(snapshot, current_combo, enemy_combo)
            self._render_augment_panel()
            self._sync_site()
        elif kind == "active_champion":
            if self.active_champion_id != msg[1]:
                self.active_champion_id = int(msg[1])
                self._render_augment_panel()
        elif kind == "quickpicks":
            _, champion_id, ok = msg
            self._quick_state[champion_id] = "ready" if ok else "failed"
            if self.active_champion_id != champion_id:
                return
            if self._last_ingame_args is not None:
                self._render_ingame(*self._last_ingame_args)
            elif self._last_render_args is not None:
                self._render(*self._last_render_args)
        elif kind == "siteview_error":
            self._site_failed(str(msg[1]))

    # ----- Site tabs (WebView2 embed of the public champion page) -----

    def _style_tabs(self) -> None:
        for key, button in self._tab_buttons.items():
            selected = key == self.tab
            button.config(fg=FG if selected else DIM)
            self._tab_marks[key].config(bg=GOLD if selected else HEAD_BOT)

    def _style_pin(self) -> None:
        self.band.set_pin(self.topmost, self._font(FONT_SUB))

    def _toggle_topmost(self) -> None:
        self.topmost = not self.topmost
        self.root.attributes("-topmost", self.topmost)
        self._style_pin()
        _save_settings(topmost=self.topmost)

    def _select_tab(self, key: str) -> None:
        if key == self.tab:
            return
        self.tab = key
        self._style_tabs()
        if key == "matchup":
            self.web_frame.pack_forget()
            self.body.pack(fill="both", expand=True, padx=14, pady=(12, 14))
            return
        self.body.pack_forget()
        self.web_frame.pack(fill="both", expand=True)
        self._sync_site()

    def _sync_site(self) -> None:
        """Point the WebView at the active champion when a site tab is showing."""
        if self.tab == "matchup":
            return
        cid = self.active_champion_id
        if self._site_error is not None:
            self._render_site_fallback()
            return
        if not cid:
            if self.site_view is None or not self.site_view.started:
                self._site_message("選角或進入遊戲後顯示該英雄的網站資料。")
            return
        for widget in self.web_frame.winfo_children():
            widget.destroy()
        try:
            if self.site_view is None:
                from aram_nn.desktop.webview_host import SiteView

                self.site_view = SiteView(
                    self.web_frame, _icon_cache_dir().parent / "webview2",
                    on_event=lambda *event: self.q.put(event),
                )
            self.site_view.show(cid, self.tab)
        except Exception as exc:
            self._site_failed(f"{type(exc).__name__}: {exc}")

    def _site_failed(self, error: str) -> None:
        print(f"[gui] site view unavailable: {error}")
        self._site_error = error
        if self.site_view is not None:
            try:
                self.site_view.close()
            except Exception:
                pass
        if self.tab != "matchup":
            self._render_site_fallback()

    def _site_message(self, text: str) -> tk.Frame:
        for widget in self.web_frame.winfo_children():
            widget.destroy()
        box = tk.Frame(self.web_frame, bg=SITE_BG)
        box.pack(fill="both", expand=True, padx=16, pady=16)
        tk.Label(
            box, text=text, bg=SITE_BG, fg=DIM, font=self._font(FONT_SUB),
            anchor="w", justify="left", wraplength=640,
        ).pack(fill="x")
        return box

    def _render_site_fallback(self) -> None:
        from aram_nn.desktop.webview_host import site_url

        box = self._site_message("這台電腦無法內嵌網站頁（需要 WebView2 Runtime）。")
        alias = ""
        if self.icon_cache is not None and self.active_champion_id:
            alias = getattr(self.icon_cache, "_alias", {}).get(self.active_champion_id, "")
        url = site_url(alias)
        tk.Button(
            box, text="在瀏覽器開啟", command=lambda: webbrowser.open(url),
            bg=ROW, fg=FG, activebackground=DIVIDER, activeforeground=FG,
            relief="flat", bd=0, padx=10, pady=4, cursor="hand2", font=self._font(FONT_SUB),
        ).pack(anchor="w", pady=(10, 0))

    def close_site(self) -> None:
        if self.site_view is not None:
            self.site_view.close()

    # ----- Rendering -----

    def _clear_body(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        # Grid weights survive their widgets; reset so a later pack/grid
        # layout does not inherit the champ-select column split.
        for index in range(3):
            self.body.grid_columnconfigure(index, weight=0, minsize=0, uniform="")
            self.body.grid_rowconfigure(index, weight=0, minsize=0)

    def _panel(self, parent: tk.Frame, title: str, *, pad: int = 12) -> tuple[tk.Frame, tk.Frame, tk.Frame]:
        """Raised section: hairline edge, gold tick + title, content area.

        Returns (outer, head, inner); callers grid/pack `outer` and may
        pack extra controls on the right of `head`.
        """
        outer = tk.Frame(parent, bg=SURFACE, highlightthickness=1,
                         highlightbackground=EDGE, highlightcolor=EDGE)
        head = tk.Frame(outer, bg=SURFACE)
        head.pack(fill="x", padx=pad, pady=(9, 6))
        tk.Frame(head, bg=GOLD, width=3, height=13).pack(side="left", padx=(0, 8))
        tk.Label(head, text=title, bg=SURFACE, fg=DIM, font=self._font(FONT_PANEL),
                 anchor="w").pack(side="left")
        inner = tk.Frame(outer, bg=SURFACE)
        inner.pack(fill="both", expand=True, padx=pad, pady=(0, 11))
        return outer, head, inner

    def _ghost_button(self, parent: tk.Frame, text: str, command, *, bg: str = SURFACE,
                      font: tuple = FONT_SUB, fg: str = DIM) -> tk.Button:
        button = tk.Button(
            parent, text=text, command=command, bg=bg, fg=fg,
            activebackground=ROW_HI, activeforeground=FG, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=EDGE, highlightcolor=EDGE,
            padx=8, pady=2, font=self._font(font), cursor="hand2", takefocus=0,
        )
        button.bind("<Enter>", lambda _e: button.config(fg=FG, bg=ROW_HI))
        button.bind("<Leave>", lambda _e: button.config(fg=fg, bg=bg))
        return button

    def _render(self, parsed, suggestions, combos=None, current_combo=None) -> None:
        combos = combos or []
        self._last_render_args = (parsed, suggestions, combos, current_combo)
        cur_name = self.id_to_name.get(parsed.my_current_id, f"#{parsed.my_current_id}")
        self.header.config(text=f"選角中 · {cur_name}", fg=FG)
        self.subheader.config(
            text=f"位置 {parsed.my_cell_id}　·　MLΔ 換人後勝率變化　·　z 英雄本體強度　·　評分 B ≈ 普通"
        )

        self._clear_body()

        self.body.grid_columnconfigure(0, weight=5, minsize=300, uniform="cs")
        self.body.grid_columnconfigure(1, weight=6, minsize=340, uniform="cs")
        self.body.grid_rowconfigure(1, weight=1)

        if combos:
            outer, head, inner = self._panel(
                self.body, f"最佳 5 人組合　·　掃描 {combos[0].total_combos} 組")
            outer.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
            self._render_combo_section(head, inner, combos, pool=suggestions, owned_ids=parsed.my_team_ids)

        left, _, left_inner = self._panel(self.body, "你的隊伍")
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        right, right_head, right_inner = self._panel(self.body, "替補池")
        right.grid(row=1, column=1, sticky="nsew")

        self._render_team_section(left_inner, parsed, suggestions, current_combo)
        self._render_spell_hint(left_inner, int(parsed.my_current_id))
        self._render_bench_table(right_head, right_inner, suggestions)

    def _render_spell_hint(self, parent: tk.Frame, champion_id: int) -> None:
        """Recommended summoner spells for the champion currently held in champ select."""
        if self.quick_source is None or not champion_id:
            return
        self._request_quick_picks(champion_id)
        if self._quick_state.get(champion_id) != "ready":
            return
        overview = self.quick_source.overview(champion_id, self._quick_route.get(champion_id, 0))
        if overview is None or not overview.spells:
            return
        # One compact line under a hairline: spells on the left, the two
        # client actions on the right, result text beneath when there is one.
        tk.Frame(parent, bg=EDGE, height=1).pack(fill="x", pady=(10, 8))
        row = tk.Frame(parent, bg=SURFACE)
        row.pack(fill="x")
        for spell in overview.spells:
            self._asset_icon(row, spell.icon, 22, SURFACE).pack(side="left", padx=(0, 2))
        tk.Label(
            row, text=" + ".join(spell.name for spell in overview.spells),
            bg=SURFACE, fg=FG, font=self._font(FONT_SUB), anchor="w",
        ).pack(side="left", padx=(5, 0))
        status = tk.Label(parent, text=self._loadout_status.get(champion_id, ""), bg=SURFACE, fg=DIM,
                          font=self._font(FONT_SECTION), anchor="e")
        for label, action in (("匯入裝備", "items"), ("套用技能", "spells")):
            self._ghost_button(
                row, label, lambda a=action: self._import_loadout(champion_id, a, status),
                font=FONT_SECTION,
            ).pack(side="right", padx=(5, 0))
        status.pack(fill="x", pady=(3, 0))

    def _loadout_blocks(self, overview) -> list[tuple[str, list[int]]]:
        core = [item.id for item in overview.items if item.is_core]
        rest = [item.id for item in overview.items if not item.is_core]
        core_label = "核心裝"
        if overview.core_wr is not None:
            core_label += f"（勝率 {_fmt_prob(overview.core_wr)}）"
        blocks = [(core_label, core), ("完整出裝" if overview.complete else "搭配裝備", rest)]
        if overview.boot is not None and overview.boot.id not in rest:
            blocks.append(("鞋子", [overview.boot.id]))
        return blocks

    def _import_loadout(self, champion_id: int, action: str, status: tk.Label) -> None:
        """Push spells or an item set to the client off the Tk thread."""
        overview = self.quick_source.overview(champion_id, self._quick_route.get(champion_id, 0)) \
            if self.quick_source is not None else None
        if overview is None:
            return
        spell_ids = [spell.id for spell in overview.spells]
        blocks = self._loadout_blocks(overview)
        name = self.id_to_name.get(champion_id, f"#{champion_id}")

        def work() -> None:
            from aram_nn.lcu.loadout import apply_item_set, apply_summoner_spells
            creds = get_credentials()
            if creds is None:
                result = (False, "找不到客戶端")
            else:
                with LCUClient(creds) as lcu:
                    if action == "spells":
                        result = apply_summoner_spells(lcu, get_champ_select_session(lcu), spell_ids)
                    else:
                        result = apply_item_set(lcu, champion_id, name, blocks)
            self._loadout_status[champion_id] = result[1]

            def show() -> None:
                try:
                    status.config(text=result[1], fg=GREEN if result[0] else RED)
                except tk.TclError:  # row was re-rendered meanwhile
                    pass
            self.root.after(0, show)

        status.config(text="匯入中…", fg=DIM)
        threading.Thread(target=work, name=f"loadout-{action}", daemon=True).start()

    def _render_ingame(self, snapshot: dict, current_combo=None, enemy_combo=None) -> None:
        self._last_render_args = None
        self._last_ingame_args = (snapshot, current_combo, enemy_combo)
        self.id_to_name.update(snapshot.get("names") or {})

        self.header.config(text="遊戲中 · 5v5", fg=FG)
        self.subheader.config(text="選角已結束：顯示目前對戰的預估勝率、隊伍評分與快速出裝。")
        self._clear_body()

        active = snapshot.get("active_champion_id")
        if active and self.quick_source is not None:
            self._render_quick_picks(self.body, int(active))

        matchup_prob = float(snapshot.get("matchup_prob", float("nan")))
        current_prob = matchup_prob
        if math.isnan(current_prob):
            current_prob = (
                current_combo.win_prob
                if current_combo is not None and not math.isnan(current_combo.win_prob)
                else float("nan")
            )

        columns = tk.Frame(self.body, bg=BG)
        columns.pack(fill="both", expand=True)
        columns.grid_columnconfigure(0, weight=1, uniform="ingame")
        columns.grid_columnconfigure(1, weight=1, uniform="ingame")

        left, _, left_inner = self._panel(columns, "我方")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        right, _, right_inner = self._panel(columns, "敵方")
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        self._render_score_line(left_inner, "5v5 預估勝率", current_prob, SURFACE)
        if current_combo is not None and getattr(current_combo, "ratings", None):
            self._render_team_ratings(
                left_inner,
                current_combo.ratings,
                getattr(current_combo, "ad_share", float("nan")),
            )
        enemy_prob = float("nan") if math.isnan(current_prob) else 1 - current_prob
        self._render_score_line(right_inner, "敵方預估勝率", enemy_prob, SURFACE)
        if enemy_combo is not None and getattr(enemy_combo, "ratings", None):
            self._render_team_ratings(
                right_inner, enemy_combo.ratings, getattr(enemy_combo, "ad_share", float("nan")),
            )

        self._render_live_team(
            left_inner,
            snapshot.get("my_team_ids") or [],
            active_champion_id=snapshot.get("active_champion_id"),
        )
        self._render_live_team(
            right_inner,
            snapshot.get("enemy_team_ids") or [],
        )
        self._grow_to_fit()

    def _grow_to_fit(self) -> None:
        """Make the window taller when the in-game view no longer fits."""
        self.root.update_idletasks()
        room = self.root.winfo_screenheight() - self.root.winfo_rooty() - 60  # keep clear of the taskbar
        needed = min(self.root.winfo_reqheight(), room)
        if needed > self.root.winfo_height():
            self.root.geometry(f"{self.root.winfo_width()}x{needed}")

    # ----- In-game quick picks (same content as the site's quick overview) -----

    def _request_quick_picks(self, champion_id: int) -> None:
        """Load the champion's shard and icons off the Tk thread."""
        if champion_id in self._quick_state:
            return
        self._quick_state[champion_id] = "loading"
        source, q = self.quick_source, self.q

        def work() -> None:
            try:
                ok = source.warm(champion_id)
            except Exception:
                ok = False
            q.put(("quickpicks", champion_id, ok))

        threading.Thread(target=work, daemon=True).start()

    def _asset_photo(self, ref: str, target_px: int) -> tk.PhotoImage | None:
        """PhotoImage for an already-downloaded icon; never touches the network."""
        key = (ref, target_px)
        if key in self._asset_photos:
            return self._asset_photos[key]
        path = self.quick_source.icon_path(ref, fetch=False) if ref else None
        if path is None:
            return None
        try:
            photo = tk.PhotoImage(file=str(path))
            factor = max(1, round(photo.width() / target_px))
            if factor > 1:
                photo = photo.subsample(factor, factor)
        except tk.TclError:
            return None
        self._asset_photos[key] = photo
        return photo

    def _asset_icon(
        self, parent: tk.Frame, ref: str, target_px: int, bg: str, ring: str | None = None,
    ) -> tk.Frame:
        """Icon in a fixed square; `ring` draws the 2px outline core items get."""
        side = target_px + 4
        holder = tk.Frame(parent, bg=ring or bg, width=side, height=side)
        holder.pack_propagate(False)
        photo = self._asset_photo(ref, target_px)
        lbl = tk.Label(holder, bg=bg, bd=0)
        if photo is not None:
            lbl.config(image=photo)
            lbl.image = photo  # type: ignore[attr-defined]
        lbl.pack(fill="both", expand=True, padx=2, pady=2)
        return holder

    def _select_quick_route(self, champion_id: int, route: int) -> None:
        self._quick_route[champion_id] = route
        if self._last_ingame_args is not None:
            self._render_ingame(*self._last_ingame_args)

    def _render_quick_picks(self, parent: tk.Frame, champion_id: int) -> None:
        self._request_quick_picks(champion_id)
        small = (FONT_SUB[0], 8)

        panel = tk.Frame(parent, bg=SURFACE, highlightthickness=1,
                         highlightbackground=EDGE, highlightcolor=EDGE)
        panel.pack(fill="x", pady=(0, 12))
        inner = tk.Frame(panel, bg=SURFACE)
        inner.pack(fill="x", padx=12, pady=(8, 10))

        champ = (self.quick_source.payload.get("champs") or {}).get(str(champion_id)) or {}
        name = self.id_to_name.get(champion_id) or champ.get("name_zh") or f"#{champion_id}"
        tk.Label(
            inner, text=f"快速推薦 · {name}", bg=SURFACE, fg=GOLD,
            font=self._font(FONT_NAME_B), anchor="w",
        ).pack(fill="x")

        def message(text: str) -> None:
            tk.Label(
                inner, text=text, bg=SURFACE, fg=DIM, font=self._font(FONT_SUB), anchor="w",
            ).pack(fill="x", pady=(4, 0))

        state = self._quick_state.get(champion_id)
        if state == "loading":
            message("載入快速推薦…")
            return
        route = self._quick_route.get(champion_id, 0)
        overview = self.quick_source.overview(champion_id, route) if state == "ready" else None
        if overview is None or not overview.items:
            message("暫時拿不到這隻英雄的快速推薦資料。")
            return

        def section(host: tk.Frame, text: str) -> None:
            tk.Label(
                host, text=text, bg=SURFACE, fg=DIM, font=self._font(FONT_SECTION), anchor="w",
            ).pack(fill="x", pady=(0, 4))

        if len(overview.routes) > 1:
            # Tk has no flow layout: start a new line when the next button
            # would run past the panel edge instead of letting it clip.
            self.root.update_idletasks()
            room = max(parent.winfo_width(), self.root.winfo_width(), 400) - 48
            measure = tkfont.Font(font=self._font(FONT_SUB)).measure
            line, used = None, 0
            for index, option in enumerate(overview.routes):
                selected = index == overview.route
                text = option.label
                if option.pick and option.pick > 0:
                    text += f"  選用 {option.pick:.0%}"
                width = measure(text) + 28  # button padding + gap
                if line is None or used + width > room:
                    gap = 6 if line is None else 4
                    line = tk.Frame(inner, bg=SURFACE)
                    line.pack(fill="x", pady=(gap, 0))
                    used = 0
                used += width
                tk.Button(
                    line, text=text,
                    command=lambda r=index: self._select_quick_route(champion_id, r),
                    bg=DIVIDER if selected else ROW,
                    fg=FG if selected else DIM,
                    activebackground=DIVIDER, activeforeground=FG,
                    relief="flat", bd=0, padx=8, pady=3, cursor="hand2",
                    font=self._font(FONT_SUB),
                ).pack(side="left", padx=(0, 6))

        loadout = tk.Frame(inner, bg=SURFACE)
        loadout.pack(fill="x", pady=(10, 0))
        loadout.grid_columnconfigure(0, weight=1)

        build = tk.Frame(loadout, bg=SURFACE)
        build.grid(row=0, column=0, sticky="nw")
        section(build, "推薦出裝" if overview.complete else "推薦核心")
        build_row = tk.Frame(build, bg=SURFACE)
        build_row.pack(fill="x")
        items = tk.Frame(build_row, bg=SURFACE)
        items.pack(side="left", anchor="n")
        for column, item in enumerate(overview.items):
            items.grid_columnconfigure(column, minsize=72)
            cell = tk.Frame(items, bg=SURFACE)
            cell.grid(row=0, column=column, sticky="n")
            ring = GOLD if item.is_core else None
            self._asset_icon(cell, item.icon, 32, SURFACE, ring=ring).pack()
            tk.Label(
                cell, text=item.name, bg=SURFACE, fg=FG, font=self._font(small),
                wraplength=68, justify="center",
            ).pack()
            if item.is_core:
                tk.Label(cell, text="核心", bg=SURFACE, fg=GOLD, font=self._font(small)).pack()

        if overview.core_wr is not None:
            rate = tk.Frame(build_row, bg=SURFACE)
            rate.pack(side="left", anchor="n", padx=(14, 0))
            tk.Label(
                rate, text="預估勝率", bg=SURFACE, fg=DIM, font=self._font(small), anchor="w",
            ).pack(fill="x")
            tk.Label(
                rate, text=f"{overview.core_wr:.1%}", bg=SURFACE, fg=FG,
                font=self._font(FONT_SCORE), anchor="w",
            ).pack(fill="x")
            basis = "核心三件" if overview.core_size == 3 else "核心兩件"
            tk.Label(
                rate, text=f"{basis} · {overview.core_games:,} 場", bg=SURFACE, fg=DIM,
                font=self._font(small), anchor="w",
            ).pack(fill="x")

        note = (
            f"核心後為常見搭配 · {overview.completion_games:,} 場"
            if overview.complete else "樣本不足，僅顯示核心"
        )
        tk.Label(
            build, text=note, bg=SURFACE, fg=MUTED, font=self._font(small), anchor="w",
        ).pack(fill="x", pady=(4, 0))

        utilities = tk.Frame(loadout, bg=SURFACE)
        utilities.grid(row=0, column=1, sticky="ne", padx=(16, 0))
        section(utilities, "鞋子")
        boot_row = tk.Frame(utilities, bg=SURFACE)
        boot_row.pack(fill="x")
        if overview.boot is not None:
            self._asset_icon(boot_row, overview.boot.icon, 32, SURFACE).pack(side="left")
            boot_text = tk.Frame(boot_row, bg=SURFACE)
            boot_text.pack(side="left", padx=(6, 0))
            tk.Label(
                boot_text, text=overview.boot.name, bg=SURFACE, fg=FG,
                font=self._font(FONT_SUB), anchor="w",
            ).pack(fill="x")
            if overview.boot_wr is not None and overview.boot_games > 0:
                tk.Label(
                    boot_text,
                    text=f"勝率 {overview.boot_wr:.1%} · {overview.boot_games:,} 場",
                    bg=SURFACE, fg=DIM, font=self._font(small), anchor="w",
                ).pack(fill="x")
        else:
            tk.Label(
                boot_row, text="樣本不足", bg=SURFACE, fg=MUTED, font=self._font(small), anchor="w",
            ).pack(fill="x")

        if overview.spells:
            tk.Frame(utilities, bg=SURFACE, height=8).pack()
            section(utilities, "召喚師技能")
            spell_row = tk.Frame(utilities, bg=SURFACE)
            spell_row.pack(fill="x")
            for spell in overview.spells:
                self._asset_icon(spell_row, spell.icon, 32, SURFACE).pack(side="left", padx=(0, 4))
            tk.Label(
                spell_row, text=" + ".join(spell.name for spell in overview.spells),
                bg=SURFACE, fg=FG, font=self._font(FONT_SUB), anchor="w",
            ).pack(side="left", padx=(2, 0))

        augments = tk.Frame(inner, bg=SURFACE)
        augments.pack(fill="x", pady=(12, 0))
        tiers = (
            ("kSilver", "銀色", "#c3ccd6"),
            ("kGold", "金色", GOLD),
            ("kPrismatic", "彩色", "#c79bff"),
        )
        for column, (rarity, label, color) in enumerate(tiers):
            augments.grid_columnconfigure(column, weight=1, uniform="quick-aug")
            host = tk.Frame(augments, bg=SURFACE)
            host.grid(row=0, column=column, sticky="new", padx=(0 if column == 0 else 12, 0))
            head = tk.Frame(host, bg=SURFACE)
            head.pack(fill="x", pady=(0, 4))
            tk.Label(
                head, text=label, bg=SURFACE, fg=color, font=self._font(FONT_SECTION),
            ).pack(side="left")
            tk.Label(
                head, text="TOP 3", bg=SURFACE, fg=MUTED, font=self._font(FONT_SECTION),
            ).pack(side="right")
            rows = overview.augments.get(rarity) or ()
            if not rows:
                tk.Label(
                    host, text="樣本不足", bg=SURFACE, fg=MUTED, font=self._font(small), anchor="w",
                ).pack(fill="x")
            for aug in rows:
                row = tk.Frame(host, bg=ROW)
                row.pack(fill="x", pady=1)
                # Augment art is drawn for a black tile; other surfaces wash it out.
                self._asset_icon(row, aug.icon, 36, "#000000").pack(side="left")
                tk.Label(
                    row, text=f"{aug.wr:.1%}", bg=ROW, fg=FG, font=self._font(FONT_NUM_B),
                ).pack(side="right", padx=(4, 8))
                tk.Label(
                    row, text=aug.name, bg=ROW, fg=FG, font=self._font(FONT_SUB), anchor="w",
                ).pack(side="left", fill="x", expand=True, padx=(8, 0))

    def _render_live_team(
        self,
        parent: tk.Frame,
        champion_ids,
        active_champion_id: int | None = None,
    ) -> None:
        roster = tk.Frame(parent, bg=SURFACE)
        roster.pack(fill="x", pady=(10, 0))
        for cid in champion_ids:
            is_me = cid == active_champion_id
            row_bg = ROW_HI if is_me else SURFACE
            row = tk.Frame(roster, bg=row_bg)
            row.pack(fill="x", pady=1, ipady=1)
            self._configure_team_row(row)
            self._icon_cell(row, int(cid), bg=row_bg)

            name = self.id_to_name.get(int(cid), f"#{cid}")
            tk.Label(
                row, text=f"{name}　你" if is_me else name,
                bg=row_bg,
                fg=GOLD if is_me else FG,
                font=self._font(FONT_NAME_B if is_me else FONT_NAME),
                anchor="w",
            ).grid(row=0, column=1, sticky="w")

    def _configure_team_row(self, row: tk.Frame) -> None:
        """Team rows only need icon + name (no Δ / no z column for teammates)."""
        row.grid_columnconfigure(0, minsize=self.COL_ICON)
        row.grid_columnconfigure(1, weight=1)

    def _configure_bench_row(self, row: tk.Frame) -> None:
        """Bench rows: icon + name + delta + prob + z (numbers right-aligned)."""
        row.grid_columnconfigure(0, minsize=self.COL_ICON)
        row.grid_columnconfigure(1, weight=1)
        row.grid_columnconfigure(2, minsize=self.COL_DELTA)
        row.grid_columnconfigure(3, minsize=self.COL_PROB)
        row.grid_columnconfigure(4, minsize=self.COL_Z)

    def _render_team_section(self, parent, parsed, suggestions, current_combo=None) -> None:
        """Show all 5 blue-team champions in the left column.

        Teammates are dimmed (you can't swap them, they're context).  Your
        own row gets a gold name + ⊙ marker and the z-score inline so the
        user always knows their current meta strength as an anchor for
        comparing the bench candidates on the right.
        """
        keep = next((s for s in suggestions if s.source == "keep" and s.is_known), None)
        own_z = keep.z_score if keep is not None else None
        current_prob = (
            current_combo.win_prob
            if current_combo is not None and not math.isnan(current_combo.win_prob)
            else keep.win_prob if keep is not None else float("nan")
        )

        self._render_score_line(parent, "目前預估勝率", current_prob, SURFACE)

        if current_combo is not None and getattr(current_combo, "ratings", None):
            self._render_team_ratings(parent, current_combo.ratings,
                                      getattr(current_combo, "ad_share", float("nan")), bg=SURFACE)

        # Teammates are context, not choices: one portrait strip (same idiom
        # as the combo rows) with your own pick ringed in gold, then a single
        # line naming your champion and its z anchor.
        roster = tk.Frame(parent, bg=SURFACE)
        roster.pack(fill="x", pady=(10, 0))
        for cid in parsed.my_team_ids:
            is_me = cid == parsed.my_current_id
            ring = tk.Frame(roster, bg=GOLD if is_me else SURFACE)
            ring.pack(side="left", padx=(0, 4))
            self._packed_icon(ring, int(cid), bg=SURFACE, padx=2)
        me = self.id_to_name.get(parsed.my_current_id, f"#{parsed.my_current_id}")
        line = tk.Frame(parent, bg=SURFACE)
        line.pack(fill="x", pady=(5, 0))
        tk.Label(line, text=f"你：{me}", bg=SURFACE, fg=GOLD, font=self._font(FONT_NAME_B),
                 anchor="w").pack(side="left")
        if own_z is not None:
            tk.Label(line, text=f"z {_fmt_signed_z(own_z)}", bg=SURFACE, fg=self._z_color(own_z),
                     font=self._font(FONT_NUM)).pack(side="left", padx=(8, 0))

    def _render_score_line(self, parent: tk.Frame, label: str, prob: float, bg: str) -> None:
        """Headline probability: small caption over a large figure."""
        box = tk.Frame(parent, bg=bg)
        box.pack(fill="x", pady=(0, 2))
        tk.Label(box, text=_fmt_prob(prob), bg=bg, fg=FG, font=self._font(FONT_SCORE),
                 anchor="w").pack(side="left")
        tk.Label(box, text=label, bg=bg, fg=MUTED, font=self._font(FONT_SECTION),
                 anchor="sw").pack(side="left", padx=(10, 0), pady=(0, 7), fill="y")

    def _scroll_area(self, parent, bg):
        """Fill the rest of *parent* with a wheel-scrollable frame.

        No chrome scrollbar: a 3px gold-dim thumb appears on the right only
        when the content overflows, so a long bench never gets clipped."""
        wrap = tk.Frame(parent, bg=bg)
        wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(wrap, bg=bg, highlightthickness=0, bd=0)
        thumb = tk.Canvas(wrap, bg=bg, width=3, highlightthickness=0, bd=0)
        thumb.pack(side="right", fill="y", padx=(4, 0))
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=bg)
        win = canvas.create_window(0, 0, window=inner, anchor="nw")

        def sync(_event=None):
            canvas.configure(scrollregion=(0, 0, inner.winfo_reqwidth(), inner.winfo_reqheight()))
            canvas.itemconfigure(win, width=canvas.winfo_width())
            thumb.delete("all")
            lo, hi = canvas.yview()
            if hi - lo < 0.999:
                h = thumb.winfo_height()
                thumb.create_rectangle(0, lo * h, 3, hi * h, fill=GOLD_DIM, width=0)

        def wheel(event):
            if canvas.yview() != (0.0, 1.0):
                canvas.yview_scroll(int(-event.delta / 120), "units")
                sync()
            return "break"

        def bind_tree(widget):
            widget.bind("<MouseWheel>", wheel, add="+")
            for child in widget.winfo_children():
                bind_tree(child)

        canvas.configure(yscrollincrement=20)
        inner.bind("<Configure>", lambda e: (sync(), bind_tree(inner)))
        canvas.bind("<Configure>", sync)
        canvas.bind("<MouseWheel>", wheel)
        return inner

    def _render_bench_table(self, head, parent, suggestions) -> None:
        bench = [s for s in suggestions if s.source == "bench"]

        bench_copy_btn = self._ghost_button(
            head, f"{COPY_ICON} 複製前 7",
            lambda pool=suggestions: self._copy_bench_top(pool, bench_copy_btn),
        )
        bench_copy_btn._idle_text = f"{COPY_ICON} 複製前 7"  # type: ignore[attr-defined]
        bench_copy_btn.pack(side="right")

        if not bench:
            tk.Label(parent, text="替補池目前是空的。", bg=SURFACE, fg=MUTED,
                     font=self._font(FONT_SUB), anchor="w").pack(fill="x")
            return

        # Name first (what you scan for), numbers right-aligned after it.
        header = tk.Frame(parent, bg=SURFACE)
        header.pack(fill="x", pady=(0, 3))
        self._configure_bench_row(header)
        for col, text in ((1, "候選"), (2, "MLΔ"), (3, "換後"), (4, "z")):
            self._cell(header, col, text, MUTED, bg=SURFACE, font=FONT_SECTION, anchor="w" if col == 1 else "e")

        best_idx = next((i for i, s in enumerate(bench) if s.is_known), None)
        parent = self._scroll_area(parent, SURFACE)

        for i, s in enumerate(bench):
            is_best = i == best_idx
            if is_best and s.is_known:
                row_bg = BEST_BG if s.delta > 0 else WARN_BG
            else:
                row_bg = ROW

            name = self.id_to_name.get(s.champion_id, f"#{s.champion_id}")
            row = tk.Frame(parent, bg=row_bg)
            row.pack(fill="x", pady=1, ipady=2)
            self._configure_bench_row(row)
            self._icon_cell(row, s.champion_id, bg=row_bg)

            if not s.is_known:
                self._cell(row, 1, f"{name}（無資料）", MUTED, bg=row_bg, font=FONT_NAME)
                for col in (2, 3, 4):
                    self._cell(row, col, "—", MUTED, bg=row_bg, font=FONT_NUM, anchor="e")
                continue

            delta_pp = s.delta * 100
            delta_font = FONT_NUM_BEST if is_best else FONT_NUM_B
            name_color = (GREEN if s.delta > 0 else RED) if is_best else FG

            name_cell = tk.Frame(row, bg=row_bg)
            name_cell.grid(row=0, column=1, sticky="w")
            tk.Label(name_cell, text=name, bg=row_bg, fg=name_color,
                     font=self._font(FONT_NAME_B if is_best else FONT_NAME), anchor="w").pack(side="left")
            if is_best:
                tk.Label(name_cell, text="首選", bg=row_bg, fg=name_color,
                         font=self._font(FONT_SECTION)).pack(side="left", padx=(6, 0))
            self._cell(row, 2, _fmt_signed_pct(delta_pp), self._delta_color(s.delta), bg=row_bg,
                       font=delta_font, anchor="e")
            self._cell(row, 3, _fmt_prob(s.win_prob), FG, bg=row_bg, font=FONT_NUM, anchor="e")
            self._cell(row, 4, _fmt_signed_z(s.z_score), self._z_color(s.z_score), bg=row_bg,
                       font=FONT_NUM, anchor="e")

    def _render_combo_section(self, head, parent, combos, pool=None, owned_ids=None) -> None:
        if not combos:
            return

        copy_btn = self._ghost_button(
            head, f"{COPY_ICON} 複製推薦",
            lambda c=combos[0], p=pool, o=owned_ids: self._copy_combo(c, p, o, copy_btn),
        )
        copy_btn._idle_text = f"{COPY_ICON} 複製推薦"  # type: ignore[attr-defined]
        copy_btn.pack(side="right")

        # Every rank gets the same portrait strip, so the three options can be
        # compared at a glance; rank 1 is lifted, not re-drawn differently.
        for combo in combos:
            best = combo.rank == 1
            row_bg = ROW_HI if best else ROW
            row = tk.Frame(parent, bg=row_bg)
            row.pack(fill="x", pady=(0, 3), ipady=3)
            row.grid_columnconfigure(0, minsize=40)
            row.grid_columnconfigure(1, weight=1)
            row.grid_columnconfigure(2, minsize=74)
            row.grid_columnconfigure(3, minsize=78)

            tk.Label(
                row, text=str(combo.rank), bg=row_bg,
                fg=GOLD if best else MUTED,
                font=self._font(FONT_NUM_BEST if best else FONT_NUM_B),
            ).grid(row=0, column=0)

            self._combo_icon_strip(row, combo.champion_ids, row_bg)

            tk.Label(
                row, text=_fmt_prob(combo.win_prob), bg=row_bg, fg=FG,
                font=self._font(FONT_NUM_BEST if best else FONT_NUM_B), anchor="e",
            ).grid(row=0, column=2, sticky="e", padx=(0, 4))

            tk.Label(
                row, text=_fmt_signed_pct(combo.delta * 100), bg=row_bg,
                fg=self._delta_color(combo.delta), font=self._font(FONT_NUM),
                anchor="e",
            ).grid(row=0, column=3, sticky="e", padx=(0, 10))

    def _combo_icon_strip(self, parent: tk.Frame, champion_ids, bg: str) -> None:
        strip = tk.Frame(parent, bg=bg)
        strip.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=1)
        for cid in champion_ids:
            self._packed_icon(strip, int(cid), bg=bg)

    def _render_team_ratings(self, parent, ratings, ad_share: float = float("nan"), bg: str = SURFACE) -> None:
        """Strengths as equal stat tiles (caption over grade); the risk line
        gets its own full-width strip so its long label is never clipped."""
        ratings = list(ratings[:5])
        risks = [r for r in ratings if r.label.startswith(("高風險", "風險", "低點"))]
        tiles = [r for r in ratings if r not in risks]

        grid = tk.Frame(parent, bg=bg)
        grid.pack(fill="x", pady=(6, 0))
        for col in range(max(len(tiles), 1)):
            grid.grid_columnconfigure(col, weight=1, uniform="team_rating")
        for col, rating in enumerate(tiles):
            tile = tk.Frame(grid, bg=ROW)
            tile.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 4, 0))
            tk.Label(tile, text=rating.label, bg=ROW, fg=MUTED, font=self._font(FONT_SECTION),
                     anchor="w").pack(fill="x", padx=8, pady=(5, 0))
            # Grade on its own line, detail (e.g. "82%", "3前排") under it:
            # side by side they clipped in a narrow tile.
            tk.Label(tile, text=_fmt_rating(rating.value), bg=ROW,
                     fg=self._rating_color(rating.value), font=self._font(FONT_NUM_BEST),
                     anchor="w").pack(fill="x", padx=8)
            tk.Label(tile, text=rating.detail or " ", bg=ROW, fg=MUTED, font=self._font(FONT_SECTION),
                     anchor="w").pack(fill="x", padx=8, pady=(0, 5))

        for rating in risks:
            severe = rating.label.startswith(("高風險", "風險"))
            strip_bg = WARN_BG if severe else ROW
            strip = tk.Frame(parent, bg=strip_bg)
            strip.pack(fill="x", pady=(4, 0))
            tk.Frame(strip, bg=RED if severe else GOLD_DIM, width=3).pack(side="left", fill="y")
            tk.Label(strip, text=rating.label, bg=strip_bg, fg=RED if severe else DIM,
                     font=self._font(FONT_SUB), anchor="w").pack(side="left", padx=(8, 6), pady=4)
            tk.Label(strip, text=_rating_value_text(rating), bg=strip_bg,
                     fg=self._rating_color(rating.value), font=self._font(FONT_NUM_B)).pack(side="left")
            if rating.detail:
                tk.Label(strip, text=rating.detail, bg=strip_bg, fg=MUTED, font=self._font(FONT_SECTION),
                         anchor="e").pack(side="right", padx=(6, 8))

    def _combo_substitute_ids(self, recommended_combo, pool, count: int = 2) -> list[int]:
        """Champion ids for the top `count` available-pool picks not in the best-5.

        Ranked by team fit (Suggestion.win_prob), the same order the bench
        table shows, so the shared substitutes are the next picks to reach for
        if one of the recommended five isn't available.
        """
        if not pool:
            return []
        chosen = set(recommended_combo.champion_ids)
        known = [
            s for s in pool
            if s.is_known and not math.isnan(s.win_prob) and s.champion_id not in chosen
        ]
        known.sort(key=lambda s: s.win_prob, reverse=True)
        return [int(s.champion_id) for s in known[:count]]

    def _combo_name(self, champion_id: int, owned: set[int]) -> str:
        """Champion display name, tagged （已有）if the team already has it.

        owned is the current team's picks (parsed.my_team_ids): a recommended
        champion in it is already locked in, an untagged one must be grabbed
        from the shared bench.
        """
        name = self.id_to_name.get(champion_id, f"#{champion_id}")
        return f"{name}（已有）" if int(champion_id) in owned else name

    def _combo_clipboard_text(self, recommended_combo, pool=None, owned_ids=None) -> str:
        owned = {int(c) for c in (owned_ids or [])}
        starters = [self._combo_name(cid, owned) for cid in recommended_combo.champion_ids]
        win_line = f"✅勝率{_fmt_prob(recommended_combo.win_prob)}"
        if not math.isnan(recommended_combo.delta):
            win_line += f"（{_fmt_signed_pct(recommended_combo.delta * 100)}）"
        lines = ["【AI推薦隊伍】", *starters, win_line]
        sub_ids = self._combo_substitute_ids(recommended_combo, pool)
        if sub_ids:
            lines += ["【替補】", *[self._combo_name(cid, owned) for cid in sub_ids]]
        return "\n".join(lines)

    def _copy_combo(self, recommended_combo, pool=None, owned_ids=None, button: tk.Button | None = None) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self._combo_clipboard_text(recommended_combo, pool, owned_ids))
        self.root.update_idletasks()
        if button is not None:
            button.config(text=f"{COPIED_ICON} 已複製", fg=GREEN)
            self.root.after(
                1200,
                lambda: button.winfo_exists() and button.config(text=getattr(button, "_idle_text", COPY_ICON), fg=DIM),
            )

    def _champ_meta_wr(self, champion_id: int) -> float | None:
        """Champion's real global meta win rate from the tier-list payload.

        This is the same number the public tier list shows (bayes-smoothed,
        matching its headline), and it is cell-independent — it does NOT
        depend on the local player's current team.  Returns None if the
        champion isn't in the payload (too few games / not visible).

        Contrast with Suggestion.win_prob, which is the local team's predicted
        win probability with the candidate swapped into *your* cell: that is
        shifted by your team's baseline and only valid for your slot, so it is
        meaningless to share with teammates.
        """
        champs = getattr(self.augment_advisor, "champs", {})
        champ = champs.get(int(champion_id))
        if not champ:
            return None
        wr = champ.get("wr")
        try:
            return float(wr) if wr is not None else None
        except (TypeError, ValueError):
            return None

    def _bench_top_clipboard_text(self, pool) -> str:
        """Top 7 of the available pool for sharing with teammates.

        Ordered by team fit (Suggestion.win_prob — the same order as the
        bench table's MLΔ column), but each champion is annotated with its
        real global meta win rate (_champ_meta_wr), not the baseline-shifted
        per-cell win_prob.  The shared number is therefore cell-independent
        and matches the public tier list, so a teammate eyeing the shared
        reroll bench gets a trustworthy "is this champ strong right now"
        signal instead of a number that only applies to your slot.
        """
        known = [s for s in pool if s.is_known and not math.isnan(s.win_prob)]
        known.sort(key=lambda s: s.win_prob, reverse=True)
        top = known[:7]
        if not top:
            return "可用池前7名：n/a"
        lines = []
        for i, s in enumerate(top, start=1):
            name = self.id_to_name.get(s.champion_id, f"#{s.champion_id}")
            wr = self._champ_meta_wr(s.champion_id)
            wr_text = _fmt_prob(wr) if wr is not None else "—"
            lines.append(f"{i}. {name} {wr_text}")
        header = "可用池前7名（依對本隊提升排序，% 為該英雄 meta 勝率）："
        return header + "\n" + "\n".join(lines)

    def _copy_bench_top(self, pool, button: tk.Button | None = None) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self._bench_top_clipboard_text(pool))
        self.root.update_idletasks()
        if button is not None:
            button.config(text=f"{COPIED_ICON} 已複製", fg=GREEN)
            self.root.after(
                1200,
                lambda: button.winfo_exists() and button.config(text=getattr(button, "_idle_text", COPY_ICON), fg=DIM),
            )

    def _icon_cell(self, parent: tk.Frame, champion_id: int, bg: str = BG) -> None:
        """Place the champion icon in column 0 of `parent`.

        bg matches the parent row's background so the icon's surrounding
        pixels blend on tinted (best-pick) rows.  Falls back to a hollow
        placeholder Label of the same width if the IconCache can't produce
        a PhotoImage, so row alignment stays stable.
        """
        photo = self.icon_cache.get(champion_id) if self.icon_cache else None
        if photo is not None:
            lbl = tk.Label(parent, image=photo, bg=bg, bd=0)
            # Hold the reference on the widget too - Tk doesn't keep it, and
            # the redundancy is cheap and removes a class of GC bugs.
            lbl.image = photo  # type: ignore[attr-defined]
            lbl.grid(row=0, column=0, padx=(0, 6))
        else:
            tk.Label(
                parent, text="", bg=bg, width=4, height=2,
            ).grid(row=0, column=0, padx=(0, 6))

    def _packed_icon(self, parent: tk.Frame, champion_id: int, bg: str = BG, padx=(0, 6)) -> None:
        photo = self.icon_cache.get(champion_id) if self.icon_cache else None
        if photo is not None:
            lbl = tk.Label(parent, image=photo, bg=bg, bd=0)
            lbl.image = photo  # type: ignore[attr-defined]
            lbl.pack(side="left", padx=padx, pady=padx if isinstance(padx, int) else 0)
        else:
            tk.Label(parent, text="", bg=bg, width=4, height=2).pack(
                side="left", padx=padx, pady=padx if isinstance(padx, int) else 0)

    @staticmethod
    def _delta_color(value: float) -> str:
        if math.isnan(value):
            return MUTED
        if value > 0:
            return GREEN
        if value < 0:
            return RED
        return DIM

    @staticmethod
    def _z_color(value: float) -> str:
        if math.isnan(value):
            return MUTED
        if value > 0.5:
            return GREEN
        if value < -0.5:
            return RED
        return FG

    @staticmethod
    def _rating_color(value: float) -> str:
        if math.isnan(value):
            return MUTED
        if value >= 4.0:
            return GREEN
        if value < 2.0:
            return RED
        if value < 3.0:
            return GOLD
        return FG

    def _cell(
        self, parent: tk.Frame, col: int, text: str, fg: str,
        bg: str = BG, font: tuple = FONT_NUM, anchor: str = "w",
    ) -> None:
        """Place a left-aligned label at `col` in the row's shared grid.

        Width is no longer passed explicitly: column widths come from
        the row's grid_columnconfigure(minsize=...) so every row pins
        to the same x positions regardless of which font the content
        is set in.
        """
        tk.Label(
            parent, text=text, bg=bg, fg=fg,
            font=self._font(font), anchor=anchor,
        ).grid(row=0, column=col, sticky=anchor, padx=(0, 10) if anchor == "e" and col == 4 else 0)


# ---------- Entry point ----------

@click.command()
@click.option("--lr-model", default=None,
              type=click.Path(path_type=Path, dir_okay=False),
              help="Path to lr_model.pkl (sklearn LR pickle, loaded without sklearn) or lr_weights.json.")
@click.option("--vocab", default=None,
              type=click.Path(path_type=Path, dir_okay=False),
              help="Path to tier2_checkpoint.pt or champ_to_idx.json - used for champion vocab.")
@click.option("--pair-stats", default=None,
              type=click.Path(path_type=Path, dir_okay=False),
              help="Path to synergy JSON: role-pooled from build_role_synergy.py (default) "
                   "or legacy pair from build_pair_stats.py. Schema is auto-detected.")
@click.option("--composition-model", default=None,
              type=click.Path(path_type=Path, dir_okay=True),
              help="Path to composition LR model.pkl or its model directory. Used for primary ML swap deltas.")
@click.option("--poll-interval", default=0.35, show_default=True, type=float,
              help="Seconds between LCU polls while in ChampSelect — drives how fast "
                   "the overlay reacts to a bench reroll. LCU is local (127.0.0.1) so "
                   "polling is cheap, and the recompute only fires on a real state change. "
                   "Idle / in-game / reconnect paths stay floored at 2.0s regardless.")
@click.option("--fake", is_flag=True, default=False,
              help="Demo mode: skip LCU, generate random champ-select states every 3s. "
                   "Useful to verify the GUI works without launching League.")
@click.option("--fake-ingame", is_flag=True, default=False,
              help="With --fake: show one random in-game 5v5 state instead of champ select.")
@click.option("--verbose", is_flag=True, default=False,
              help="Print per-poll status (phase + session presence) to stdout. "
                   "Useful for diagnosing why a champ-select isn't being detected.")
@click.option("--no-update", is_flag=True, help="Skip online updates for this launch.")
@click.option("--self-test", is_flag=True, help="Check packaged models and Tk; save diagnostics and exit.")
def main(
    lr_model: Path | None,
    vocab: Path | None,
    pair_stats: Path | None,
    composition_model: Path | None,
    poll_interval: float,
    fake: bool,
    fake_ingame: bool,
    verbose: bool,
    no_update: bool,
    self_test: bool,
) -> None:
    """Tk GUI for the ARAM champ-select recommender."""
    _enable_windows_dpi_awareness()
    _set_app_user_model_id()
    global DESKTOP_DATA_DIR, DESKTOP_STATUS
    if getattr(sys, "frozen", False):
        from aram_nn.desktop.startup import bundled_version, launch_updated, run_updates
        from aram_nn.desktop.update import read_json, validate_data
        resource_root = Path(sys._MEIPASS)
        version = bundled_version(resource_root)
        if not no_update:
            updated = run_updates(version)
            if updated.app_path:
                try:
                    child = launch_updated(updated.app_path)
                    if self_test:
                        raise SystemExit(child.wait(timeout=240))
                    return
                except OSError:
                    updated.message = "新程式啟動失敗，保留目前可用版本"
            DESKTOP_DATA_DIR = updated.data_dir
            DESKTOP_STATUS = updated.message
        metadata = validate_data(DESKTOP_DATA_DIR or resource_root / "desktop-data")
        cutoff = datetime.fromisoformat(metadata['time_cutoff']).astimezone().strftime("%Y-%m-%d %H:%M %Z")
        DESKTOP_STATUS = (f"Mayhem · {metadata['region']} · patch {metadata['current_patch']} · "
                          f"模型涵蓋 {','.join(metadata['patches'])} · {metadata['row_count']:,} 場 · "
                          f"資料截止 {cutoff} · "
                          f"{DESKTOP_STATUS or '使用內附資料'}")

    lr_model = _resolve_resource(lr_model, DEFAULT_LR_MODEL)
    vocab = _resolve_resource(vocab, DEFAULT_VOCAB)
    pair_stats = _resolve_resource(pair_stats, DEFAULT_SYNERGY_STATS)
    composition_model = _resolve_resource(composition_model, DEFAULT_COMPOSITION_MODEL)

    print(f"[gui] loading model from {lr_model}")
    model = load_lr(lr_model, vocab)
    print(f"[gui] vocab covers {model.n_champs} champions")
    comp_model = None
    if composition_model.exists():
        comp_model = load_composition_lr(composition_model)
        print(
            f"[gui] composition LR features={len(comp_model.feature_names)} "
            f"champions={len(comp_model.champ_to_idx)}"
        )
    else:
        print(f"[gui] WARN: composition model not found at {composition_model}; using old blend score")
    pair_model = None
    if pair_stats.exists():
        pair_model = load_synergy(pair_stats)
        if isinstance(pair_model, RoleSynergyStats):
            print(
                f"[gui] role synergy cells={len(pair_model.rows):,} "
                f"roles={list(pair_model.roles)} champs={len(pair_model.role_by_champ)} "
                f"min_cell={pair_model.min_pair}"
            )
        else:
            print(
                f"[gui] pair synergy rows={len(pair_model.rows):,} "
                f"patch={pair_model.patch_prefix} min_pair={pair_model.min_pair}"
            )
    else:
        print(f"[gui] WARN: synergy stats not found at {pair_stats}; old blend fallback will use LR only")

    if self_test:
        from aram_nn.desktop import webview_host
        from aram_nn.desktop.update import atomic_json, cache_root
        probe = tk.Tk()
        probe.withdraw()
        probe.update()
        sample = sorted(comp_model.champ_to_idx)[:5] if comp_model else []
        probability, unknown = comp_model.predict_team_prob(sample) if comp_model else (None, [])
        atomic_json(cache_root() / "diagnostics.json", {
            "ok": bool(comp_model and pair_model and not unknown and math.isfinite(probability)),
            "app_version": version if getattr(sys, "frozen", False) else "source",
            "python": sys.version.split()[0], "tk": probe.tk.call("info", "patchlevel"),
            "champions": model.n_champs, "data_status": DESKTOP_STATUS,
            "sample_probability": probability, "webview": webview_host.diagnose(),
        })
        probe.destroy()
        return

    q: queue.Queue = queue.Queue()
    stop_event = threading.Event()

    # IconCache works in both modes: prefers LCU (local, fast) when creds
    # are present, otherwise falls back to Riot's Data Dragon CDN.  In
    # --fake without League running, only the CDN path is used; that needs
    # internet but caches to disk so future runs are instant offline.
    creds_for_icons = get_credentials()  # may be None, that's fine
    icon_cache = IconCache(_icon_cache_dir(), lcu_creds=creds_for_icons)
    threading.Thread(target=icon_cache.prefetch_all, daemon=True).start()

    if fake:
        print("[gui] --fake: synthesizing champ-select states every 3s, no LCU needed")
        thread = threading.Thread(
            target=fake_poll_loop, args=(stop_event, q, model, pair_model, comp_model),
            kwargs={"ingame": fake_ingame}, daemon=True,
        )
    else:
        creds = creds_for_icons  # reuse - same credentials work for both
        if not creds:
            print("[gui] no LCU credentials at startup; GUI will keep waiting")
        thread = threading.Thread(
            target=poll_loop,
            args=(stop_event, q, model, pair_model, comp_model, creds, poll_interval, verbose),
            daemon=True,
        )

    thread.start()  # crucial - without this, the poll loop never runs and
                    # the GUI stays on its placeholder "Loading..." header forever.

    root = tk.Tk()
    app = RecommenderApp(root, q, icon_cache=icon_cache)
    try:
        root.mainloop()
    finally:
        try:
            app.close_site()
        except Exception:
            pass
        # Signal the poll thread to exit cleanly so the httpx client closes.
        stop_event.set()


if __name__ == "__main__":
    main()
