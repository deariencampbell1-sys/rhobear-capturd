"""Captur'd sqlite->Postgres migrator tests (no Postgres needed: MemorySink)."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent / "service"
sys.path.insert(0, str(SERVICE))

from app import store  # noqa: E402
from scripts.migrate_sqlite_to_postgres import MemorySink, migrate  # noqa: E402


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
        ("INSERT INTO usage(user_id,kind,n,at) VALUES(?,?,?,?)",
         ("u1", "generation", 1, 400)),
        ("INSERT INTO usage(user_id,kind,n,at) VALUES(?,?,?,?)",
         ("u1", "generation", 1, 400)),  # duplicate row shape -> same _key
    ]
    for sql, params in seed:
        conn.execute(sql, params)
    conn.commit()
    return conn


def test_migrate_counts_and_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "capturd.sqlite3"
    _make_sqlite(db_path).close()

    sink = MemorySink()
    first = migrate(db_path, sink)
    assert first["users"]["imported"] == 2
    assert first["usage"]["source"] == 2
    assert first["usage"]["imported"] == 1          # duplicate row -> one row
    assert first["usage"]["duplicates"] == 1
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


def test_config_env_override_wins():
    # Subprocess so config re-reads env at import (it caches at import time).
    code = "from app import config; print(config.DATA_DIR)"
    out = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "CAPTURD_DATA_DIR": "/tmp/capturd-test"},
        capture_output=True, text=True, cwd=str(SERVICE),
    )
    assert out.returncode == 0, out.stderr
    assert "/tmp/capturd-test" in out.stdout.strip().replace("\\", "/")
