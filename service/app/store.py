"""SQLite persistence — users, sessions, jobs, usage. Real, not a stub."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Optional

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  plan TEXT NOT NULL DEFAULT 'free',      -- free | pro
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_tokens (
  token TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  kind TEXT NOT NULL,                     -- walk | shots
  status TEXT NOT NULL,                   -- queued|running|done|failed|capped
  output TEXT DEFAULT '',
  detail TEXT DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
  user_id TEXT NOT NULL,
  kind TEXT NOT NULL,                     -- generation | shot
  n INTEGER NOT NULL DEFAULT 1,
  at INTEGER NOT NULL
);

-- ---- V2 engagement layer (hosted service owns this, not Core) --------------
-- Stable demo identity. IDs are minted with secrets.token_urlsafe (>= 16 bytes)
-- so they are globally safe for hosted public URLs.
CREATE TABLE IF NOT EXISTS demos (
  id TEXT PRIMARY KEY,
  owner_uid TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  draft_spec TEXT NOT NULL DEFAULT '{}',  -- mutable DemoSpec JSON (the working copy)
  published_version_id TEXT,              -- what prospects currently see (nullable)
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  FOREIGN KEY(owner_uid) REFERENCES users(id)
);

-- Immutable published versions. Publishing NEVER mutates a prior row — a new
-- row is frozen from the draft, so agent edits can't silently change what a
-- prospect is watching.
CREATE TABLE IF NOT EXISTS demo_versions (
  id TEXT PRIMARY KEY,
  demo_id TEXT NOT NULL,
  version_number INTEGER NOT NULL,        -- per-demo monotonic, starts at 1
  spec_json TEXT NOT NULL,                -- frozen DemoSpec JSON
  steps_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  UNIQUE(demo_id, version_number),
  FOREIGN KEY(demo_id) REFERENCES demos(id)
);

-- Anonymous viewer sessions. Opaque server-recognized session key (the client
-- supplies a random UUID — no PII). attribution_token is the Frontman opaque
-- send token, stored opaquely and NEVER returned to any public caller.
CREATE TABLE IF NOT EXISTS viewer_sessions (
  id TEXT PRIMARY KEY,
  demo_id TEXT NOT NULL,
  version_id TEXT,
  device_class TEXT NOT NULL DEFAULT 'desktop',
  source_json TEXT NOT NULL DEFAULT '{}', -- UTM/source metadata (bounded)
  attribution_token TEXT,                 -- Frontman opaque token, opaque only
  started_at INTEGER NOT NULL,
  last_seen INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_viewer_sessions_demo ON viewer_sessions(demo_id, started_at);

-- Raw viewer events (the capturd:event vocabulary, server-validated).
CREATE TABLE IF NOT EXISTS demo_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  demo_id TEXT NOT NULL,
  version_id TEXT,
  event TEXT NOT NULL,                    -- validated vocabulary only
  step_id TEXT,
  branch_id TEXT,
  choice_id TEXT,
  cta_id TEXT,
  elapsed_ms INTEGER NOT NULL DEFAULT 0,
  device_class TEXT,
  at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_demo_events_demo ON demo_events(demo_id, event, at);
CREATE INDEX IF NOT EXISTS ix_demo_events_session ON demo_events(session_id);
"""


@contextmanager
def _db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    config.ensure_dirs()
    with _db() as c:
        c.executescript(_SCHEMA)


# ---- users ------------------------------------------------------------------

def upsert_user(email: str) -> dict:
    email = email.strip().lower()
    with _db() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if row:
            return dict(row)
        uid = secrets.token_hex(8)
        c.execute("INSERT INTO users(id,email,plan,created_at) VALUES(?,?,?,?)",
                  (uid, email, "free", int(time.time())))
        return {"id": uid, "email": email, "plan": "free", "created_at": int(time.time())}


def get_user(uid: str) -> Optional[dict]:
    with _db() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None


def get_user_by_email(email: str) -> Optional[dict]:
    with _db() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        return dict(row) if row else None


def set_plan(uid: str, plan: str) -> None:
    with _db() as c:
        c.execute("UPDATE users SET plan=? WHERE id=?", (plan, uid))


# ---- sessions ---------------------------------------------------------------

def new_session(uid: str) -> str:
    token = secrets.token_urlsafe(32)
    with _db() as c:
        c.execute("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)",
                  (token, uid, int(time.time())))
    return token


def user_for_session(token: str) -> Optional[dict]:
    if not token:
        return None
    with _db() as c:
        row = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
            (token,)).fetchone()
        return dict(row) if row else None


