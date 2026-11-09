"""SQLite storage. One file, created on first use."""
import sqlite3
from contextlib import contextmanager
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS merchants (
  merchant_id TEXT PRIMARY KEY, name TEXT, area TEXT, address TEXT, tz TEXT,
  hours_json TEXT, hours_verified INTEGER, synthetic INTEGER,
  pos_status TEXT DEFAULT 'online',            -- online | paused | busy (from the POS feed)
  pos_status_at TEXT
);
CREATE TABLE IF NOT EXISTS sources (
  merchant_id TEXT PRIMARY KEY, kind TEXT, expected_interval_s INTEGER,
  last_pull_at TEXT, last_change_at TEXT, pulls INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
  item_id TEXT PRIMARY KEY, merchant_id TEXT, name TEXT, category TEXT,
  description TEXT, description_flags TEXT,
  price_cents INTEGER, currency TEXT DEFAULT 'USD',
  price_verified INTEGER, verified_on TEXT, verified_how TEXT,
  listed INTEGER DEFAULT 1, observed_at TEXT, price_changed_at TEXT
);
CREATE TABLE IF NOT EXISTS tags (
  item_id TEXT, tag TEXT, value INTEGER, confidence REAL,
  source TEXT,                                  -- merchant_stated | inferred:rules | inferred:llm | agent_feedback
  updated_at TEXT, PRIMARY KEY (item_id, tag)
);
CREATE TABLE IF NOT EXISTS tag_history (
  item_id TEXT, tag TEXT, value INTEGER, confidence REAL, source TEXT, replaced_at TEXT
);
CREATE TABLE IF NOT EXISTS quotes (
  quote_id TEXT PRIMARY KEY, item_id TEXT, price_cents INTEGER, observed_at TEXT,
  issued_at TEXT, agent TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, tag TEXT, observed_value INTEGER,
  note TEXT, agent TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, agent TEXT, endpoint TEXT, request TEXT, result_count INTEGER
);
"""


@contextmanager
def connect():
    con = sqlite3.connect(config.DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with connect() as con:
        con.executescript(SCHEMA)
