from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


CREATE_RUNTIME_STATE_SQL = """
CREATE TABLE IF NOT EXISTS crawl_runtime_state (
    state_key   TEXT PRIMARY KEY,
    state_value TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

CAPTURE_WATERMARK_PREFIX = "latest_capture:"


def capture_watermark_key(queue_id: int) -> str:
    return f"{CAPTURE_WATERMARK_PREFIX}{int(queue_id)}"


def ensure_runtime_state_schema(con: sqlite3.Connection) -> None:
    con.execute(CREATE_RUNTIME_STATE_SQL)


def update_capture_watermark(
    con: sqlite3.Connection,
    *,
    queue_id: int,
    captured_at: str,
) -> None:
    """Record a successful game insert without committing the caller's transaction."""
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        """
        INSERT INTO crawl_runtime_state(state_key, state_value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(state_key) DO UPDATE SET
            state_value = excluded.state_value,
            updated_at = excluded.updated_at
        """,
        (capture_watermark_key(queue_id), str(captured_at), now),
    )
