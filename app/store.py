"""Durable task + payment state (SQLite). Every write is transactional so a crash
mid-task can be resumed without repeating a payment."""
import json, os, sqlite3, threading, time
from contextlib import contextmanager

DB_PATH = os.environ.get("STATE_DB", os.path.join(os.path.dirname(__file__), "..", "state.db"))
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id      TEXT PRIMARY KEY,
  request_hash TEXT NOT NULL,
  package      TEXT,                   -- JSON of every document seen for this task
  status       TEXT NOT NULL,          -- received | decided | executing | done | failed
  decision     TEXT,                   -- JSON
  result       TEXT,                   -- JSON returned to caller
  attempts     INTEGER NOT NULL DEFAULT 0,
  created_at   REAL NOT NULL,
  updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
  invoice_key  TEXT PRIMARY KEY,       -- vendor|invoice_number, one payment per invoice ever
  task_id      TEXT NOT NULL,
  amount       REAL NOT NULL,
  currency     TEXT,
  status       TEXT NOT NULL,          -- pending | paid | failed
  receipt      TEXT,                   -- JSON receipt from the payment rail
  created_at   REAL NOT NULL,
  updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS a2a_tasks (
  task_id TEXT PRIMARY KEY, context_id TEXT, sub_ids TEXT, history TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT, kind TEXT, detail TEXT, at REAL
);
"""


def _connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=FULL")
    return c


_conn = None


def conn():
    global _conn
    if _conn is None:
        _conn = _connect()
        _conn.executescript(SCHEMA)
    return _conn


@contextmanager
def tx():
    """Serialized write transaction."""
    with _lock:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def log(c, task_id, kind, detail):
    c.execute("INSERT INTO events(task_id,kind,detail,at) VALUES(?,?,?,?)",
              (task_id, kind, json.dumps(detail, default=str), time.time()))


def get_task(task_id):
    with _lock:
        r = conn().execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("decision", "result"):
        d[k] = json.loads(d[k]) if d[k] else None
    return d


def get_payment(invoice_key):
    with _lock:
        r = conn().execute("SELECT * FROM payments WHERE invoice_key=?", (invoice_key,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["receipt"] = json.loads(d["receipt"]) if d["receipt"] else None
    return d


def list_tasks(limit=100):
    rows = conn().execute("SELECT task_id FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [get_task(r["task_id"]) for r in rows]


def list_payments():
    return [get_payment(r["invoice_key"]) for r in conn().execute("SELECT invoice_key FROM payments").fetchall()]


def reset():
    with tx() as c:
        for t in ("tasks", "payments", "events", "a2a_tasks"):
            c.execute(f"DELETE FROM {t}")