def drop_session(token: str) -> None:
    with _db() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


# ---- jobs + usage -----------------------------------------------------------

def record_job(job_id: str, uid: str, kind: str, status: str,
               output: str = "", detail: str = "") -> None:
    with _db() as c:
        c.execute(
            "INSERT OR REPLACE INTO jobs(id,user_id,kind,status,output,detail,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (job_id, uid, kind, status, output, detail, int(time.time())))


def get_job(job_id: str) -> Optional[dict]:
    with _db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def list_jobs(uid: str, limit: int = 30) -> list[dict]:
    """A user's recent jobs, newest first — powers the studio gallery."""
    with _db() as c:
        rows = c.execute(
            "SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (uid, limit)).fetchall()
        return [dict(r) for r in rows]


def add_usage(uid: str, kind: str, n: int = 1) -> None:
    with _db() as c:
        c.execute("INSERT INTO usage(user_id,kind,n,at) VALUES(?,?,?,?)",
                  (uid, kind, n, int(time.time())))


def usage_count(uid: str, kind: str) -> int:
    with _db() as c:
        row = c.execute("SELECT COALESCE(SUM(n),0) AS t FROM usage WHERE user_id=? AND kind=?",
                        (uid, kind)).fetchone()
        return int(row["t"])


# ---- render caps — atomic gates (race-proof Free slot + concurrency + rate) -
# The old Free gate was a check (here, in the route) plus an increment (in the
# post-completion task), two transactions up to 600s apart. N concurrent Free
# calls all read count==0 and passed. These primitives do the check-and-reserve
# in ONE BEGIN IMMEDIATE transaction, so a caller only ever sees slots a
# concurrent caller already committed. Every cap applies to Pro too.


def try_acquire(uid: str, kind: str, *, limit: int,
                window_seconds: Optional[int] = None) -> bool:
    """Atomically reserve one unit of (uid, kind) iff its count within the
    sliding window is below *limit*. True if reserved, False if at/over.

    *window_seconds* None ⇒ lifetime (no lower bound on ``at``). The SELECT and
    INSERT run inside ``BEGIN IMMEDIATE`` so concurrent callers serialize on the
    database write lock. Release a reservation with :func:`refund`.
    """
    now = int(time.time())
    since = now - window_seconds if window_seconds else 0
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None                  # autocommit; we manage the txn
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT COALESCE(SUM(n),0) AS t FROM usage "
            "WHERE user_id=? AND kind=? AND at >= ?",
            (uid, kind, since)).fetchone()
        if int(row["t"]) >= limit:
            conn.execute("ROLLBACK")
            return False
        conn.execute("INSERT INTO usage(user_id,kind,n,at) VALUES(?,?,?,?)",
                     (uid, kind, 1, now))
        conn.execute("COMMIT")
        return True
    except sqlite3.OperationalError:
        # lock not available within the busy timeout, or similar — fail safe:
        # never hand out a slot we couldn't prove was under the limit.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return False
    finally:
        conn.close()


def refund(uid: str, kind: str, n: int = 1) -> None:
    """Release *n* units previously reserved by :func:`try_acquire` — append a
    negative row so the running SUM drops back. Used when a reserved render
    fails (a Free user shouldn't lose their one slot to a render that never
    delivered) or when a later gate rejects a request that already reserved."""
    with _db() as c:
        c.execute("INSERT INTO usage(user_id,kind,n,at) VALUES(?,?,?,?)",
                  (uid, kind, -n, int(time.time())))


def window_retry_after(uid: str, kind: str, window_seconds: int) -> int:
    """Seconds until the oldest in-window (uid, kind) event ages out — a
    best-effort ``Retry-After`` for a rate-limited request (>= 1)."""
    now = int(time.time())
    since = now - window_seconds
    with _db() as c:
        row = c.execute(
            "SELECT MIN(at) AS oldest FROM usage "
            "WHERE user_id=? AND kind=? AND at >= ?",
            (uid, kind, since)).fetchone()
    oldest = int(row["oldest"]) if row and row["oldest"] is not None else now
    return max(1, (oldest + window_seconds) - now)


def try_queue_job(uid: str, kind: str, *, max_concurrent: int) -> Optional[str]:
    """Atomically create a 'queued' job for *uid* iff they hold fewer than
    *max_concurrent* in-flight jobs (status queued|running). Returns the new
    job_id, or None at/over the cap.

    Creating the queued job IS the concurrency reservation — the count and the
    insert run in one ``BEGIN IMMEDIATE`` transaction, so two simultaneous
    requests can't both read "1 in flight" and both queue a third. ``record_job``
    later moves the status to running/done/failed/capped; only queued|running
    count against the cap, so a finished render frees its slot.
    """
    now = int(time.time())
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT COUNT(*) AS t FROM jobs "
            "WHERE user_id=? AND status IN ('queued','running')",
            (uid,)).fetchone()
        if int(row["t"]) >= max_concurrent:
            conn.execute("ROLLBACK")
            return None
        job_id = secrets.token_hex(6)            # 12 hex chars, like the rest
        conn.execute(
            "INSERT INTO jobs(id,user_id,kind,status,output,detail,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (job_id, uid, kind, "queued", "", "", now))
        conn.execute("COMMIT")
        return job_id
    except sqlite3.OperationalError:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return None
    finally:
        conn.close()


# ---- MCP tokens -------------------------------------------------------------
# The endpoint used to be keyed on the raw user id, which is guessable from any
# response that leaks it. These are random, revocable, and one per user.

def mcp_token_for(user_id: str) -> str:
    """Return this user's MCP token, minting one on first use."""
    with _db() as db:
        row = db.execute("SELECT token FROM mcp_tokens WHERE user_id=?", (user_id,)).fetchone()
        if row:
            return row[0]
        token = secrets.token_urlsafe(24)
        db.execute("INSERT INTO mcp_tokens(token,user_id,created_at) VALUES(?,?,?)",
                   (token, user_id, int(time.time())))
        db.commit()
        return token


def user_for_mcp_token(token: str) -> dict | None:
    if not token:
        return None
    with _db() as db:
        row = db.execute(
            "SELECT u.id,u.email,u.plan FROM mcp_tokens m JOIN users u ON u.id=m.user_id "
            "WHERE m.token=?", (token,)).fetchone()
    return {"id": row[0], "email": row[1], "plan": row[2]} if row else None


def revoke_mcp_token(user_id: str) -> None:
    with _db() as db:
        db.execute("DELETE FROM mcp_tokens WHERE user_id=?", (user_id,))
        db.commit()


# ---- V2 engagement layer: demos, versions, viewer sessions, events -----------
# Hosted-service owned (service/app). Core (capturd/) never imports these.

_VALID_EVENTS = frozenset({
    "demo_open", "demo_start",
    "step_view", "step_complete", "step_replay",
    "branch_view", "branch_select",
    "cta_view", "cta_click",
    "demo_complete", "demo_exit",
    "viewer_pause", "viewer_resume",
})


def valid_event(name: str) -> bool:
    return name in _VALID_EVENTS


def _mint(prefix: str, nbytes: int = 16) -> str:
    """Globally-safe public identifier: prefix + urlsafe random."""
    return f"{prefix}_{secrets.token_urlsafe(nbytes)}"


def create_demo(uid: str, title: str, draft_spec: str) -> dict:
    now = int(time.time())
    demo_id = _mint("dm")
    with _db() as c:
        c.execute(
            "INSERT INTO demos(id,owner_uid,title,draft_spec,published_version_id,"
            "created_at,updated_at) VALUES(?,?,?,?,NULL,?,?)",
            (demo_id, uid, str(title or "")[:200], draft_spec, now, now))
    return {"id": demo_id, "title": title, "published_version_id": None}


def get_demo(demo_id: str) -> Optional[dict]:
    with _db() as c:
        row = c.execute("SELECT * FROM demos WHERE id=?", (demo_id,)).fetchone()
        return dict(row) if row else None


def list_demos(uid: str, limit: int = 100) -> list[dict]:
    with _db() as c:
        rows = c.execute(
            "SELECT id,title,published_version_id,created_at,updated_at FROM demos "
            "WHERE owner_uid=? ORDER BY updated_at DESC LIMIT ?", (uid, limit)).fetchall()
        return [dict(r) for r in rows]


def save_draft(demo_id: str, draft_spec: str) -> bool:
    """Update the mutable draft. Published versions are never touched."""
    with _db() as c:
        cur = c.execute(
            "UPDATE demos SET draft_spec=?, updated_at=? WHERE id=?",
            (draft_spec, int(time.time()), demo_id))
        return cur.rowcount > 0


def publish_version(demo_id: str, spec_json: str, steps_count: int) -> Optional[dict]:
    """Freeze the current spec into a new immutable version and point the demo's
    published pointer at it. Returns the version row (or None if demo missing)."""
    now = int(time.time())
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        demo = conn.execute("SELECT id FROM demos WHERE id=?", (demo_id,)).fetchone()
        if not demo:
            conn.execute("ROLLBACK")
            return None
        row = conn.execute(
            "SELECT COALESCE(MAX(version_number),0)+1 AS nxt FROM demo_versions "
            "WHERE demo_id=?", (demo_id,)).fetchone()
        n = int(row["nxt"])
        vid = _mint("dv")
        conn.execute(
            "INSERT INTO demo_versions(id,demo_id,version_number,spec_json,steps_count,created_at)"
            " VALUES(?,?,?,?,?,?)", (vid, demo_id, n, spec_json, int(steps_count), now))
        conn.execute(
            "UPDATE demos SET published_version_id=?, updated_at=? WHERE id=?",
            (vid, now, demo_id))
        conn.execute("COMMIT")
        return {"id": vid, "demo_id": demo_id, "version_number": n,
                "steps_count": int(steps_count), "created_at": now}
    except sqlite3.OperationalError:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return None
    finally:
        conn.close()


def get_version(version_id: str) -> Optional[dict]:
    with _db() as c:
        row = c.execute("SELECT * FROM demo_versions WHERE id=?", (version_id,)).fetchone()
        return dict(row) if row else None


def get_published(demo_id: str) -> Optional[dict]:
    """The version a prospect currently sees, or None (nothing published)."""
    with _db() as c:
        row = c.execute(
            "SELECT v.* FROM demos d JOIN demo_versions v ON v.id=d.published_version_id "
            "WHERE d.id=?", (demo_id,)).fetchone()
        return dict(row) if row else None


def list_versions(demo_id: str) -> list[dict]:
    with _db() as c:
        rows = c.execute(
            "SELECT id,demo_id,version_number,steps_count,created_at FROM demo_versions "
            "WHERE demo_id=? ORDER BY version_number DESC", (demo_id,)).fetchall()
        return [dict(r) for r in rows]


def restore_version_to_draft(demo_id: str, version_id: str) -> bool:
    """Copy an old version's spec back into the mutable draft (history intact)."""
    ver = get_version(version_id)
    if not ver or ver["demo_id"] != demo_id:
        return False
    return save_draft(demo_id, ver["spec_json"])


def upsert_viewer_session(session_id: str, demo_id: str, version_id: Optional[str],
                          device_class: str = "desktop",
                          source: Optional[dict] = None,
                          attribution_token: Optional[str] = None) -> dict:
    """Insert or refresh an anonymous viewer session. The session key is the
    client-generated opaque UUID (validated by the route); nothing here is PII."""
    now = int(time.time())
    with _db() as c:
        row = c.execute("SELECT * FROM viewer_sessions WHERE id=?", (session_id,)).fetchone()
        if row:
            c.execute("UPDATE viewer_sessions SET last_seen=? WHERE id=?",
                      (now, session_id))
            return dict(row)
        c.execute(
            "INSERT INTO viewer_sessions(id,demo_id,version_id,device_class,source_json,"
            "attribution_token,started_at,last_seen) VALUES(?,?,?,?,?,?,?,?)",
            (session_id, demo_id, version_id,
             str(device_class or "desktop")[:16],
             json.dumps(_bounded_source(source)),
             (str(attribution_token)[:128] if attribution_token else None),
             now, now))
        return {"id": session_id, "demo_id": demo_id, "version_id": version_id,
                "attribution_token": attribution_token, "started_at": now}


def _bounded_source(source: Optional[dict]) -> dict:
    """Keep only short-string UTM/source fields; drop everything else."""
    if not isinstance(source, dict):
        return {}
    keep = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "ref")
    out = {}
    for k in keep:
        v = source.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()[:120]
    return out


def get_viewer_session(session_id: str) -> Optional[dict]:
    with _db() as c:
        row = c.execute("SELECT * FROM viewer_sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None


def record_events(session: dict, events: list[dict]) -> int:
    """Bulk-insert validated viewer events. Returns the count stored."""
    now = int(time.time())
    rows = []
    for ev in events:
        rows.append((
            session["id"], session["demo_id"], session.get("version_id"),
            ev["event"], ev.get("step_id"), ev.get("branch_id"),
            ev.get("choice_id"), ev.get("cta_id"),
            int(ev.get("elapsed_ms") or 0),
            session.get("device_class"), now,
        ))
    with _db() as c:
        c.executemany(
            "INSERT INTO demo_events(session_id,demo_id,version_id,event,step_id,"
            "branch_id,choice_id,cta_id,elapsed_ms,device_class,at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)
