"""SQLite storage for the rider helpdesk (small: settings, orders, riders, links, handled messages)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def iso(d):
    return d.isoformat(timespec="seconds") if isinstance(d, datetime) else d


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, data TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS riders (id TEXT PRIMARY KEY, data TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS links (contact_id TEXT PRIMARY KEY, rider_id TEXT, how TEXT, contact_name TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS handled (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, conversation_id TEXT, contact_id TEXT, contact_name TEXT,
                rider_id TEXT, rider TEXT, order_ref TEXT, city TEXT, intent TEXT, action TEXT, message TEXT, reply TEXT,
                note TEXT, lang TEXT);
            CREATE TABLE IF NOT EXISTS seen (k TEXT PRIMARY KEY, at TEXT);
            CREATE INDEX IF NOT EXISTS handled_conv ON handled(conversation_id);
            """)
            self.db.commit()

    # ---------------- key/value settings ----------------
    def get(self, k, default=None):
        with self.lock:
            r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        if not r:
            return default
        try:
            return json.loads(r["v"])
        except ValueError:
            return default

    def put(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))
            self.db.commit()

    # ---------------- orders / riders (the MotionTools picture) ----------------
    def save_order(self, o: dict):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO orders(id,data,updated) VALUES(?,?,?)",
                            (o["id"], json.dumps(o, default=iso), iso(datetime.now(UTC))))
            self.db.commit()

    def save_rider(self, r: dict):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO riders(id,data,updated) VALUES(?,?,?)",
                            (r["id"], json.dumps(r, default=iso), iso(datetime.now(UTC))))
            self.db.commit()

    def load(self, table: str, since_hours: int = 36) -> list:
        cut = iso(datetime.now(UTC) - timedelta(hours=since_hours))
        with self.lock:
            rows = self.db.execute(f"SELECT data FROM {table} WHERE updated>=?", (cut,)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def load_all_riders(self) -> list:
        with self.lock:
            rows = self.db.execute("SELECT data FROM riders").fetchall()
        return [json.loads(r["data"]) for r in rows]

    def prune(self, keep_hours: int = 48):
        cut = iso(datetime.now(UTC) - timedelta(hours=keep_hours))
        cut_log = iso(datetime.now(UTC) - timedelta(days=60))
        with self.lock:
            self.db.execute("DELETE FROM orders WHERE updated<?", (cut,))
            self.db.execute("DELETE FROM seen WHERE at<?", (cut,))
            self.db.execute("DELETE FROM handled WHERE at<?", (cut_log,))
            self.db.commit()

    # ---------------- Intercom contact <-> MotionTools rider ----------------
    def link(self, contact_id: str, rider_id: str, how: str, contact_name: str = ""):
        with self.lock:
            if rider_id:
                self.db.execute("INSERT OR REPLACE INTO links VALUES(?,?,?,?,?)",
                                (contact_id, rider_id, how, contact_name, iso(datetime.now(UTC))))
            else:
                self.db.execute("DELETE FROM links WHERE contact_id=?", (contact_id,))
            self.db.commit()

    def linked(self, contact_id: str):
        with self.lock:
            r = self.db.execute("SELECT * FROM links WHERE contact_id=?", (contact_id,)).fetchone()
        return dict(r) if r else None

    def links(self) -> list:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM links ORDER BY updated DESC").fetchall()]

    # ---------------- dedupe ----------------
    def first_time(self, key: str) -> bool:
        with self.lock:
            try:
                self.db.execute("INSERT INTO seen VALUES(?,?)", (key, iso(datetime.now(UTC))))
                self.db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    # ---------------- handled messages ----------------
    def log(self, **row):
        row.setdefault("at", iso(datetime.now(UTC)))
        cols = ["at", "conversation_id", "contact_id", "contact_name", "rider_id", "rider", "order_ref", "city",
                "intent", "action", "message", "reply", "note", "lang"]
        with self.lock:
            self.db.execute(f"INSERT INTO handled({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                            [row.get(c, "") or "" for c in cols])
            self.db.commit()

    def handled(self, limit: int = 300, conversation_id: str = None) -> list:
        with self.lock:
            if conversation_id:
                rows = self.db.execute("SELECT * FROM handled WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                                       (conversation_id, limit)).fetchall()
            else:
                rows = self.db.execute("SELECT * FROM handled ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats_since(self, since: datetime) -> dict:
        with self.lock:
            rows = self.db.execute("SELECT intent, action, COUNT(*) n FROM handled WHERE at>=? GROUP BY intent, action",
                                   (iso(since),)).fetchall()
        out = {"total": 0, "auto": 0, "solved": 0, "escalated": 0, "by_intent": {}}
        for r in rows:
            out["total"] += r["n"]
            if r["action"].startswith("auto"):
                out["auto"] += r["n"]
            if r["action"] == "auto-reply":
                out["solved"] += r["n"]
            if "escalat" in r["action"]:
                out["escalated"] += r["n"]
            out["by_intent"][r["intent"]] = out["by_intent"].get(r["intent"], 0) + r["n"]
        return out
