"""Captur'd sqlite->Postgres migrator tests.

Covers the layers the reviewer asked for, with no Postgres server in CI:

  * Direct unit tests for the real ``PostgresSink`` write path — ``sql_for`` and
    ``params_for`` (pure), plus a *fake-psycopg-driver* test that drives the real
    ``PostgresSink`` class (``init_schema``/``upsert``/``count``) and asserts the
    exact SQL + params it sends. This is what makes CI prove the production write
    path is correct instead of trusting an unexercised code path.
  * A real-Postgres integration test (auto-skipped when ``CAPTURD_TEST_POSTGRES_URL``
    is unset or psycopg isn't installed) that migrates actual rows and proves
    counts + idempotency against a live driver, with a non-destructive host guard.
  * Config path-default/env-override tests, incl. both nt and posix default
    branches and the unprivileged ``ensure_dirs`` fallback.
  * ``main()`` guard tests (missing CAPTURD_DATABASE_URL / missing sqlite source).
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

SERVICE = Path(__file__).resolve().parent.parent / "service"
sys.path.insert(0, str(SERVICE))

from app import config as app_config  # noqa: E402
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
    # Bare ON CONFLICT DO NOTHING (no target column): a row that collides on ANY
    # unique constraint — PK or a non-PK unique like users.email — is skipped,
    # never raised. This is what makes an idempotent re-run safe.
    assert sql_for("users") == (
        "INSERT INTO users (id, email, plan, created_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT DO NOTHING")
    assert sql_for("mcp_tokens") == (
        "INSERT INTO mcp_tokens (token, user_id, created_at) VALUES (%s, %s, %s) "
        "ON CONFLICT DO NOTHING")
    assert sql_for("sessions") == (
        "INSERT INTO sessions (token, user_id, created_at) VALUES (%s, %s, %s) "
        "ON CONFLICT DO NOTHING")
    assert sql_for("jobs") == (
        "INSERT INTO jobs (id, user_id, kind, status, output, detail, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING")
    assert sql_for("usage") == (
        "INSERT INTO usage (_key, user_id, kind, n, at) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT DO NOTHING")


def test_sql_for_users_uses_no_conflict_target():
    """The users upsert must NOT target a mutable column (email), or a re-run
    after an email change could hard-fail. Bare ``ON CONFLICT DO NOTHING`` — no
    ``ON CONFLICT (email)``, no ``ON CONFLICT (id)`` — skips either cleanly."""
    s = sql_for("users")
    assert "ON CONFLICT" in s
    assert "ON CONFLICT (email)" not in s
    assert "ON CONFLICT (id)" not in s


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


def test_usage_key_stable_across_processes():
    """Uses SHA-256 (not seeded ``hash()``), so keys are identical regardless of
    PYTHONHASHSEED — otherwise a rerun in a fresh process could drop/duplicate usage."""
    code = (
        "from scripts.migrate_sqlite_to_postgres import _usage_key; "
        "print(_usage_key({'_rowid': 7, 'user_id': 'u1', 'kind': 'generation', 'n': 1, 'at': 400}))"
    )
    keys = set()
    for seed in ("0", "12345", "99999"):
        p = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True, text=True, cwd=str(SERVICE),
        )
        assert p.returncode == 0, p.stderr
        keys.add(p.stdout.strip())
    assert len(keys) == 1, keys


class _FakeCursor:
    def __init__(self, conn, table_counts):
        self._conn = conn
        self._counts = table_counts
        self._row = None

    def execute(self, sql, params=None):
        sql_norm = " ".join(sql.strip().split())
        self._conn.executes.append((sql_norm, params))
        if sql_norm.upper().startswith("SELECT COUNT"):
            m = re.search(r"\bfrom\s+(\w+)", sql_norm, re.I)
            table = m.group(1) if m else None
            self._row = (self._counts.get(table, 0),)
        else:
            self._row = None

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    def __init__(self):
        self.executes = []
        self.counts = {}

    def cursor(self):
        return _FakeCursor(self, self.counts)

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class _FakePsycopg:
    def __init__(self, conn):
        self._conn = conn
        self._connect_args = None

    def connect(self, url):
        self._connect_args = url
        return self._conn


def test_postgres_sink_write_path_with_fake_driver(monkeypatch):
    """Exercise the REAL PostgresSink class (the production write path) against a
    fake psycopg driver so the generated SQL, params, and schema DDL are proven
    in default CI — not just the pure helper functions."""
    fake_conn = _FakeConnection()
    fake = _FakePsycopg(fake_conn)
    monkeypatch.setitem(sys.modules, "psycopg", fake)

    sink = PostgresSink("postgresql://u:p@localhost:5432/capturd")
    assert fake._connect_args == "postgresql://u:p@localhost:5432/capturd"

    sink.init_schema()
    assert any("CREATE TABLE IF NOT EXISTS users" in sql for sql, _ in fake_conn.executes)
    assert any("CREATE TABLE IF NOT EXISTS usage" in sql for sql, _ in fake_conn.executes)

    sink.upsert("users", {"id": "u1", "email": "a@b.co", "plan": "free", "created_at": 100})
    assert any(
        sql == sql_for("users") and params == ("u1", "a@b.co", "free", 100)
        for sql, params in fake_conn.executes
    )

    sink.upsert("usage", {"_key": "k", "user_id": "u1", "kind": "generation", "n": 1, "at": 400})
    assert any(
        sql == sql_for("usage") and params == ("k", "u1", "generation", 1, 400)
        for sql, params in fake_conn.executes
    )

    # count() runs a real SELECT count(*) and returns an int from the driver.
    assert sink.count("users") == 0
    fake_conn.counts["users"] = 5
    assert sink.count("users") == 5
    assert any(sql == "SELECT count(*) FROM users" for sql, _ in fake_conn.executes)


# ---------------------------------------------------------------------------
# Memory-sink migrate semantics
# ---------------------------------------------------------------------------


def test_memory_sink_first_write_wins_no_overwrite():
    """MemorySink must never overwrite an existing key — matching the destination's
    ON CONFLICT DO NOTHING so dry-run counts don't diverge from the real sink."""
    sink = MemorySink()
    sink.upsert("users", {"id": "u1", "email": "a@b.co", "plan": "free", "created_at": 100})
    sink.upsert("users", {"id": "u1", "email": "CHANGED", "plan": "pro", "created_at": 999})
    assert sink.count("users") == 1
    assert sink.rows["users"]["u1"]["email"] == "a@b.co"  # first write won


