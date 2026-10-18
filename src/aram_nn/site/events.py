"""Anonymous UI usage counters for product decisions.

Only allowlisted event names with small enum parameters are stored: no query
text, IP address, user agent or visitor id ever reaches the table.  Add an
event to ``EVENT_PARAMS`` (and the matching name in site.js
``USAGE_EVENTS``) before the frontend may send it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_EVENTS_DB = Path(os.environ.get("ARAM_EVENTS_DB", "data/site/events.db"))
DEFAULT_EVENTS_RATE_LIMIT_PER_HOUR = 240

# event name -> {param name -> allowed values}
EVENT_PARAMS: dict[str, dict[str, frozenset[str]]] = {
    "search_scope_open": {},
    "search_scope_change": {"scope": frozenset({"champions", "all"})},
    "search_use": {"scope": frozenset({"champions", "all"})},
}
EVENT_LOCALES = frozenset({"zh-Hant", "zh-CN", "en"})
EVENT_VIEWPORTS = frozenset({"mobile", "desktop"})


CREATE_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS ui_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    params      TEXT NOT NULL DEFAULT '{}',
    locale      TEXT NOT NULL DEFAULT '',
    viewport    TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
"""

CREATE_EVENTS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_ui_events_name_created
ON ui_events(name, created_at);
"""


class EventValidationError(ValueError):
    """Raised when a usage event is not in the allowlisted contract."""


def events_rate_limit_per_hour() -> int:
    raw = os.environ.get("ARAM_EVENTS_RATE_LIMIT_PER_HOUR", "").strip()
    if raw == "":
        return DEFAULT_EVENTS_RATE_LIMIT_PER_HOUR
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_EVENTS_RATE_LIMIT_PER_HOUR


def normalize_event(payload: Any) -> dict[str, str]:
    """Reduce a client payload to the stored contract or raise."""
    if not isinstance(payload, dict):
        raise EventValidationError("JSON object required")
    name = payload.get("name")
    if not isinstance(name, str) or name not in EVENT_PARAMS:
        raise EventValidationError("unknown event")
    raw_params = payload.get("params") or {}
    if not isinstance(raw_params, dict):
        raise EventValidationError("params must be an object")
    params: dict[str, str] = {}
    for key, allowed in EVENT_PARAMS[name].items():
        value = raw_params.get(key)
        if value not in allowed:
            raise EventValidationError(f"invalid {key}")
        params[key] = value
    locale = payload.get("locale")
    viewport = payload.get("viewport")
    return {
        "name": name,
        "params": json.dumps(params, sort_keys=True, separators=(",", ":")),
        "locale": locale if locale in EVENT_LOCALES else "",
        "viewport": viewport if viewport in EVENT_VIEWPORTS else "",
    }


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=30.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def insert_event(
    db_path: Path,
    event: dict[str, str],
    *,
    created_at: str | None = None,
) -> None:
    timestamp = created_at or dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
    con = _connect(db_path)
    try:
        con.execute(CREATE_EVENTS_SQL)
        con.execute(CREATE_EVENTS_INDEX_SQL)
        con.execute(
            "INSERT INTO ui_events (name, params, locale, viewport, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (event["name"], event["params"], event["locale"], event["viewport"], timestamp),
        )
        con.commit()
    finally:
        con.close()
