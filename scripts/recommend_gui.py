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
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import click

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
LIVE_CLIENT_ALL_GAME_DATA_URL = "https://127.0.0.1:2999/liveclientdata/allgamedata"
OVERWOLF_AUGMENT_EVENT = Path("data/overwolf/latest_augments.json")
OVERWOLF_AUGMENT_LOG = Path("data/overwolf/augment_events.jsonl")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _resource_path(relative: Path | str) -> Path:
    rel = Path(relative)
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)) / rel

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


def load_champion_alias_to_id() -> dict[str, int]:
    """Offline Data Dragon alias -> championId map for Live Client Data."""
    names = load_fallback_champion_names()
    return {alias.lower(): cid for cid, alias in names.items()}


def _norm_key(value: object) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("_", "").replace("-", "")


class AugmentAdvisor:
    """Small in-GUI augment scorer backed by the generated site payload."""

    def __init__(self, payload_path: Path | None = None) -> None:
        self.payload_path = _resolve_resource(payload_path, DEFAULT_TIER_PAYLOAD)
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

        raw_augs = payload.get("augs") or payload.get("augments") or {}
        raw_champs = payload.get("champs") or payload.get("champions") or {}
        self.augments = {int(k): v for k, v in raw_augs.items() if str(k).isdigit()}
        self.champs = {int(k): v for k, v in raw_champs.items() if str(k).isdigit()}

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

    def _champ_rows(self, champion_id: int) -> dict[int, dict]:
        champ = self.champs.get(int(champion_id)) or {}
        rows: dict[int, dict] = {}
        for side in ("top", "bot"):
            by_rarity = champ.get(side) or {}
            if isinstance(by_rarity, dict):
                groups = by_rarity.values()
            else:
                groups = [by_rarity]
            for group in groups:
                for row in group or []:
                    try:
                        rows[int(row.get("id") or row.get("augment_id"))] = row
                    except (TypeError, ValueError):
                        continue
        return rows

    def recommend(self, champion_id: int | None, offered: list[dict]) -> list[dict]:
        if not offered:
            return []
        champ_rows = self._champ_rows(int(champion_id or 0)) if champion_id else {}
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
) -> None:
    """Synthetic poll loop for --fake mode.

    Emits randomly-generated champ-select states every `interval` seconds so
    the GUI can be validated without an LCU connection.  Predictions use the
    real LR model on the random teams, so delta magnitudes match what real
    play would produce - only the champion picks are synthetic.

    Bench size is randomized between 5 and 10 each tick to exercise the
    GUI's vertical scrolling and to match the bench sizes a real ARAM
    queue produces once teammates start rerolling.
    """
    import random

    q.put(("static", load_fallback_champion_names()))
    all_ids = sorted(model.champ_to_idx.keys())
    cell_id = 2

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
BG        = "#0e1116"
SURFACE   = "#161a22"
ROW       = "#11151d"
BEST_BG   = "#202414"
WARN_BG   = "#241817"
FG        = "#e6e8eb"
DIM       = "#9aa0a6"
MUTED     = "#69707a"
DIVIDER   = "#30363d"
GOLD      = "#f5c518"
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
FONT_SCORE    = ("Microsoft JhengHei UI", 18, "bold")
FONT_ICON     = ("Segoe UI Symbol", 12, "bold")
FONT_NUM      = ("Consolas", 11)
FONT_NUM_B    = ("Consolas", 11, "bold")
FONT_NUM_BEST = ("Consolas", 14, "bold")

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


