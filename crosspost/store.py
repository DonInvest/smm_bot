"""SQLite-реестр кросспостов: пост -> доставка на каждую площадку."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

PENDING, SENDING, DONE, FAILED, UNCERTAIN, CANCELLED = (
    "pending", "sending", "done", "failed", "uncertain", "cancelled")
TERMINAL = (DONE, FAILED, UNCERTAIN, CANCELLED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    x_parts TEXT NOT NULL,
    photo_file_id TEXT,
    image_url TEXT,
    superseded INTEGER NOT NULL DEFAULT 0,
    notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_posts_source ON posts(source_key);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    platform TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at INTEGER NOT NULL,
    remote_ids TEXT NOT NULL DEFAULT '[]',
    url TEXT,
    error TEXT,
    updated_at INTEGER NOT NULL,
    UNIQUE(post_id, platform)
);
CREATE INDEX IF NOT EXISTS ix_deliveries_due ON deliveries(status, next_at);
"""


@dataclass
class Post:
    id: int
    source_key: str
    created_at: int
    x_parts: List[str]
    photo_file_id: Optional[str]
    image_url: Optional[str]
    superseded: bool
    notified: bool


@dataclass
class Delivery:
    id: int
    post_id: int
    platform: str
    status: str
    attempts: int
    next_at: int
    remote_ids: List[str]
    url: Optional[str]
    error: Optional[str]


def _post(row) -> Post:
    return Post(row["id"], row["source_key"], row["created_at"], json.loads(row["x_parts"]),
                row["photo_file_id"], row["image_url"], bool(row["superseded"]), bool(row["notified"]))


def _delivery(row) -> Delivery:
    return Delivery(row["id"], row["post_id"], row["platform"], row["status"], row["attempts"],
                    row["next_at"], json.loads(row["remote_ids"] or "[]"), row["url"], row["error"])


class Store:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")

    def _q(self, sql: str, args: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, args)
            return cur.lastrowid

    # ---------- posts ----------
    def create_post(self, source_key: str, x_parts: List[str], photo_file_id: Optional[str],
                    platforms: List[str], send_at: int) -> int:
        now = int(time.time())
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    "INSERT INTO posts(source_key, created_at, x_parts, photo_file_id) VALUES (?,?,?,?)",
                    (source_key, now, json.dumps(x_parts, ensure_ascii=False), photo_file_id))
                pid = cur.lastrowid
                for p in platforms:
                    self._conn.execute(
                        "INSERT INTO deliveries(post_id, platform, status, next_at, updated_at) VALUES (?,?,?,?,?)",
                        (pid, p, PENDING, send_at, now))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return pid

    def get_post(self, post_id: int) -> Optional[Post]:
        rows = self._q("SELECT * FROM posts WHERE id=?", (post_id,))
        return _post(rows[0]) if rows else None

    def latest_post(self, source_key: str) -> Optional[Post]:
        rows = self._q("SELECT * FROM posts WHERE source_key=? AND superseded=0 ORDER BY id DESC LIMIT 1",
                       (source_key,))
        return _post(rows[0]) if rows else None

    def set_superseded(self, post_id: int) -> None:
        self._x("UPDATE posts SET superseded=1 WHERE id=?", (post_id,))

    def set_image_url(self, post_id: int, url: str) -> None:
        self._x("UPDATE posts SET image_url=? WHERE id=?", (url, post_id))

    def set_notified(self, post_id: int, value: bool = True) -> None:
        self._x("UPDATE posts SET notified=? WHERE id=?", (1 if value else 0, post_id))

    def posts_to_notify(self) -> List[Post]:
        rows = self._q(
            "SELECT p.* FROM posts p WHERE p.notified=0 AND p.superseded=0 AND NOT EXISTS ("
            " SELECT 1 FROM deliveries d WHERE d.post_id=p.id AND d.status IN (?,?))",
            (PENDING, SENDING))
        return [_post(r) for r in rows]

    # ---------- deliveries ----------
    def deliveries(self, post_id: int) -> List[Delivery]:
        return [_delivery(r) for r in self._q("SELECT * FROM deliveries WHERE post_id=? ORDER BY id", (post_id,))]

    def get_delivery(self, delivery_id: int) -> Optional[Delivery]:
        rows = self._q("SELECT * FROM deliveries WHERE id=?", (delivery_id,))
        return _delivery(rows[0]) if rows else None

    def claim_due(self, now: int, limit: int = 5) -> List[Delivery]:
        """Атомарно переводит созревшие pending в sending и возвращает их."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT * FROM deliveries WHERE status=? AND next_at<=? ORDER BY next_at LIMIT ?",
                    (PENDING, now, limit)).fetchall()
                for r in rows:
                    self._conn.execute(
                        "UPDATE deliveries SET status=?, attempts=attempts+1, updated_at=? WHERE id=?",
                        (SENDING, now, r["id"]))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        out = [_delivery(r) for r in rows]
        for d in out:
            d.status, d.attempts = SENDING, d.attempts + 1
        return out

    def update_delivery(self, delivery_id: int, *, status: Optional[str] = None, next_at: Optional[int] = None,
                        remote_ids: Optional[List[str]] = None, url: Optional[str] = None,
                        error: Optional[str] = None, reset_attempts: bool = False) -> None:
        sets, args = ["updated_at=?"], [int(time.time())]
        if status is not None:
            sets.append("status=?"); args.append(status)
        if next_at is not None:
            sets.append("next_at=?"); args.append(next_at)
        if remote_ids is not None:
            sets.append("remote_ids=?"); args.append(json.dumps(remote_ids))
        if url is not None:
            sets.append("url=?"); args.append(url)
        if error is not None:
            sets.append("error=?"); args.append(error[:1000])
        if reset_attempts:
            sets.append("attempts=0")
        args.append(delivery_id)
        self._x(f"UPDATE deliveries SET {', '.join(sets)} WHERE id=?", tuple(args))

    def recover_interrupted(self) -> int:
        """После рестарта: sending -> uncertain (мог уйти частично, автоповтор опасен)."""
        rows = self._q("SELECT id FROM deliveries WHERE status=?", (SENDING,))
        for r in rows:
            self.update_delivery(r["id"], status=UNCERTAIN,
                                 error="бот перезапущен во время отправки, проверь площадку и нажми «повторить»")
        return len(rows)
