"""Quick-picks overview for one champion, shared by the desktop recommender.

This is the Python twin of `buildQuickOverview` in `scripts/templates/site.js`:
the same tier-list payload + per-champion detail shard go in, the same
recommended build / boots / summoner spells / top-3 augments come out.  The
selection rules live here (not in the Tk layer) so they stay testable and so
the app and the site cannot quietly disagree about what "recommended" means.

`QuickPickSource` resolves the data and icons: a source checkout reads the
generated files under `docs/`; a frozen app has no shards on disk, so it falls
back to the public site and caches what it fetched.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path

import httpx

SITE_BASE = "https://arammeta.com/"
DDRAGON_ITEM_ICON = "https://ddragon.leagueoflegends.com/cdn/{ver}/img/item/{item_id}.png"
RARITIES = ("kSilver", "kGold", "kPrismatic")
TOP_AUGMENTS = 3


@dataclass(frozen=True)
class QuickItem:
    id: int
    name: str
    icon: str
    is_core: bool = False


@dataclass(frozen=True)
class QuickRoute:
    label: str
    pick: float | None


@dataclass(frozen=True)
class QuickAugment:
    id: int
    name: str
    icon: str
    wr: float
    lift: float | None


@dataclass(frozen=True)
class QuickOverview:
    champion_id: int
    route: int
    routes: tuple[QuickRoute, ...]
    # True: `items` is the full build (core, common follow-ups, boots).
    # False: not enough games for a full build, `items` is the core only.
    complete: bool
    items: tuple[QuickItem, ...]
    core_size: int
    core_wr: float | None
    core_games: int
    completion_games: int
    boot: QuickItem | None
    boot_wr: float | None
    boot_games: int
    spells: tuple[QuickItem, ...]
    augments: dict[str, tuple[QuickAugment, ...]] = field(default_factory=dict)

    def icon_refs(self) -> list[str]:
        refs = [item.icon for item in self.items]
        if self.boot:
            refs.append(self.boot.icon)
        refs.extend(item.icon for item in self.spells)
        for rows in self.augments.values():
            refs.extend(row.icon for row in rows)
        return [ref for ref in refs if ref]


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _item(row: dict, payload: dict, *, is_core: bool = False) -> QuickItem:
    """Resolve an item row to name + icon.

    Detail shards strip item identities into `payload["itemLut"]` (rows tagged
    `ic`); summoner spells keep their own name and icon inline.
    """
    item_id = int(row["id"])
    meta = (payload.get("itemLut") or {}).get(str(item_id)) or {}
    name = row.get("name_zh") or row.get("name") or meta.get("z") or meta.get("e") or f"#{item_id}"
    icon = row.get("icon") or ""
    if not icon and payload.get("ddv"):
        icon = DDRAGON_ITEM_ICON.format(ver=payload["ddv"], item_id=item_id)
    return QuickItem(id=item_id, name=str(name), icon=str(icon), is_core=is_core)


def route_count(detail: dict) -> int:
    return len((detail.get("itemClusters") or {}).get("groups") or [])


def select_quick_overview(champion_id: int, detail: dict, payload: dict, route: int = 0) -> QuickOverview:
    groups = (detail.get("itemClusters") or {}).get("groups") or []
    route = min(max(0, int(route)), max(0, len(groups) - 1))
    group = groups[route] if groups else {}

    quick_core = group.get("quickCore") or {}
    if quick_core.get("items"):
        core_rows, core_games, core_wr = quick_core["items"], quick_core.get("g"), quick_core.get("wr")
    else:
        core_rows, core_games, core_wr = group.get("core") or [], group.get("g"), group.get("wr")
    core_ids = {int(row["id"]) for row in core_rows}
    completion = group.get("completion") or []
    completion_ids = {int(row["id"]) for row in completion}
    completion_games = int(group.get("completionGames") or 0)
    boot_row = group.get("routeBoot") or None

    # The engine only emits a completion whose every step cleared its support
    # floor, so this checks shape (extends this core, ends in this route's
    # boots), not sample size.
    complete = (
        len(completion) > len(core_rows) + 1
        and completion_games > 0
        and core_ids <= completion_ids
        and boot_row is not None
        and int(boot_row["id"]) in completion_ids
    )
    rows = completion if complete else core_rows

    routes = tuple(
        QuickRoute(
            label=" + ".join(_item(row, payload).name for row in g.get("core") or []),
            pick=_finite(g.get("pick")),
        )
        for g in groups
    )

    spell = ((detail.get("spells") or {}).get("top") or [{}])[0]
    champ = (payload.get("champs") or {}).get(str(champion_id)) or {}
    top = detail.get("top") or champ.get("top") or {}
    augs = payload.get("augs") or {}
    augments: dict[str, tuple[QuickAugment, ...]] = {}
    for rarity in RARITIES:
        picked = []
        for row in (top.get(rarity) or [])[:TOP_AUGMENTS]:
            aug = augs.get(str(row.get("id")))
            wr = _finite(row.get("wr"))
            if not aug or wr is None:
                continue
            picked.append(QuickAugment(
                id=int(row["id"]),
                name=str(aug.get("name_zh") or aug.get("name") or aug.get("name_en") or row["id"]),
                icon=str(aug.get("icon") or ""),
                wr=wr,
                lift=_finite(row.get("lift")),
            ))
        augments[rarity] = tuple(picked)

    core_wr = _finite(core_wr)
    core_games = int(core_games or 0)
    return QuickOverview(
        champion_id=int(champion_id),
        route=route,
        routes=routes,
        complete=complete,
        items=tuple(_item(row, payload, is_core=int(row["id"]) in core_ids) for row in rows),
        core_size=len(core_rows),
        core_wr=core_wr if core_games > 0 else None,
        core_games=core_games,
        completion_games=completion_games,
        boot=_item(boot_row, payload) if boot_row else None,
        boot_wr=_finite(boot_row.get("wr")) if boot_row else None,
        boot_games=int(boot_row.get("g") or 0) if boot_row else 0,
        spells=tuple(_item(row, payload) for row in spell.get("items") or []),
        augments=augments,
    )


class QuickPickSource:
    """Loads champion detail shards and the icons a quick overview needs.

    `local_root` is the generated site root (`docs/`) when running from a
    source checkout.  Anything missing there is fetched from the public site
    and kept under `cache_dir`.  All methods are safe to call off the Tk
    thread; none of them create Tk objects.
    """

    def __init__(
        self,
        payload: dict,
        cache_dir: Path,
        local_root: Path | None = None,
        site_base: str = SITE_BASE,
        timeout: float = 6.0,
    ) -> None:
        self.payload = payload
        self.cache_dir = Path(cache_dir)
        self.local_root = Path(local_root) if local_root else None
        self.site_base = site_base.rstrip("/") + "/"
        self.timeout = timeout
        self._details: dict[int, dict | None] = {}
        self._lock = threading.Lock()

    # ----- detail shards -----

    def detail(self, champion_id: int) -> dict | None:
        with self._lock:
            if champion_id in self._details:
                return self._details[champion_id]
        detail = self._load_detail(champion_id)
        if detail:  # a failed fetch stays uncached so the next game retries
            with self._lock:
                self._details[champion_id] = detail
        return detail

    def _load_detail(self, champion_id: int) -> dict | None:
        base = str(self.payload.get("detailBase") or "api/champions").strip("/")
        version = str(self.payload.get("detailVersion") or "unversioned")
        rel = f"{base}/{int(champion_id)}.json"
        cached = self.cache_dir / "detail" / version / f"{int(champion_id)}.json"
        candidates = [cached]
        if self.local_root:
            candidates.insert(0, self.local_root / rel)
        for path in candidates:
            try:
                if path.exists():
                    return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
        try:
            response = httpx.get(
                f"{self.site_base}{rel}", params={"v": version},
                timeout=self.timeout, follow_redirects=True,
            )
            if response.status_code != 200:
                return None
            detail = response.json()
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(response.content)
            return detail
        except (httpx.HTTPError, OSError, ValueError):
            return None

    def overview(self, champion_id: int, route: int = 0) -> QuickOverview | None:
        detail = self.detail(champion_id)
        if not detail:
            return None
        return select_quick_overview(champion_id, detail, self.payload, route)

    # ----- icons -----

    def icon_path(self, ref: str, *, fetch: bool = True) -> Path | None:
        """Local PNG for an icon ref (absolute URL or site-relative path)."""
        if not ref:
            return None
        is_url = ref.startswith(("http://", "https://"))
        if not is_url and self.local_root:
            local = self.local_root / ref
            if local.exists():
                return local
        url = ref if is_url else f"{self.site_base}{ref.lstrip('/')}"
        cached = self.cache_dir / "assets" / f"{hashlib.sha1(url.encode('utf-8')).hexdigest()}.png"
        if cached.exists():
            return cached
        if not fetch:
            return None
        try:
            response = httpx.get(url, timeout=self.timeout, follow_redirects=True)
            if response.status_code != 200 or not response.content:
                return None
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(response.content)
            return cached
        except (httpx.HTTPError, OSError):
            return None

    def warm(self, champion_id: int) -> bool:
        """Fetch the shard and every icon any route of this champion shows."""
        detail = self.detail(champion_id)
        if not detail:
            return False
        refs: set[str] = set()
        for route in range(max(1, route_count(detail))):
            refs.update(select_quick_overview(champion_id, detail, self.payload, route).icon_refs())
        for ref in sorted(refs):
            self.icon_path(ref)
        return True
