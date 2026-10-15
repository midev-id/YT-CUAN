import json
import sqlite3
import threading
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY,
  yt_channel_id TEXT UNIQUE,
  title TEXT NOT NULL,
  token_enc TEXT,
  token_ok INTEGER NOT NULL DEFAULT 0,
  settings TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS stream_keys (
  id INTEGER PRIMARY KEY,
  channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
  yt_stream_id TEXT,
  name TEXT NOT NULL,
  key_enc TEXT NOT NULL,
  desired TEXT NOT NULL DEFAULT 'stopped',   -- running | stopped
  state TEXT NOT NULL DEFAULT 'stopped',     -- stopped | starting | running | degraded | reconnecting | error
  n_video INTEGER NOT NULL DEFAULT 20,
  n_audio INTEGER NOT NULL DEFAULT 20,
  locked INTEGER NOT NULL DEFAULT 1,
  locked_files TEXT,
  slots_paused INTEGER NOT NULL DEFAULT 0,
  broadcast_fails INTEGER NOT NULL DEFAULT 0,
  speed REAL,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS media (
  id INTEGER PRIMARY KEY,
  channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                 -- video | audio | thumb
  orig_name TEXT NOT NULL,
  src_path TEXT,
  path TEXT,
  status TEXT NOT NULL DEFAULT 'queued',  -- queued | processing | ready | failed
  progress REAL NOT NULL DEFAULT 0,
  error TEXT,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS titles (
  id INTEGER PRIMARY KEY,
  channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  last_used REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS broadcasts (
  id INTEGER PRIMARY KEY,
  key_id INTEGER NOT NULL REFERENCES stream_keys(id) ON DELETE CASCADE,
  yt_id TEXT,
  title TEXT,
  state TEXT NOT NULL,      -- creating | live | ending | complete | failed
  created REAL NOT NULL,
  started REAL,
  ends REAL,
  viewers INTEGER,
  restarts INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  level TEXT NOT NULL,
  channel_id INTEGER,
  msg TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quota (
  day TEXT PRIMARY KEY,
  units INTEGER NOT NULL DEFAULT 0,
  exhausted INTEGER NOT NULL DEFAULT 0
);
"""

_local = threading.local()


def conn():
    # ponytail: one sqlite connection per thread, WAL mode; enough for one VPS
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


def init():
    conn().executescript(SCHEMA)


def q(sql, *args):
    return conn().execute(sql, args).fetchall()


def one(sql, *args):
    return conn().execute(sql, args).fetchone()


def run(sql, *args):
    return conn().execute(sql, args).lastrowid


def event(level, msg, channel_id=None):
    run("INSERT INTO events(ts,level,channel_id,msg) VALUES(?,?,?,?)", time.time(), level, channel_id, msg)
    run("DELETE FROM events WHERE id < (SELECT MAX(id) - 5000 FROM events)")


def settings(channel):
    s = dict(config.CHANNEL_DEFAULTS)
    s.update(json.loads(channel["settings"] or "{}"))
    return s
