from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .paths import events_dir, utc_now


def record_event(
    conn: sqlite3.Connection,
    root: Path,
    entity_type: str,
    entity_id: str,
    event_type: str,
    payload: dict | None = None,
) -> None:
    payload = payload or {}
    ts = utc_now()
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    conn.execute(
        "INSERT INTO events(entity_type, entity_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (entity_type, entity_id, event_type, payload_json, ts),
    )
    day = ts[:10]
    path = events_dir(root) / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "ts": ts,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "event_type": event_type,
                    "payload": payload,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
