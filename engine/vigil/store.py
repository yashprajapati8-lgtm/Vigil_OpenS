"""Durable memory: an append-only event log plus the current state of each incident.

Every state change is written as an event and applied to the incident row in ONE
transaction, so the UI timeline and the incident can never disagree, and VIGIL
can restart mid-incident without losing anything.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id         TEXT PRIMARY KEY,
    seq        INTEGER UNIQUE NOT NULL,
    status     TEXT NOT NULL,
    severity   TEXT NOT NULL,
    opened_at  REAL NOT NULL,
    closed_at  REAL,
    services   TEXT NOT NULL,          -- JSON list, in the order they joined
    root       TEXT,
    category   TEXT,
    title      TEXT NOT NULL,
    data       TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    incident_id TEXT,
    stage       TEXT NOT NULL,          -- detect | incident | diagnose | decide | act | verify | system
    actor       TEXT NOT NULL,          -- detector | engine | walk | llm | policy | executor | verifier | human
    kind        TEXT NOT NULL,
    summary     TEXT NOT NULL,          -- one plain-English line for the narration feed
    payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_incident ON events(incident_id, id);
CREATE TABLE IF NOT EXISTS outcomes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    incident_id TEXT NOT NULL,
    category    TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL,
    success     INTEGER NOT NULL
);
"""

TERMINAL = ("RESOLVED", "SELF_RESOLVED", "ESCALATED")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    # ---- writes (each one transaction)

    def _insert_event(self, ev: dict) -> dict:
        cur = self._db.execute(
            "INSERT INTO events(ts, incident_id, stage, actor, kind, summary, payload) VALUES (?,?,?,?,?,?,?)",
            (ev["ts"], ev.get("incident_id"), ev["stage"], ev["actor"], ev["kind"], ev["summary"],
             json.dumps(ev.get("payload", {}))),
        )
        return {**ev, "id": cur.lastrowid, "payload": ev.get("payload", {})}

    def append_event(self, **ev) -> dict:
        ev.setdefault("ts", time.time())
        with self._lock:
            return self._insert_event(ev)

    def create_incident(self, *, services: list[str], title: str, severity: str, event: dict) -> tuple[dict, dict]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                seq = (self._db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM incidents").fetchone()[0])
                inc_id = f"VGL-{seq:04d}"
                now = event.setdefault("ts", time.time())
                self._db.execute(
                    "INSERT INTO incidents(id, seq, status, severity, opened_at, services, title) VALUES (?,?,?,?,?,?,?)",
                    (inc_id, seq, "OPEN", severity, now, json.dumps(services), title),
                )
                ev = self._insert_event({**event, "incident_id": inc_id,
                                         "summary": event["summary"].replace("{id}", inc_id)})
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return self.get_incident(inc_id), ev

    def update_incident(self, inc_id: str, fields: dict, event: dict | None = None) -> dict | None:
        """Apply field changes and (optionally) log the event that caused them, atomically."""
        cols = {k: (json.dumps(v) if k in ("services", "data") else v) for k, v in fields.items()}
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if cols:
                    sets = ", ".join(f"{k} = ?" for k in cols)
                    self._db.execute(f"UPDATE incidents SET {sets} WHERE id = ?", (*cols.values(), inc_id))
                ev = None
                if event:
                    event.setdefault("ts", time.time())
                    ev = self._insert_event({**event, "incident_id": inc_id})
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return ev

    # ---- reads

    @staticmethod
    def _incident(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["services"] = json.loads(d["services"])
        d["data"] = json.loads(d["data"])
        return d

    @staticmethod
    def _event(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d

    def get_incident(self, inc_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM incidents WHERE id = ?", (inc_id,)).fetchone()
        return self._incident(row) if row else None

    def list_incidents(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM incidents ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [self._incident(r) for r in rows]

    def open_incidents(self) -> list[dict]:
        marks = ",".join("?" * len(TERMINAL))
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM incidents WHERE status NOT IN ({marks}) ORDER BY seq", TERMINAL).fetchall()
        return [self._incident(r) for r in rows]

    def events(self, incident_id: str | None = None, since_id: int = 0, limit: int = 200) -> list[dict]:
        with self._lock:
            return self._events_locked(incident_id, since_id, limit)

    def _events_locked(self, incident_id, since_id, limit) -> list[dict]:
        if incident_id:
            rows = self._db.execute(
                "SELECT * FROM events WHERE incident_id = ? AND id > ? ORDER BY id LIMIT ?",
                (incident_id, since_id, limit)).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM (SELECT * FROM events WHERE id > ? ORDER BY id DESC LIMIT ?) ORDER BY id",
                (since_id, limit)).fetchall()
        return [self._event(r) for r in rows]

    # ---- outcome memory: did this kind of fix work for this kind of failure?

    def record_outcome(self, incident_id: str, category: str, action: str, target: str, success: bool) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO outcomes(ts, incident_id, category, action, target, success) VALUES (?,?,?,?,?,?)",
                (time.time(), incident_id, category, action, target, int(success)))

    def outcome_history(self) -> dict[tuple[str, str], tuple[int, int]]:
        """(category, action) -> (successes, attempts)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT category, action, SUM(success), COUNT(*) FROM outcomes GROUP BY category, action").fetchall()
        return {(r[0], r[1]): (int(r[2]), int(r[3])) for r in rows}
