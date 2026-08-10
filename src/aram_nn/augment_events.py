"""Privacy-safe contract for dynamic ARAM: Mayhem augment events.

The Overwolf bridge currently receives ``offer`` and ``picked`` payloads as
human-readable names.  This module defines the normalized, joinable record that
future capture must emit.  Names are intentionally not part of the canonical
training key: resolve them to augment IDs before writing the record.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

SCHEMA_VERSION = 1
EVENT_TYPES = frozenset({"offer", "picked"})
PATCH_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
PRIVATE_KEYS = frozenset({"puuid", "riotid", "riot_id", "summonername", "summoner_name", "game_name", "tag_line"})


def event_id(*, match_id: str, player_key: str, round_index: int, event_type: str) -> str:
    """Generate a deterministic non-PII event id for idempotent ingestion."""
    raw = f"{match_id}|{player_key}|{round_index}|{event_type}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def validate_event(record: dict[str, Any]) -> list[str]:
    """Return contract violations; an empty list means the record is valid."""
    errors: list[str] = []
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version")
    if record.get("event_type") not in EVENT_TYPES:
        errors.append("event_type")
    for key in ("event_id", "match_id", "player_key", "captured_at", "source"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            errors.append(key)
    if not isinstance(record.get("round_index"), int) or record["round_index"] < 1:
        errors.append("round_index")
    if not isinstance(record.get("champion_id"), int) or record["champion_id"] <= 0:
        errors.append("champion_id")
    if not isinstance(record.get("patch"), str) or not PATCH_RE.fullmatch(record["patch"]):
        errors.append("patch")
    ids = record.get("augment_ids")
    if not isinstance(ids, list) or not 2 <= len(ids) <= 3 or any(not isinstance(x, int) or x <= 0 for x in ids):
        errors.append("augment_ids")
    if isinstance(ids, list) and len(set(ids)) != len(ids):
        errors.append("augment_ids_unique")
    if record.get("event_type") == "picked":
        picked = record.get("picked_augment_id")
        if not isinstance(picked, int) or picked <= 0:
            errors.append("picked_augment_id")
        elif isinstance(ids, list) and picked not in ids:
            errors.append("picked_in_offer")
    elif record.get("picked_augment_id") is not None:
        errors.append("picked_augment_id_null_for_offer")
    try:
        datetime.fromisoformat(str(record.get("captured_at")).replace("Z", "+00:00"))
    except ValueError:
        errors.append("captured_at_iso8601")
    # Defence in depth: no canonical record may carry raw identity fields.
    lowered = {str(k).lower() for k in record}
    if lowered & PRIVATE_KEYS:
        errors.append("private_identifier_field")
    return sorted(set(errors))


def normalize_event(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize and validate one canonical record, raising ``ValueError``."""
    normalized = dict(record)
    normalized.setdefault("schema_version", SCHEMA_VERSION)
    normalized["augment_ids"] = [int(x) for x in normalized.get("augment_ids") or []]
    errors = validate_event(normalized)
    if errors:
        raise ValueError("invalid augment event: " + ", ".join(errors))
    return normalized


def validate_jsonl(path: str) -> dict[str, Any]:
    """Validate canonical JSONL or report missing context in raw bridge events."""
    counts: dict[str, int] = {"lines": 0, "valid": 0, "invalid_json": 0, "invalid_contract": 0}
    missing: dict[str, int] = {}
    types: dict[str, int] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            counts["lines"] += 1
            try:
                outer = json.loads(line)
            except json.JSONDecodeError:
                counts["invalid_json"] += 1
                continue
            raw = outer.get("payload", outer)
            raw_event = raw.get("event", raw) if isinstance(raw, dict) else {}
            et = raw_event.get("type") if isinstance(raw_event, dict) else None
            types[str(et)] = types.get(str(et), 0) + 1
            errors = validate_event(raw_event if isinstance(raw_event, dict) else {})
            if errors:
                counts["invalid_contract"] += 1
                for error in errors:
                    missing[error] = missing.get(error, 0) + 1
            else:
                counts["valid"] += 1
    return {"path": str(path), "counts": counts, "event_types": types, "violations": missing}
