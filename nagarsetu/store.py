"""SQLite persistence: tickets, an append-only audit log, and a demo clock."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    reporter TEXT, phone TEXT,
    text TEXT NOT NULL, lang TEXT,
    lat REAL NOT NULL, lng REAL NOT NULL,
    ward_id TEXT, zone TEXT,
    category TEXT, severity INTEGER, hazards TEXT,
    summary_en TEXT, summary_hi TEXT,
    ai TEXT,
    dept TEXT, crew_id TEXT,
    status TEXT NOT NULL,
    sla_due REAL, level INTEGER DEFAULT 1, at_risk INTEGER DEFAULT 0,
    parent_id TEXT, supporters INTEGER DEFAULT 0,
    resolved_at REAL, closed_at REAL,
    proof TEXT, review_reason TEXT, photo TEXT, rating INTEGER
);
CREATE INDEX IF NOT EXISTS ix_t_status ON tickets(status);
CREATE INDEX IF NOT EXISTS ix_t_cat ON tickets(category);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL, ts REAL NOT NULL,
    actor TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_e_ticket ON events(ticket_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

JSON_COLS = ("hazards", "ai", "proof")


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- demo clock ------------------------------------------------------
    def now(self) -> float:
        return time.time() + float(self.get_meta("clock_offset", "0"))

    def advance_clock(self, hours: float) -> float:
        offset = float(self.get_meta("clock_offset", "0")) + hours * 3600
        self.set_meta("clock_offset", str(offset))
        return self.now()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
            self.db.commit()

    # --- tickets -----------------------------------------------------------
    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for c in JSON_COLS:
            d[c] = json.loads(d[c]) if d.get(c) else ([] if c == "hazards" else None)
        return d

    def insert(self, t: dict) -> None:
        t = {**t}
        for c in JSON_COLS:
            if c in t and t[c] is not None:
                t[c] = json.dumps(t[c], ensure_ascii=False)
        cols = ",".join(t)
        with self.lock:
            self.db.execute(f"INSERT INTO tickets({cols}) VALUES({','.join('?' * len(t))})",
                            tuple(t.values()))
            self.db.commit()

    def update(self, ticket_id: str, **fields) -> None:
        fields["updated_at"] = self.now()
        for c in JSON_COLS:
            if c in fields and fields[c] is not None:
                fields[c] = json.dumps(fields[c], ensure_ascii=False)
        sets = ",".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE tickets SET {sets} WHERE id=?", (*fields.values(), ticket_id))
            self.db.commit()

    def get(self, ticket_id: str) -> dict | None:
        with self.lock:
            return self._row(self.db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone())

    def tickets(self, where: str = "1=1", params: tuple = (), order: str = "created_at DESC",
                limit: int | None = None) -> list[dict]:
        sql = f"SELECT * FROM tickets WHERE {where} ORDER BY {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.lock:
            return [self._row(r) for r in self.db.execute(sql, params).fetchall()]

    def count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]

    def next_id(self) -> str:
        with self.lock:
            n = int(self.get_meta("seq", "1000")) + 1
            self.set_meta("seq", str(n))
        return f"IND-{n}"

    # --- audit log -----------------------------------------------------------
    def log(self, ticket_id: str, actor: str, kind: str, detail: dict | None = None,
            ts: float | None = None) -> None:
        with self.lock:
            self.db.execute("INSERT INTO events(ticket_id, ts, actor, kind, detail) VALUES(?,?,?,?,?)",
                            (ticket_id, ts if ts is not None else self.now(), actor, kind,
                             json.dumps(detail or {}, ensure_ascii=False)))
            self.db.commit()

    def events(self, ticket_id: str | None = None, limit: int = 200) -> list[dict]:
        with self.lock:
            if ticket_id:
                rows = self.db.execute("SELECT * FROM events WHERE ticket_id=? ORDER BY ts, id",
                                       (ticket_id,)).fetchall()
            else:
                rows = self.db.execute("SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?",
                                       (limit,)).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]

    def reset(self) -> None:
        with self.lock:
            self.db.executescript("DELETE FROM tickets; DELETE FROM events; DELETE FROM meta;")
            self.db.commit()