def test_sink_protocol_surface():
    """Both sinks expose the full Sink protocol the migrator relies on."""
    memory = MemorySink()
    pg = PostgresSink.__new__(PostgresSink)
    for sink in (memory, pg):
        for meth in ("upsert", "count", "commit", "init_schema", "close"):
            assert callable(getattr(sink, meth, None)), f"{type(sink).__name__} missing {meth}"
    assert callable(PostgresSink.rollback)


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


def test_platform_default_selection_both_branches():
    """Both the nt and posix default branches are asserted literally — a Windows
    default can't regress silently on a Linux CI surface."""
    assert app_config._default_data_dir("nt") == Path(r"D:\capturd-service\data")
    assert app_config._default_vault_dir("nt") == Path(r"D:\rhobear-agent-vault")
    assert app_config._default_data_dir("posix") == Path("/var/lib/capturd")
    assert app_config._default_vault_dir("posix") == Path("/var/lib/capturd-agent-vault")


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


def test_ensure_dirs_falls_back_on_permission_error(monkeypatch):
    """Unprivileged runs (no systemd, /var/lib not writable) must fall back to a
    user-writable location instead of crashing at startup."""
    orig = (app_config.DATA_DIR, app_config.JOBS_DIR, app_config.DB_PATH)
    real_mkdir = Path.mkdir

    def _raising_mkdir(self, *a, **k):
        if str(self) == str(orig[0]):
            raise PermissionError("read-only filesystem")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", _raising_mkdir)
    app_config.ensure_dirs()
    try:
        fallback = app_config._FALLBACK_DATA_DIR
        assert app_config.DATA_DIR == fallback
        assert app_config.JOBS_DIR == fallback / "jobs"
        assert app_config.DB_PATH == fallback / "capturd.sqlite3"
    finally:
        app_config.DATA_DIR, app_config.JOBS_DIR, app_config.DB_PATH = orig


