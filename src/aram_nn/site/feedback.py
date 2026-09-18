"""Private, privacy-aware storage and validation for product feedback."""

from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_FEEDBACK_DB = Path(
    os.environ.get("ARAM_FEEDBACK_DB", "data/site/feedback.db")
)
DEFAULT_FEEDBACK_RATE_LIMIT_PER_HOUR = 5
MAX_MESSAGE_LENGTH = 3000
MAX_PAGE_PATH_LENGTH = 256
MAX_EMAIL_LENGTH = 254

FEEDBACK_CATEGORIES = frozenset({"feature", "usability", "bug", "data", "other"})
FEEDBACK_FEATURES = frozenset(
    {"champions", "augments", "draft", "metapick", "changes", "mobile", "other"}
)
FEEDBACK_IMPACTS = frozenset({"blocked", "friction", "idea"})
FEEDBACK_LOCALES = frozenset({"zh-Hant", "zh-CN", "en"})
FEEDBACK_THEMES = frozenset({"dark", "light"})
FEEDBACK_VIEWPORTS = frozenset({"mobile", "desktop"})
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


CREATE_FEEDBACK_SQL = """
CREATE TABLE IF NOT EXISTS feedback (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    category       TEXT NOT NULL,
    feature        TEXT NOT NULL,
    message        TEXT NOT NULL,
    impact         TEXT NOT NULL,
    page_path      TEXT NOT NULL DEFAULT '',
    locale         TEXT NOT NULL DEFAULT 'zh-Hant',
    theme          TEXT NOT NULL DEFAULT '',
    viewport       TEXT NOT NULL DEFAULT '',
    contact_email  TEXT,
    created_at     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'new'
);
"""

CREATE_FEEDBACK_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_feedback_status_created
ON feedback(status, created_at);
"""

INSERT_FEEDBACK_SQL = """
INSERT INTO feedback (
    category, feature, message, impact, page_path, locale, theme, viewport,
    contact_email, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class FeedbackValidationError(ValueError):
    """Raised when a public feedback submission does not satisfy the contract."""


def feedback_rate_limit_per_hour() -> int:
    raw = os.environ.get("ARAM_FEEDBACK_RATE_LIMIT_PER_HOUR", "").strip()
    if raw == "":
        return DEFAULT_FEEDBACK_RATE_LIMIT_PER_HOUR
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_FEEDBACK_RATE_LIMIT_PER_HOUR


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise FeedbackValidationError(f"{key} must be a string")
    return value.strip()


def _enum(payload: dict[str, Any], key: str, allowed: frozenset[str], default: str) -> str:
    value = _text(payload, key) or default
    if value not in allowed:
        raise FeedbackValidationError(f"invalid {key}")
    return value


def _optional_context(
    payload: dict[str, Any], key: str, allowed: frozenset[str], max_length: int
) -> str:
    value = _text(payload, key)
    if not value:
        return ""
    if len(value) > max_length or any(ord(ch) < 32 for ch in value):
        raise FeedbackValidationError(f"invalid {key}")
    if allowed and value not in allowed:
        raise FeedbackValidationError(f"invalid {key}")
    return value


def normalize_feedback(payload: Any) -> dict[str, str] | None:
    """Validate and normalize a browser submission.

    A non-empty honeypot field returns ``None`` so automated spam receives the
    same successful response shape without creating a record.
    """
    if not isinstance(payload, dict):
        raise FeedbackValidationError("JSON object required")
    if _text(payload, "website"):
        return None

    category = _enum(payload, "category", FEEDBACK_CATEGORIES, "other")
    feature = _enum(payload, "feature", FEEDBACK_FEATURES, "other")
    impact = _enum(payload, "impact", FEEDBACK_IMPACTS, "idea")
    message = _text(payload, "message")
    if len(message) < 5:
        raise FeedbackValidationError("message is too short")
    if len(message) > MAX_MESSAGE_LENGTH:
        raise FeedbackValidationError("message is too long")
    if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in message):
        raise FeedbackValidationError("message contains control characters")

    page_path = _text(payload, "page_path")
    if page_path:
        if (
            len(page_path) > MAX_PAGE_PATH_LENGTH
            or not page_path.startswith("/")
            or any(ord(ch) < 32 for ch in page_path)
        ):
            raise FeedbackValidationError("invalid page_path")

    locale = _optional_context(payload, "locale", FEEDBACK_LOCALES, 16) or "zh-Hant"
    theme = _optional_context(payload, "theme", FEEDBACK_THEMES, 8)
    viewport = _optional_context(payload, "viewport", FEEDBACK_VIEWPORTS, 8)

    contact_email = _text(payload, "contact_email")
    if contact_email:
        if len(contact_email) > MAX_EMAIL_LENGTH or not _EMAIL_RE.fullmatch(contact_email):
            raise FeedbackValidationError("invalid contact_email")
        if payload.get("contact_consent") is not True:
            raise FeedbackValidationError("contact_consent is required for contact_email")
    else:
        contact_email = ""

    return {
        "category": category,
        "feature": feature,
        "message": message,
        "impact": impact,
        "page_path": page_path,
        "locale": locale,
        "theme": theme,
        "viewport": viewport,
        "contact_email": contact_email,
    }


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=30.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def ensure_feedback_schema(con: sqlite3.Connection) -> None:
    con.execute(CREATE_FEEDBACK_SQL)
    con.execute(CREATE_FEEDBACK_INDEX_SQL)
    con.commit()


def insert_feedback(
    db_path: Path,
    payload: dict[str, str],
    *,
    created_at: str | None = None,
) -> int:
    """Insert one already-normalized record and return its private reference id."""
    timestamp = created_at or dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
    con = _connect(db_path)
    try:
        ensure_feedback_schema(con)
        cursor = con.execute(
            INSERT_FEEDBACK_SQL,
            (
                payload["category"],
                payload["feature"],
                payload["message"],
                payload["impact"],
                payload["page_path"],
                payload["locale"],
                payload["theme"],
                payload["viewport"],
                payload["contact_email"] or None,
                timestamp,
            ),
        )
        con.commit()
        return int(cursor.lastrowid)
    finally:
        con.close()
