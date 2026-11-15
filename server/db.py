"""Tiny SQLite-backed job store. Jobs survive service restarts."""
import json
import sqlite3
import threading
import time

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_JSON_COLS = ("outputs", "summary")


def init(path):
    global _conn
    _conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            created REAL NOT NULL,
            filename TEXT NOT NULL,
            input_name TEXT NOT NULL,
            media TEXT NOT NULL,
            size INTEGER NOT NULL,
            model TEXT NOT NULL,
            output TEXT NOT NULL,
            conf REAL NOT NULL,
            status TEXT NOT NULL,
            processed INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            fps REAL,
            message TEXT,
            error TEXT,
            started REAL,
            finished REAL,
            outputs TEXT NOT NULL DEFAULT '[]',
            summary TEXT NOT NULL DEFAULT '{}'
        )"""
    )
    _conn.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created)")


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for c in _JSON_COLS:
        d[c] = json.loads(d[c])
    return d


def _exec(sql, args=()):
    with _lock:
        return _conn.execute(sql, args)


def create(**job):
    job.setdefault("created", time.time())
    job.setdefault("status", "queued")
    cols = ", ".join(job)
    marks = ", ".join("?" for _ in job)
    _exec(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(job.values()))
    return get(job["id"])


def get(job_id):
    with _lock:
        return _row(_conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def status(job_id):
    with _lock:
        r = _conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return r[0] if r else None


def list_jobs(limit=200):
    with _lock:
        rows = _conn.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def update(job_id, **fields):
    for c in _JSON_COLS:
        if c in fields:
            fields[c] = json.dumps(fields[c])
    sets = ", ".join(f"{k} = ?" for k in fields)
    _exec(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))


def transition(job_id, from_statuses, **fields):
    """Atomically update a job only if it is currently in one of from_statuses."""
    sets = ", ".join(f"{k} = ?" for k in fields)
    marks = ", ".join("?" for _ in from_statuses)
    cur = _exec(
        f"UPDATE jobs SET {sets} WHERE id = ? AND status IN ({marks})",
        (*fields.values(), job_id, *from_statuses),
    )
    return cur.rowcount > 0


def next_queued():
    with _lock:
        return _row(
            _conn.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY created LIMIT 1").fetchone()
        )


def counts():
    with _lock:
        rows = _conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
    return {s: n for s, n in rows}


def recover_after_restart():
    """Jobs interrupted by a restart go back in the queue (they restart from scratch)."""
    _exec(
        "UPDATE jobs SET status = 'queued', processed = 0, fps = NULL, started = NULL, "
        "message = 'Requeued after service restart' WHERE status = 'running'"
    )
    _exec("UPDATE jobs SET status = 'canceled', finished = ? WHERE status = 'canceling'", (time.time(),))


def delete(job_id):
    _exec("DELETE FROM jobs WHERE id = ?", (job_id,))


def finished_before(ts):
    with _lock:
        rows = _conn.execute(
            "SELECT id FROM jobs WHERE status IN ('done', 'failed', 'canceled') AND COALESCE(finished, created) < ?",
            (ts,),
        ).fetchall()
    return [r[0] for r in rows]