class RecommenderApp:
    def __init__(self, root: tk.Tk, q: queue.Queue, icon_cache: IconCache | None = None) -> None:
        self.root = root
        self.q = q
        self.id_to_name: dict[int, str] = {}
        self.icon_cache = icon_cache
        self.font_scale = 1.0
        self._last_render_args: tuple | None = None
        self.active_champion_id: int | None = None
        self.augment_advisor = AugmentAdvisor()
        self.latest_augment_event: dict | None = None
        self._last_augment_signature: tuple | None = None

        root.title("ARAM Recommender")
        _set_window_icon(root)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 1.0)
        root.geometry("840x620+40+40")
        root.configure(bg=BG)
        root.minsize(740, 520)

        # Pixel-perfect column geometry, applied identically to every row
        # frame so cells line up regardless of font.  Tk widget `width=N`
        # is in font-average chars; mixing FONT_SECTION (Segoe UI
        # proportional) with FONT_NUM (Consolas mono) at the same `width=N`
        # produced visibly different pixel widths in earlier versions.
        # Pinning to minsize fixes it regardless of font.
        self._base_cols = {
            "icon": 42,
            "delta": 74,
            "prob": 64,
            "z": 58,
        }
        self._apply_zoom_geometry()

        # Tk widget constructors only accept a single int for padx/pady
        # (internal padding).  Asymmetric padding goes on the geometry
        # manager call (.pack / .grid).  We use that distinction to set
        # generous outer rhythm without bloating the labels themselves.
        header_row = tk.Frame(root, bg=BG)
        header_row.pack(fill="x", pady=(8, 0))
        header_row.grid_columnconfigure(0, weight=1)
        self.header = tk.Label(
            header_row, text="Loading...",
            bg=BG, fg=FG, font=self._font(FONT_HEAD),
            anchor="w", padx=12,
        )
        self.header.grid(row=0, column=0, sticky="ew")

        self.subheader = tk.Label(
            root, text="",
            bg=BG, fg=DIM, font=self._font(FONT_SUB),
            anchor="w", padx=12,
        )
        self.subheader.pack(fill="x", pady=(0, 8))

        # Thin divider between header and the dynamic body - replaces what
        # a bottom border on the header would do, without violating the
        # absolute ban on accent borders.
        tk.Frame(root, bg=DIVIDER, height=1).pack(fill="x", padx=12)

        self.augment_panel = tk.Frame(root, bg=BG)
        # Augment panel disabled: Overwolf bridge currently can't connect, so the
        # "Best augment" line is stale noise.  Keep the frame attribute alive
        # (referenced by _render_augment_panel / _apply_zoom) but don't pack or
        # populate it.  Re-enable the three lines below once Overwolf is back.
        # self.augment_panel.pack(fill="x", padx=12, pady=(6, 0))
        # self._render_augment_panel()

        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=12, pady=(8, 10))

        _make_observer_window(root)
        self.root.after_idle(lambda: _make_observer_window(root))
        self._bind_zoom_shortcuts()

        # Begin draining the queue.
        self.root.after(100, self._drain)
        # Augment polling disabled while the Overwolf bridge is offline.
        # self.root.after(750, self._poll_augments)

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
        # self._render_augment_panel()  # augment panel disabled (Overwolf offline)
        if self._last_render_args is not None:
            self._render(*self._last_render_args)

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
        if force or signature != self._last_augment_signature:
            self.latest_augment_event = event
            self._last_augment_signature = signature
            self._render_augment_panel()
        if not force:
            self.root.after(1000, self._poll_augments)

    def _force_rescan_augments(self) -> None:
        self._poll_augments(force=True)

    def _render_augment_panel(self) -> None:
        for widget in self.augment_panel.winfo_children():
            widget.destroy()

        event = self.latest_augment_event
        if event is None:
            text = "Augments: waiting for Overwolf"
            fg = DIM
        else:
            event_type = str(event.get("type") or "unknown")
            augments = [aug for aug in event.get("augments") or [] if isinstance(aug, dict)]
            if event_type == "offer":
                rows = self.augment_advisor.recommend(self.active_champion_id, augments)
                if rows:
                    best = rows[0]
                    if best.get("id") is None:
                        text = f"Augments detected: {best.get('raw_name') or best.get('name')} (unmapped)"
                        fg = GOLD
                    elif best.get("wr") is None:
                        text = f"Best augment: {best['name']} (no champ data)"
                        fg = GOLD
                    else:
                        lift = best.get("lift")
                        lift_text = f", {_fmt_signed_pct(float(lift) * 100)}" if lift is not None else ""
                        text = f"Best augment: {best['name']} ({_fmt_prob(float(best['wr']))}{lift_text})"
                        fg = GREEN
                else:
                    text = "Augment offer detected, no options parsed"
                    fg = GOLD
            elif event_type == "picked":
                names = [
                    str(aug.get("name") or aug.get("slot") or "?")
                    for aug in augments
                ]
                text = "Picked augments: " + (" / ".join(names[-3:]) if names else "detected")
                fg = FG
            elif event_type == "error":
                text = "Augments: latest event unreadable"
                fg = RED
            else:
                text = f"Augments: {event_type}"
                fg = DIM

        row = tk.Frame(self.augment_panel, bg=ROW, highlightthickness=1, highlightbackground=DIVIDER)
        row.pack(fill="x")
        tk.Button(
            row,
            text="Rescan",
            command=self._force_rescan_augments,
            bg=SURFACE,
            fg=DIM,
            activebackground=ROW,
            activeforeground=FG,
            relief="flat",
            bd=0,
            padx=6,
            pady=1,
            font=self._font(FONT_SECTION),
            cursor="hand2",
            takefocus=0,
        ).pack(side="right", padx=(4, 6), pady=3)
        tk.Label(
            row,
            text=text,
            bg=ROW,
            fg=fg,
            font=self._font(FONT_SUB),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=8, pady=4)

        if event is not None and event.get("type") == "offer":
            rows = self.augment_advisor.recommend(
                self.active_champion_id,
                [aug for aug in event.get("augments") or [] if isinstance(aug, dict)],
            )
            detail = " > ".join(
                str(row.get("name") or row.get("raw_name") or "?")
                for row in rows[:3]
            )
            if detail:
                tk.Label(
                    row,
                    text=detail,
                    bg=ROW,
                    fg=DIM,
                    font=self._font(FONT_SECTION),
                    anchor="e",
                ).pack(side="right", padx=8, pady=4)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "static":
            self.id_to_name = msg[1]
            self.header.config(text="Waiting for champ select", fg=FG)
            self.subheader.config(text=f"{len(self.id_to_name)} champions loaded")
            self._clear_body()
        elif kind == "idle":
            phase = msg[1]
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
        elif kind == "ingame":
            _, snapshot, current_combo, enemy_combo = msg
            active = snapshot.get("active_champion_id")
            self.active_champion_id = int(active) if active else None
            self._render_ingame(snapshot, current_combo, enemy_combo)
            self._render_augment_panel()

    # ----- Rendering -----

    def _clear_body(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()

    def _render(self, parsed, suggestions, combos=None, current_combo=None) -> None:
        combos = combos or []
        self._last_render_args = (parsed, suggestions, combos, current_combo)
        cur_name = self.id_to_name.get(parsed.my_current_id, f"#{parsed.my_current_id}")
        self.header.config(text=f"Cell {parsed.my_cell_id} · {cur_name}", fg=FG)
        self.subheader.config(
            text="MLΔ 是換人後勝率變化；z 是英雄本體強度；隊伍評分 B 約等於普通"
        )

        self._clear_body()

        self.body.grid_columnconfigure(0, weight=2, minsize=260)
        self.body.grid_columnconfigure(1, weight=3, minsize=360)
        self.body.grid_rowconfigure(0, weight=0)
        self.body.grid_rowconfigure(1, weight=1)
        self.body.grid_rowconfigure(2, weight=0)

        combo_host = tk.Frame(self.body, bg=BG)
        combo_host.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 0))
        self._render_combo_section(
            combo_host, combos, pool=suggestions, owned_ids=parsed.my_team_ids, wraplength=560,
        )

        left = tk.Frame(self.body, bg=BG)
        left.grid(row=1, column=0, sticky="new", padx=(0, 18), pady=(8, 0))
        right = tk.Frame(self.body, bg=BG)
        right.grid(row=1, column=1, sticky="new", pady=(8, 0))

        self._render_team_section(left, parsed, suggestions, current_combo)
        self._render_bench_table(right, suggestions)

    def _render_ingame(self, snapshot: dict, current_combo=None, enemy_combo=None) -> None:
        self._last_render_args = None
        self.id_to_name.update(snapshot.get("names") or {})

        self.header.config(text="In-game · 10 champions detected", fg=FG)
        self.subheader.config(text="Champ-select bench is gone; showing current 5v5 only.")
        self._clear_body()

        matchup_prob = float(snapshot.get("matchup_prob", float("nan")))
        current_prob = matchup_prob
        if math.isnan(current_prob):
            current_prob = (
                current_combo.win_prob
                if current_combo is not None and not math.isnan(current_combo.win_prob)
                else float("nan")
            )

        summary = tk.Frame(self.body, bg=BG)
        summary.pack(fill="x", pady=(0, 8))
        tk.Label(
            summary, text=f"5v5 estimate {_fmt_prob(current_prob)}",
            bg=BG, fg=FG, anchor="w", font=self._font(FONT_HEAD),
        ).pack(side="left")

        if current_combo is not None and getattr(current_combo, "ratings", None):
            self._render_team_ratings(
                self.body,
                current_combo.ratings,
                getattr(current_combo, "ad_share", float("nan")),
            )

        columns = tk.Frame(self.body, bg=BG)
        columns.pack(fill="both", expand=True, pady=(4, 0))
        columns.grid_columnconfigure(0, weight=1, uniform="ingame")
        columns.grid_columnconfigure(1, weight=1, uniform="ingame")

        left = tk.Frame(columns, bg=BG)
        left.grid(row=0, column=0, sticky="new", padx=(0, 12))
        right = tk.Frame(columns, bg=BG)
        right.grid(row=0, column=1, sticky="new", padx=(12, 0))

        self._render_live_team(
            left,
            "Your team",
            snapshot.get("my_team_ids") or [],
            active_champion_id=snapshot.get("active_champion_id"),
        )
        self._render_live_team(
            right,
            "Enemy team",
            snapshot.get("enemy_team_ids") or [],
        )

    def _render_live_team(
        self,
        parent: tk.Frame,
        title: str,
        champion_ids,
        active_champion_id: int | None = None,
    ) -> None:
        tk.Label(
            parent, text=title, bg=BG, fg=DIM,
            font=self._font(FONT_SECTION), anchor="w",
        ).pack(fill="x", pady=(0, 4))

        for cid in champion_ids:
            row = tk.Frame(parent, bg=ROW if cid == active_champion_id else BG)
            row.pack(fill="x", pady=1, ipady=2)
            self._configure_team_row(row)
            self._icon_cell(row, int(cid), bg=ROW if cid == active_champion_id else BG)

            name = self.id_to_name.get(int(cid), f"#{cid}")
            marker = "You · " if cid == active_champion_id else ""
            tk.Label(
                row, text=f"{marker}{name}",
                bg=ROW if cid == active_champion_id else BG,
                fg=GOLD if cid == active_champion_id else FG,
                font=self._font(FONT_NAME_B if cid == active_champion_id else FONT_NAME),
                anchor="w",
            ).grid(row=0, column=1, sticky="w")

    def _configure_team_row(self, row: tk.Frame) -> None:
        """Team rows only need icon + name (no Δ / no z column for teammates)."""
        row.grid_columnconfigure(0, minsize=self.COL_ICON)
        row.grid_columnconfigure(1, weight=1)

    def _configure_bench_row(self, row: tk.Frame) -> None:
        """Bench rows: icon + delta + prob + z + name."""
        row.grid_columnconfigure(0, minsize=self.COL_ICON)
        row.grid_columnconfigure(1, minsize=self.COL_DELTA)
        row.grid_columnconfigure(2, minsize=self.COL_PROB)
        row.grid_columnconfigure(3, minsize=self.COL_Z)
        row.grid_columnconfigure(4, weight=1)

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

        current = tk.Frame(parent, bg=BG)
        current.pack(fill="x", pady=(0, 6))
        tk.Label(
            current, text="當前勝率",
            bg=BG, fg=DIM, anchor="w", font=self._font(FONT_SECTION),
        ).pack(side="left", padx=(0, 6))
        tk.Label(
            current, text=_fmt_prob(current_prob),
            bg=BG, fg=FG, anchor="w", font=self._font(FONT_SCORE),
        ).pack(side="left")

        if current_combo is not None and getattr(current_combo, "ratings", None):
            self._render_team_ratings(parent, current_combo.ratings, getattr(current_combo, "ad_share", float("nan")))

        for cid in parsed.my_team_ids:
            is_me = (cid == parsed.my_current_id)
            row = tk.Frame(parent, bg=BG)
            row.pack(fill="x", pady=1)
            self._configure_team_row(row)

            self._icon_cell(row, cid, bg=BG)

            name = self.id_to_name.get(cid, f"#{cid}")
            if is_me:
                z_str = f"   {_fmt_signed_z(own_z)}" if own_z is not None else ""
                tk.Label(
                    row, text=f"你 · {name}{z_str}",
                    bg=BG, fg=GOLD, font=self._font(FONT_NAME_B), anchor="w",
                ).grid(row=0, column=1, sticky="w")
            else:
                tk.Label(
                    row, text=name, bg=BG, fg=DIM,
                    font=self._font(FONT_NAME), anchor="w",
                ).grid(row=0, column=1, sticky="w")

    def _render_bench_table(self, parent, suggestions) -> None:
        bench = [s for s in suggestions if s.source == "bench"]

        title_bar = tk.Frame(parent, bg=BG)
        title_bar.pack(fill="x", pady=(0, 4))
        tk.Label(
            title_bar, text="替補池",
            bg=BG, fg=DIM, anchor="w", font=self._font(FONT_SECTION),
        ).pack(side="left")
        bench_copy_btn = tk.Button(
            title_bar,
            text=COPY_ICON,
            command=lambda pool=suggestions: self._copy_bench_top(pool, bench_copy_btn),
            bg=SURFACE,
            fg=FG,
            activebackground=BEST_BG,
            activeforeground=FG,
            relief="flat",
            bd=0,
            width=2,
            padx=5,
            pady=1,
            font=self._font(FONT_ICON),
            cursor="hand2",
            takefocus=0,
        )
        bench_copy_btn.pack(side="right")

        header = tk.Frame(parent, bg=BG)
        header.pack(fill="x", pady=(0, 2))
        self._configure_bench_row(header)
        self._cell(header, 1, "MLΔ", DIM, bg=BG, font=FONT_SECTION)
        self._cell(header, 2, "換後", DIM, bg=BG, font=FONT_SECTION)
        self._cell(header, 3, "z", DIM, bg=BG, font=FONT_SECTION)
        self._cell(header, 4, "候選", DIM, bg=BG, font=FONT_SECTION)

        best_idx = next((i for i, s in enumerate(bench) if s.is_known), None)

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
                self._cell(row, 1, "n/a", MUTED, bg=row_bg, font=FONT_NUM)
                self._cell(row, 2, "n/a", MUTED, bg=row_bg, font=FONT_NUM)
                self._cell(row, 3, "n/a", MUTED, bg=row_bg, font=FONT_NUM)
                self._cell(row, 4, f"{name}   (not in vocab)", MUTED, bg=row_bg, font=FONT_NAME)
                continue

            delta_pp = s.delta * 100
            delta_font = FONT_NUM_BEST if is_best else FONT_NUM
            name_color = (GREEN if s.delta > 0 else RED) if is_best else FG
            marker = "首選 · " if is_best else ""

            self._cell(row, 1, _fmt_signed_pct(delta_pp), self._delta_color(s.delta), bg=row_bg, font=delta_font)
            self._cell(row, 2, _fmt_prob(s.win_prob), FG, bg=row_bg, font=FONT_NUM)
            self._cell(row, 3, _fmt_signed_z(s.z_score), self._z_color(s.z_score), bg=row_bg, font=FONT_NUM)
            self._cell(row, 4, f"{marker}{name}", name_color, bg=row_bg, font=FONT_NAME_B if is_best else FONT_NAME)

    def _render_combo_section(self, parent, combos, pool=None, owned_ids=None, wraplength: int = 260) -> None:
        if not combos:
            return

        total = combos[0].total_combos
        copy_bar = tk.Frame(parent, bg=BG)
        copy_bar.pack(fill="x", pady=(0, 4))
        tk.Label(
            copy_bar, text=f"可用池最佳 5 人 · 掃描 {total} 組",
            bg=BG, fg=DIM, anchor="w", font=self._font(FONT_SECTION),
        ).pack(side="left")
        copy_btn = tk.Button(
            copy_bar,
            text=COPY_ICON,
            command=lambda c=combos[0], p=pool, o=owned_ids: self._copy_combo(c, p, o, copy_btn),
            bg=SURFACE,
            fg=FG,
            activebackground=BEST_BG,
            activeforeground=FG,
            relief="flat",
            bd=0,
            width=2,
            padx=5,
            pady=1,
            font=self._font(FONT_ICON),
            cursor="hand2",
            takefocus=0,
        )
        copy_btn.pack(side="right")

        for combo in combos:
            row_bg = SURFACE if combo.rank == 1 else ROW
            row = tk.Frame(parent, bg=row_bg)
            row.pack(fill="x", pady=1, ipady=3)
            row.grid_columnconfigure(0, minsize=38)
            row.grid_columnconfigure(1, weight=1)
            row.grid_columnconfigure(2, minsize=58)
            row.grid_columnconfigure(3, minsize=64)

            tk.Label(
                row, text=f"#{combo.rank}", bg=row_bg,
                fg=GOLD if combo.rank == 1 else DIM,
                font=self._font(FONT_NUM_B if combo.rank == 1 else FONT_NUM),
                anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=(8, 4))

            names = [self.id_to_name.get(cid, f"#{cid}") for cid in combo.champion_ids]
            if combo.rank == 1:
                self._combo_icon_strip(row, combo.champion_ids, row_bg)
            else:
                tk.Label(
                    row, text=" / ".join(names), bg=row_bg, fg=FG,
                    font=self._font(FONT_NAME), anchor="w", wraplength=wraplength, justify="left",
                ).grid(row=0, column=1, sticky="ew", padx=(0, 8))

            tk.Label(
                row, text=_fmt_prob(combo.win_prob), bg=row_bg, fg=FG,
                font=self._font(FONT_NUM), anchor="e",
            ).grid(row=0, column=2, sticky="e", padx=(0, 8))

            tk.Label(
                row, text=_fmt_signed_pct(combo.delta * 100), bg=row_bg,
                fg=self._delta_color(combo.delta), font=self._font(FONT_NUM),
                anchor="e",
            ).grid(row=0, column=3, sticky="e", padx=(0, 8))

    def _combo_icon_strip(self, parent: tk.Frame, champion_ids, bg: str) -> None:
        strip = tk.Frame(parent, bg=bg)
        strip.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=1)
        for cid in champion_ids:
            self._packed_icon(strip, int(cid), bg=bg)

    def _render_team_ratings(self, parent, ratings, ad_share: float = float("nan")) -> None:
        header = tk.Frame(parent, bg=BG)
        header.pack(fill="x", pady=(5, 3))
        tk.Label(
            header, text="隊伍評分",
            bg=BG, fg=DIM, anchor="w", font=self._font(FONT_SECTION),
        ).pack(side="left")

        grid = tk.Frame(parent, bg=BG)
        grid.pack(fill="x", pady=(0, 4))
        column_count = min(max(len(ratings), 1), 5)
        for col in range(column_count):
            grid.grid_columnconfigure(col, weight=1, uniform="team_rating")

        for i, rating in enumerate(ratings[:5]):
            row = i // column_count
            col = i % column_count
            chip = tk.Frame(grid, bg=ROW, highlightthickness=1, highlightbackground=DIVIDER)
            chip.grid(row=row, column=col, sticky="ew", padx=(0 if col == 0 else 4, 0), pady=(0, 3))
            label_color = RED if rating.label.startswith(("高風險", "風險")) else DIM

            tk.Label(
                chip, text=rating.label, bg=ROW, fg=label_color,
                font=self._font(FONT_SECTION), anchor="w",
            ).pack(side="left", padx=(5, 3), pady=2)
            tk.Label(
                chip, text=_rating_value_text(rating), bg=ROW,
                fg=self._rating_color(rating.value), font=self._font(FONT_NUM_B),
                anchor="w",
            ).pack(side="left", pady=2)
            if rating.detail and rating.label != "AD佔比":
                tk.Label(
                    chip, text=rating.detail, bg=ROW, fg=MUTED,
                    font=self._font(FONT_SECTION), anchor="e",
                ).pack(side="right", padx=(3, 5), pady=2)

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
            button.config(text=COPIED_ICON, fg=GREEN)
            self.root.after(
                1200,
                lambda: button.winfo_exists() and button.config(text=COPY_ICON, fg=FG),
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
            button.config(text=COPIED_ICON, fg=GREEN)
            self.root.after(
                1200,
                lambda: button.winfo_exists() and button.config(text=COPY_ICON, fg=FG),
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

    def _packed_icon(self, parent: tk.Frame, champion_id: int, bg: str = BG) -> None:
        photo = self.icon_cache.get(champion_id) if self.icon_cache else None
        if photo is not None:
            lbl = tk.Label(parent, image=photo, bg=bg, bd=0)
            lbl.image = photo  # type: ignore[attr-defined]
            lbl.pack(side="left", padx=(0, 6))
        else:
            tk.Label(parent, text="", bg=bg, width=4, height=2).pack(side="left", padx=(0, 6))

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
        bg: str = BG, font: tuple = FONT_NUM,
    ) -> None:
        """Place a left-aligned label at `col` in the row's shared grid.

        Width is no longer passed explicitly: column widths come from
        the row's grid_columnconfigure(minsize=...) so every row pins
        to the same x positions regardless of which font the content
        is set in.
        """
        tk.Label(
            parent, text=text, bg=bg, fg=fg,
            font=self._font(font), anchor="w",
        ).grid(row=0, column=col, sticky="w")


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
@click.option("--verbose", is_flag=True, default=False,
              help="Print per-poll status (phase + session presence) to stdout. "
                   "Useful for diagnosing why a champ-select isn't being detected.")
def main(
    lr_model: Path | None,
    vocab: Path | None,
    pair_stats: Path | None,
    composition_model: Path | None,
    poll_interval: float,
    fake: bool,
    verbose: bool,
) -> None:
    """Tk GUI for the ARAM champ-select recommender."""
    _enable_windows_dpi_awareness()
    _set_app_user_model_id()

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
            target=fake_poll_loop, args=(stop_event, q, model, pair_model, comp_model), daemon=True,
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
    RecommenderApp(root, q, icon_cache=icon_cache)
    try:
        root.mainloop()
    finally:
        # Signal the poll thread to exit cleanly so the httpx client closes.
        stop_event.set()


if __name__ == "__main__":
    main()