# ---------------------------------------------------------------------------
# main() guard tests (subprocess — the script prints + returns non-zero)
# ---------------------------------------------------------------------------


def _run_migrator(*args, **env_extra):
    env = {**os.environ}
    env.pop("CAPTURD_DATABASE_URL", None)
    for k, v in env_extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    return subprocess.run(
        [sys.executable, str(SERVICE / "scripts" / "migrate_sqlite_to_postgres.py"), *args],
        env=env, capture_output=True, text=True, cwd=str(SERVICE),
    )


def test_main_returns_2_when_database_url_missing(tmp_path: Path):
    db_path = tmp_path / "capturd.sqlite3"
    _make_sqlite(db_path).close()
    p = _run_migrator("--sqlite", str(db_path))
    assert p.returncode == 2
    assert "CAPTURD_DATABASE_URL" in p.stderr


def test_main_returns_2_when_sqlite_source_missing(tmp_path: Path):
    p = _run_migrator("--sqlite", str(tmp_path / "nope.sqlite3"), "--dry-run")
    assert p.returncode == 2
    assert "not found" in p.stderr


def test_main_returns_1_on_migration_failure(tmp_path: Path):
    """A real run that can't reach Postgres (or has no psycopg) must fail cleanly:
    exit code 1 and a single readable ERROR line, not an uncaught traceback."""
    db_path = tmp_path / "capturd.sqlite3"
    _make_sqlite(db_path).close()
    p = _run_migrator(
        "--sqlite", str(db_path), CAPTURD_DATABASE_URL="postgresql://u:p@no-such-host.invalid:5432/db"
    )
    assert p.returncode == 1
    assert "ERROR" in p.stderr


# ---------------------------------------------------------------------------
# Real Postgres integration test (auto-skipped without a PG URL)
# ---------------------------------------------------------------------------


def _pg_url_is_test_safe(url: str) -> bool:
    """Refuse to run the destructive integration test against anything that looks
    like a real/lasting database. Only localhost, loopback, or an explicit 'test'
    host/db-name."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    db = (parsed.path or "").strip("/").lower()
    return host in ("localhost", "127.0.0.1", "::1") or "test" in host or "test" in db


def test_pg_url_is_test_safe():
    assert _pg_url_is_test_safe("postgresql://u:p@localhost:5432/capturd")
    assert _pg_url_is_test_safe("postgresql://u:p@127.0.0.1/capturd")
    assert _pg_url_is_test_safe("postgresql://u:p@db.test.internal/capturd")
    assert _pg_url_is_test_safe("postgresql://u:p@localhost/capturd_test")
    assert not _pg_url_is_test_safe("postgresql://u:p@prod-db.example.com/capturd")


_PG_TEST_URL = os.environ.get("CAPTURD_TEST_POSTGRES_URL", "").strip()


def test_postgres_sink_migrates_real_rows_and_is_idempotent(tmp_path: Path):
    if not _PG_TEST_URL:
        pytest.skip("set CAPTURD_TEST_POSTGRES_URL to run the Postgres integration test")
    pytest.importorskip("psycopg")
    if not _pg_url_is_test_safe(_PG_TEST_URL):
        pytest.skip(
            "refusing to run destructive integration test against a non-test "
            "Postgres host/db (CAPTURD_TEST_POSTGRES_URL)"
        )

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
    # must be a no-op (bare ON CONFLICT DO NOTHING covers both PK and email-unique),
    # NOT a duplicate-key hard-fail.
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE users SET email='a@changed.co' WHERE id='u1'")
    conn.commit()
    conn.close()
    third = migrate(db_path, sink)
    assert third["users"]["imported"] == 0
    assert sink.count("users") == 2

    sink.close()
