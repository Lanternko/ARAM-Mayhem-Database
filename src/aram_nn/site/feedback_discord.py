"""Durable, private Discord delivery for accepted feedback.

The database remains the receipt. Network failures never undo a submission.
Delivery is at least once: a crash after Discord accepts but before SQLite is
updated can duplicate a notification; the F-reference identifies that case.
"""

from __future__ import annotations

import logging
import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from .feedback import _connect, ensure_feedback_schema

LOG = logging.getLogger(__name__)


def discord_webhook_url() -> str:
    url = os.environ.get("ARAM_FEEDBACK_DISCORD_WEBHOOK_URL", "").strip()
    secret_file = os.environ.get("ARAM_FEEDBACK_DISCORD_WEBHOOK_FILE", "").strip()
    if not url and secret_file:
        url = Path(secret_file).read_text(encoding="utf-8").strip()
    if url and not re.fullmatch(r"https://discord\.com/api/webhooks/\d+/[A-Za-z0-9_-]+", url):
        raise ValueError("Invalid feedback Discord webhook configuration")
    return url


def notification_payload(row: dict[str, Any]) -> dict[str, Any]:
    # Two embeds keep even 3,000 astral characters within Discord's UTF-16
    # description budget without truncating the user's message.
    message = row["message"]
    embeds = [{"description": message[:1500]}]
    if len(message) > 1500:
        embeds.append({"description": message[1500:]})
    context = " · ".join(str(row.get(key) or "—") for key in ("locale", "theme", "viewport"))
    # Metadata is separate content, so a maximum-length message cannot exceed
    # Discord's aggregate embed limit. Email exists only after explicit consent.
    content = (f"**arammeta 回饋與聯絡**\n編號：F-{row['id']:06d}\n收到：{row['created_at']}\n"
               f"頁面：{row.get('page_path') or '—'}\n環境：{context}\n"
               f"回覆 Email：{row.get('contact_email') or '未提供（匿名）'}")
    return {"content": content, "embeds": embeds,
            "allowed_mentions": {"parse": [], "users": [], "roles": []}}


def deliver_one(db: Path, webhook: str, client: httpx.Client, *, now: float | None = None) -> bool:
    """Lease one due notification, send it, and record a redacted outcome."""
    now = time.time() if now is None else now
    con = _connect(db)
    try:
        con.row_factory = sqlite3.Row
        con.execute("BEGIN IMMEDIATE")
        cooldown = con.execute("""SELECT MAX(next_attempt_at) FROM feedback_discord_outbox
            WHERE delivered_at IS NULL AND last_error = 'http_429'""").fetchone()[0]
        if cooldown and cooldown > now:
            con.rollback()
            return False
        row = con.execute("""
            SELECT f.*, o.attempts FROM feedback_discord_outbox o
            JOIN feedback f ON f.id = o.feedback_id
            WHERE o.delivered_at IS NULL AND o.next_attempt_at <= ?
            ORDER BY o.next_attempt_at, o.feedback_id LIMIT 1
        """, (now,)).fetchone()
        if row is None:
            con.rollback()
            return False
        item = dict(row)
        con.execute("""UPDATE feedback_discord_outbox
            SET attempts = attempts + 1, next_attempt_at = ? WHERE feedback_id = ?""",
            (now + 120, item["id"]))
        con.commit()
    finally:
        con.close()

    retry = min(3600, 15 * 2 ** min(item["attempts"], 8))
    error = "network"
    message_id = None
    try:
        response = client.post(webhook, params={"wait": "true"}, json=notification_payload(item))
        error = f"http_{response.status_code}"
        if response.status_code == 200:
            body = response.json()
            if isinstance(body, dict) and str(body.get("id", "")).isdigit():
                message_id = str(body["id"])
            else:
                error = "invalid_response"
        elif response.status_code == 429:
            retry_after = float(response.json().get("retry_after", retry))
            if math.isfinite(retry_after):
                retry = max(1, min(86400, retry_after))
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        # Never log response bodies, request URLs, exception text, or feedback.
        pass

    con = _connect(db)
    try:
        if message_id:
            con.execute("""UPDATE feedback_discord_outbox
                SET delivered_at = ?, discord_message_id = ?, last_error = NULL
                WHERE feedback_id = ?""", (now, message_id, item["id"]))
        else:
            con.execute("""UPDATE feedback_discord_outbox
                SET next_attempt_at = ?, last_error = ? WHERE feedback_id = ?""",
                (now + retry, error, item["id"]))
        con.commit()
    finally:
        con.close()
    if not message_id:
        LOG.warning("Feedback F-%06d Discord delivery deferred: %s", item["id"], error)
    return True


def run_discord_worker(db: Path, webhook: str, stop: threading.Event) -> None:
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        while not stop.is_set():
            try:
                deliver_one(db, webhook, client)
            except Exception as exc:
                LOG.warning("Feedback Discord worker retry: %s", type(exc).__name__)
            stop.wait(2)


def start_discord_worker(db: Path) -> tuple[threading.Event, threading.Thread] | None:
    webhook = discord_webhook_url()
    if not webhook:
        return None
    con = _connect(db)
    try:
        ensure_feedback_schema(con)
    finally:
        con.close()
    stop = threading.Event()
    thread = threading.Thread(target=run_discord_worker, args=(db, webhook, stop),
                              name="feedback-discord", daemon=True)
    thread.start()
    return stop, thread
