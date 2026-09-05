"""Captur'd sqlite->Postgres migrator tests.

Covers two layers the reviewer asked for:

  * Direct unit tests for the real ``PostgresSink`` write path — the SQL
    generation (``sql_for``) and param mapping (``params_for``) — that run with
    NO Postgres server. These are what make CI prove the generated statements
    are correct instead of trusting an unexercised code path.
  * A real-Postgres integration test (auto-skipped when
    ``CAPTURD_TEST_POSTGRES_URL`` is unset or psycopg isn't installed) that
    migrates actual rows and proves counts + idempotency against a live driver.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parent.parent / "service"
sys.path.insert(0, str(SERVICE))

from app import store  # noqa: E402
from scripts.migrate_sqlite_to_postgres import (  # noqa: E402
    MemorySink,
    PostgresSink,
    _usage_key,
    migrate,
    params_for,
    sql_for,
)


def _make_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(store._SCHEMA)
    seed = [
        ("INSERT INTO users(id,email,plan,created_at) VALUES(?,?,?,?)",
         ("u1", "a@b.co", "free", 100)),
        ("INSERT INTO users(id,email,plan,created_at) VALUES(?,?,?,?)",
         ("u2", "c@d.co", "pro", 200)),
        ("INSERT INTO mcp_tokens(token,user_id,created_at) VALUES(?,?,?)",
         ("tok1", "u1", 100)),
        ("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)",
         ("s1", "u2", 200)),
        ("INSERT INTO jobs(id,user_id,kind,status,output,detail,created_at) VALUES(?,?,?,?,?,?,?)",
         ("j1", "u1", "walk", "done", "out", "ok", 300)),
        # Two DISTINCT usage rows that share every payload column (same second,
        # same n). They must BOTH survive — rowid makes the keys distinct.
        ("INSERT INTO usage(user_id,kind,n,at) VALUES(?,?,?,?)",
         ("u1", "generation", 1, 400)),
        ("INSERT INTO usage(user_id,kind,n,at) VALUES(?,?,?,?)",
         ("u1", "generation", 1, 400)),
    ]
    for sql, params in seed:
        conn.execute(sql, params)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Direct unit tests for the PostgresSink write path (no server needed)
# ---------------------------------------------------------------------------


def test_sql_for_all_tables():
    assert sql_for("users") == (
        "INSERT INTO users (id, email, plan, created_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING")
    assert sql_for("mcp_tokens") == (
        "INSERT INTO mcp_tokens (token, user_id, created_at) VALUES (%s, %s, %s) "
        "ON CONFLICT (token) DO NOTHING")
    assert sql_for("sessions") == (
        "INSERT INTO sessions (token, user_id, created_at) VALUES (%s, %s, %s) "
        "ON CONFLICT (token) DO NOTHING")
    assert sql_for("jobs") == (
        "INSERT INTO jobs (id, user_id, kind, status, output, detail, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING")
    assert sql_for("usage") == (
        "INSERT INTO usage (_key, user_id, kind, n, at) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (_key) DO NOTHING")


def test_sql_for_users_conflicts_on_pk_not_email():
    """A rerun after a user's email changed must NOT hard-fail on a duplicate
    PK. Conflict on the PK (id), never on the mutable email column."""
    s = sql_for("users")
    assert "ON CONFLICT (email)" not in s
    assert "ON CONFLICT (id)" in s


def test_params_for_matches_sql_placeholders():
    # 1 placeholder per column, in the SAME order as sql_for's column list.
    users_row = {"id": "u1", "email": "a@b.co", "plan": "free", "created_at": 100}
    assert params_for("users", users_row) == ("u1", "a@b.co", "free", 100)

    tok_row = {"token": "tok1", "user_id": "u1", "created_at": 100}
    assert params_for("mcp_tokens", tok_row) == ("tok1", "u1", 100)
    assert params_for("sessions", tok_row) == ("tok1", "u1", 100)

    jobs_row = {"id": "j1", "user_id": "u1", "kind": "walk", "status": "done",
                "output": "out", "detail": "ok", "created_at": 300}
    assert params_for("jobs", jobs_row) == ("j1", "u1", "walk", "done", "out", "ok", 300)

    usage_row = {"_key": "k", "user_id": "u1", "kind": "generation", "n": 1, "at": 400}
    assert params_for("usage", usage_row) == ("k", "u1", "generation", 1, 400)


def test_usage_key_includes_rowid_so_distinct_events_survive():
    # Two events identical in every payload column (same second, same n) but
    # distinct rows must get DISTINCT keys — otherwise append-only usage/billing
    # is silently collapsed and undercounted.
    a = {"_rowid": 1, "user_id": "u1", "kind": "generation", "n": 1, "at": 400}
    b = {"_rowid": 2, "user_id": "u1", "kind": "generation", "n": 1, "at": 400}
    assert a["_rowid"] != b["_rowid"]
    assert _usage_key(a) != _usage_key(b)
    # ...but the same row (same rowid) maps to the same key, so reruns stay idempotent.
    assert _usage_key(a) == _usage_key(dict(a))


# ---------------------------------------------------------------------------
# Memory-sink migrate semantics
# ---------------------------------------------------------------------------


def test_migrate_counts_and_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "capturd.sqlite3"
    _make_sqlite(db_path).close()

    sink = MemorySink()
    first = migrate(db_path, sink)
    assert first["users"]["imported"] == 2
    assert first["usage"]["source"] == 2
    assert first["usage"]["imported"] == 2          # distinct rows preserved
    assert first["usage"]["duplicates"] == 0
    assert first["jobs"]["source"] == 1
    assert first["mcp_tokens"]["source"] == 1
    assert first["sessions"]["source"] == 1

    second = migrate(db_path, sink)
    for table, r in second.items():
        assert r["imported"] == 0, table             # idempotent re-run
        assert r["resulting"] == first[table]["resulting"], table


def test_dry_run_writes_nothing(tmp_path: Path):
    db_path = tmp_path / "capturd.sqlite3"
    _make_sqlite(db_path).close()
    sink = MemorySink()
    report = migrate(db_path, sink, dry_run=True)
    assert report["users"]["imported"] == 2           # counts reported
    assert len(sink.rows) == 0                        # nothing written


# ---------------------------------------------------------------------------
# Config: portable path defaults + env overrides (subprocess re-imports config)
# ---------------------------------------------------------------------------


def _run_config(**env_extra) -> dict:
    """Import config in a fresh subprocess and dump the path facts we assert on."""
    env = {**os.environ}
    for k in ("CAPTURD_VAULT_DIR", "CAPTURD_DATA_DIR", "CAPTURD_JOBS_DIR"):
        env.pop(k, None)
    env.update({k: v for k, v in env_extra.items() if v is not None})
    code = (
        "import json, os, app.config as c; print(json.dumps({"
        "'os': os.name, 'data': str(c.DATA_DIR), 'jobs': str(c.JOBS_DIR), "
        "'vault': str(c.VAULT), 'default_data': str(c._DEFAULT_DATA_DIR), "
        "'default_vault': str(c._DEFAULT_VAULT)}))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True,
        cwd=str(SERVICE),
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def _norm(p: str) -> str:
    return Path(p).as_posix()


def test_config_platform_defaults():
    """No env: DATA_DIR/JOBS_DIR/VAULT take the platform defaults, and JOBS_DIR
    lives under DATA_DIR."""
    cfg = _run_config()
    assert cfg["data"] == cfg["default_data"]
    assert cfg["vault"] == cfg["default_vault"]
    assert Path(cfg["jobs"]) == Path(cfg["data"]) / "jobs"


def test_config_joins_override_tracks_data_dir():
    """CAPTURD_DATA_DIR override must move JOBS_DIR under it (CodeAnt finding) —
    DB and job artifacts share one data root instead of splitting across two."""
    cfg = _run_config(CAPTURD_DATA_DIR="/tmp/capturd-test")
    assert _norm(cfg["data"]) == "/tmp/capturd-test"
    assert _norm(cfg["jobs"]) == "/tmp/capturd-test/jobs"


def test_config_jobs_dir_override_still_wins():
    cfg = _run_config(CAPTURD_DATA_DIR="/tmp/capturd-test", CAPTURD_JOBS_DIR="/custom/jobs")
    assert _norm(cfg["jobs"]) == "/custom/jobs"
    assert _norm(cfg["data"]) == "/tmp/capturd-test"


def test_config_vault_dir_override_wins():
    """CAPTURD_VAULT_DIR override is honored (reviewer's medium finding)."""
    cfg = _run_config(CAPTURD_VAULT_DIR="/tmp/vault-test")
    assert _norm(cfg["vault"]) == "/tmp/vault-test"


# ---------------------------------------------------------------------------
# Real Postgres integration test (auto-skipped without a PG URL)
# ---------------------------------------------------------------------------

_PG_TEST_URL = os.environ.get("CAPTURD_TEST_POSTGRES_URL", "").strip()


def test_postgres_sink_migrates_real_rows_and_is_idempotent(tmp_path: Path):
    if not _PG_TEST_URL:
        pytest.skip("set CAPTURD_TEST_POSTGRES_URL to run the Postgres integration test")
    pytest.importorskip("psycopg")

    db_path = tmp_path / "capturd.sqlite3"
    _make_sqlite(db_path).close()

    sink = PostgresSink(_PG_TEST_URL)
    # Operate only on the tables this tool owns; leave the rest of the DB alone.
    for table in ("users", "mcp_tokens", "sessions", "jobs", "usage"):
        sink._conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    sink._conn.commit()
    sink.init_schema()

    first = migrate(db_path, sink)
    assert first["users"]["imported"] == 2
    assert first["usage"]["imported"] == 2        # distinct rows preserved
    assert first["jobs"]["imported"] == 1
    assert sink.count("usage") == 2
    assert sink.count("users") == 2

    second = migrate(db_path, sink)
    for table, r in second.items():
        assert r["imported"] == 0, table          # idempotent against a real driver
        assert r["resulting"] == first[table]["resulting"], table

    # The reviewer's users-conflict bug: a rerun after a user's email changed
    # must be a no-op (conflict on PK id), NOT a duplicate-key hard-fail.
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE users SET email='a@changed.co' WHERE id='u1'")
    conn.commit()
    conn.close()
    third = migrate(db_path, sink)
    assert third["users"]["imported"] == 0
    assert sink.count("users") == 2
