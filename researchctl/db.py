from __future__ import annotations

import sqlite3
from pathlib import Path

from .paths import ensure_project_dirs, state_db_path


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS anchors (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  object_path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY,
  anchor_id TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  object_path TEXT NOT NULL,
  branch TEXT,
  base_branch TEXT,
  worktree_path TEXT,
  contract_hash TEXT,
  run_ids_json TEXT NOT NULL DEFAULT '[]',
  verdict TEXT,
  paper_merge_status TEXT NOT NULL DEFAULT 'not_eligible',
  budget_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(anchor_id) REFERENCES anchors(id)
);

CREATE TABLE IF NOT EXISTS sessions (
  session_key TEXT PRIMARY KEY,
  claim_id TEXT,
  role TEXT NOT NULL,
  platform TEXT NOT NULL,
  worktree_path TEXT,
  last_seen_at TEXT NOT NULL,
  FOREIGN KEY(claim_id) REFERENCES claims(id)
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  claim_id TEXT NOT NULL,
  host TEXT,
  status TEXT NOT NULL,
  command TEXT,
  artifact_root TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  FOREIGN KEY(claim_id) REFERENCES claims(id)
);

CREATE TABLE IF NOT EXISTS reviews (
  id TEXT PRIMARY KEY,
  claim_id TEXT NOT NULL,
  reviewer_role TEXT NOT NULL,
  decision TEXT NOT NULL,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(claim_id) REFERENCES claims(id)
);

CREATE TABLE IF NOT EXISTS paper_builds (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  merged_claims_json TEXT NOT NULL DEFAULT '[]',
  block_reason TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
"""


def connect(root: Path) -> sqlite3.Connection:
    ensure_project_dirs(root)
    conn = sqlite3.connect(state_db_path(root))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def next_id(conn: sqlite3.Connection, table: str, prefix: str) -> str:
    rows = conn.execute(f"SELECT id FROM {table} WHERE id LIKE ? ORDER BY id", (f"{prefix}%",)).fetchall()
    max_num = 0
    for row in rows:
        value = str(row["id"])
        if value.startswith(prefix) and value[len(prefix):].isdigit():
            max_num = max(max_num, int(value[len(prefix):]))
    return f"{prefix}{max_num + 1:03d}"


def one(conn: sqlite3.Connection, query: str, params: tuple = ()) -> sqlite3.Row | None:
    return conn.execute(query, params).fetchone()
