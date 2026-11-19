"""SQLite video queue with owner-enforced status transitions.

Statuses: idea, scripted, edited, approved, rejected, published, measured.
Each actor may only make the transitions listed in TRANSITIONS. Anything else raises.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

STATUSES = ("idea", "scripted", "edited", "approved", "rejected", "published", "measured")

# actor -> allowed (from, to). None as "from" means creating a new row.
TRANSITIONS: dict[str, set[tuple[str | None, str]]] = {
    "writer": {(None, "idea"), ("idea", "scripted"), ("rejected", "idea")},
    "editor": {("scripted", "idea")},             # footage can't support the script
    "render": {("scripted", "edited")},
    "reviewer": {("edited", "approved"), ("edited", "rejected")},
    "upload": {("approved", "published")},
    "analyst": {("published", "measured")},
    # code-enforced safety gates (creator paused/removed, bad clip bounds, render failure)
    "safety": {(s, "rejected") for s in ("idea", "scripted", "edited", "approved")},
}

COLUMNS = [
    "id", "status", "creator", "source_url", "credit_line", "moment", "hook", "script", "edit_spec",
    "source_file", "clip_start", "clip_end", "output_file", "title", "description", "review_notes",
    "youtube_id", "publish_at", "created_at", "updated_at",
    # additions beyond the brief, all optional:
    "hook_type", "moment_type", "structure", "attempts", "owner_ok_at", "rejected_by",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL CHECK (status IN ('idea','scripted','edited','approved','rejected','published','measured')),
    creator TEXT NOT NULL,
    source_url TEXT,
    credit_line TEXT,
    moment TEXT,
    hook TEXT,
    script TEXT,
    edit_spec TEXT,
    source_file TEXT,
    clip_start REAL,
    clip_end REAL,
    output_file TEXT,
    title TEXT,
    description TEXT,
    review_notes TEXT,
    youtube_id TEXT,
    publish_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    hook_type TEXT,
    moment_type TEXT,
    structure TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    owner_ok_at TEXT,
    rejected_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL,
    actor TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    note TEXT,
    at TEXT NOT NULL
);
"""


class TransitionError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Queue:
    def __init__(self, path: Path):
        self.path = Path(path)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ---------- reads ----------
    def get(self, vid: int) -> dict[str, Any] | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
        return dict(r) if r else None

    def by_status(self, status: str, order: str = "id ASC") -> list[dict[str, Any]]:
        if order not in ("id ASC", "id DESC", "updated_at ASC", "updated_at DESC", "publish_at ASC"):
            raise ValueError(order)
        with self.conn() as c:
            rows = c.execute(f"SELECT * FROM videos WHERE status=? ORDER BY {order}", (status,)).fetchall()
        return [dict(r) for r in rows]

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        if not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("read-only")
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def recent_published(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM videos WHERE status IN ('published','measured') ORDER BY publish_at DESC, id DESC LIMIT ?",
            (limit,))

    # ---------- writes ----------
    def create(self, actor: str, **fields: Any) -> int:
        if (None, "idea") not in TRANSITIONS.get(actor, set()):
            raise TransitionError(f"{actor} may not create rows")
        fields = {k: v for k, v in fields.items() if k in COLUMNS and k not in ("id", "status", "created_at", "updated_at")}
        if not fields.get("creator"):
            raise TransitionError("creator is required")
        if isinstance(fields.get("edit_spec"), (dict, list)):
            fields["edit_spec"] = json.dumps(fields["edit_spec"])
        now = _now()
        cols = ["status", "created_at", "updated_at", *fields.keys()]
        vals = ["idea", now, now, *fields.values()]
        with self.conn() as c:
            cur = c.execute(f"INSERT INTO videos ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            vid = cur.lastrowid
            c.execute("INSERT INTO transitions (video_id, actor, from_status, to_status, note, at) VALUES (?,?,?,?,?,?)",
                      (vid, actor, None, "idea", "created", now))
        return int(vid)

    def update_fields(self, actor: str, vid: int, allowed_status: str | tuple[str, ...], **fields: Any) -> None:
        """Update data fields (never status) on a row the actor currently owns."""
        allowed = (allowed_status,) if isinstance(allowed_status, str) else allowed_status
        fields = {k: v for k, v in fields.items() if k in COLUMNS and k not in ("id", "status", "created_at")}
        if isinstance(fields.get("edit_spec"), (dict, list)):
            fields["edit_spec"] = json.dumps(fields["edit_spec"])
        fields["updated_at"] = _now()
        with self.conn() as c:
            r = c.execute("SELECT status FROM videos WHERE id=?", (vid,)).fetchone()
            if not r:
                raise TransitionError(f"no row {vid}")
            if r["status"] not in allowed:
                raise TransitionError(f"{actor} may not edit row {vid} in status {r['status']}")
            sets = ",".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE videos SET {sets} WHERE id=?", (*fields.values(), vid))

    def transition(self, actor: str, vid: int, to_status: str, note: str = "", **fields: Any) -> None:
        if to_status not in STATUSES:
            raise TransitionError(f"unknown status {to_status}")
        fields = {k: v for k, v in fields.items() if k in COLUMNS and k not in ("id", "status", "created_at")}
        if isinstance(fields.get("edit_spec"), (dict, list)):
            fields["edit_spec"] = json.dumps(fields["edit_spec"])
        now = _now()
        fields["updated_at"] = now
        with self.conn() as c:
            r = c.execute("SELECT status FROM videos WHERE id=?", (vid,)).fetchone()
            if not r:
                raise TransitionError(f"no row {vid}")
            frm = r["status"]
            if (frm, to_status) not in TRANSITIONS.get(actor, set()):
                raise TransitionError(f"{actor} may not move row {vid} from {frm} to {to_status}")
            sets = ",".join(f"{k}=?" for k in ["status", *fields.keys()])
            c.execute(f"UPDATE videos SET {sets} WHERE id=? AND status=?", (to_status, *fields.values(), vid, frm))
            c.execute("INSERT INTO transitions (video_id, actor, from_status, to_status, note, at) VALUES (?,?,?,?,?,?)",
                      (vid, actor, frm, to_status, note[:2000], now))

    def set_owner_ok(self, vid: int) -> None:
        """Owner approval (scripts/approve.py). Not a status change."""
        with self.conn() as c:
            r = c.execute("SELECT status FROM videos WHERE id=?", (vid,)).fetchone()
            if not r or r["status"] != "approved":
                raise TransitionError(f"row {vid} is not in approved")
            c.execute("UPDATE videos SET owner_ok_at=?, updated_at=? WHERE id=?", (_now(), _now(), vid))
